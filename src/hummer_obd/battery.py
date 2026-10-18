"""PiSugar battery watch and graceful shutdown.

The node runs on a PiSugar pack in a vehicle -- a PiSugar2 until 2026-10-17 and
a PiSugar3 since.  A lithium cell that is run flat
is not merely inconvenient: an unexpected power loss can corrupt the SD card
mid-write, and the SQLite database and the append-only transcript on it are
readings nobody can take again.  This module watches the cell and powers the
node down cleanly while there is still charge to do it with.

It is deliberately paranoid, because the failure mode it can cause is worse
than the one it prevents.  A node that shuts down when it should not is a node
that is simply gone until somebody walks out to the truck and presses a button.
So:

* the shutdown threshold is a **measured voltage**, not a modelled percentage;
* an implausible reading is refused rather than acted on;
* one low reading does nothing -- a run of them, over minutes, is required;
* a cell that is charging is never acted on, even below the threshold; and
* nothing here ever writes to the power IC.  Every I2C access is a read.

Why the default action is *still not* a shutdown
-----------------------------------------------
It used to be for one reason: a PiSugar2 cannot power the Pi back on by itself.
Its own library says so -- ``toggle_power_restore`` returns "not supported" for
the IP5209 and IP5312 and is implemented only for the PiSugar 3.

**That reason is gone, and it was only ever half of it.**  The PiSugar3 fitted
on 2026-10-17 does support automatic power-on, and on this node the bit is
already set: control register 1 (:data:`PISUGAR3_CTR1`, 0x02) read 0xFC, which
has :data:`PISUGAR3_RESTORE_BIT` set.  Nothing had to be programmed for it.

The other half still stands, and it is the half that stranded the node.
**Halting does not cut power.**  The vendor's ``pisugar-poweroff`` service
exists precisely because something must tell the chip to cut, during shutdown,
over I2C -- and that service is not installed here.  So a ``systemctl
poweroff`` leaves the Pi halted with the board still supplying it from the cell,
and the restore bit never fires, because from the board's point of view power
never went away.  Automatic power-on only helps across an output the board
actually cut.

So the default action remains to stop the *collector* -- the thing actually
drawing power and holding the serial port -- and leave the operating system up
and reachable.  A node that is still reachable can be told what to do next; a
halted one cannot.  ``--poweroff`` becomes genuinely safe on this hardware the
day something cuts the output on the way down, and not before.

Note what is NOT a problem on this node: a Raspberry Pi Zero 2 W has no power
latch and boots whenever 5 V appears, so "come back on when power is applied"
needs nothing programmed on the Pi side at all.  The only way it fails to come
back is if it was halted while the board kept supplying it, which is the
paragraph above.

Hardware identification is done by measurement rather than by the label on the
box, and the board change is why that matters.  A PiSugar2 carries an IP5209 and
a PiSugar2 Pro an IP5312, reporting voltage from different registers; the
PiSugar3 is a single MCU at a different address again.  On this node, before any
of this was written down: 0x75 and 0x32 stopped acknowledging altogether, 0x57
answers, its firmware register reads "v1.3.4", and its voltage pair read
3860 mV.  :func:`identify_chip` re-checks all of that at run time rather than
assuming it, which is how the watch reported an unusable reading every 30
seconds after the swap instead of quietly converting a wrong register.

One thing has not changed: **nothing here ever writes to the power IC.**  Every
I2C access is a read, including the control register the restore bit lives in.
A single wrong byte written to a power-management MCU can cut the supply to the
node it is running on, and no reading is worth that.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Final, Optional

__all__ = [
    "ACTIONS",
    "BatteryReading",
    "ChipProfile",
    "IP5209",
    "IP5312",
    "PISUGAR3",
    "PISUGAR3_CTR1",
    "PISUGAR3_PLUGGED_BIT",
    "PISUGAR3_RESTORE_BIT",
    "PROFILES",
    "Reader",
    "I2C_ADDRESS",
    "PLAUSIBLE_MIN_V",
    "PLAUSIBLE_MAX_V",
    "open_i2c_reader",
    "read_voltage",
    "identify_chip",
    "BatteryWatch",
    "main",
]

#: What to do when the cell is genuinely low.  ``stop-collector`` is the
#: default because this hardware cannot power itself back on.
ACTIONS: Final[frozenset[str]] = frozenset({"stop-collector", "poweroff"})

#: The PiSugar2 power IC.  The SD3078 real-time clock sits at 0x32 and is not
#: touched by this module.
I2C_ADDRESS: Final[int] = 0x75

#: A reading outside this range is not a cell voltage, and the right response
#: is to distrust the number rather than power the node off because of it.
#:
#: The floor is 3.0 V for a physical reason rather than a chosen one: the
#: reading is taken *by a running Raspberry Pi*, so the cell is necessarily
#: above the PiSugar boost converter's cutoff.  A number below that did not
#: come from the cell -- it is a bad transaction, a missing HAT, or the wrong
#: register pair, which is exactly how the IP5312 profile is ruled out here
#: (it reads 2.6 V on this node, a voltage at which nothing could have read
#: it).  The default shutdown threshold of 3.40 V sits comfortably above the
#: floor, so a genuinely draining cell trips the shutdown well before it could
#: reach the range this call would distrust.
PLAUSIBLE_MIN_V: Final[float] = 3.0
PLAUSIBLE_MAX_V: Final[float] = 4.5


@dataclass(frozen=True)
class ChipProfile:
    """How one power IC reports battery voltage.

    ``low_register``/``high_register`` hold a 14-bit count.  ``base_mv`` and
    ``step_mv`` convert it, and ``signed_bit`` marks the chips that encode a
    below-base reading as a negative offset instead of a smaller count.
    """

    name: str
    low_register: int
    high_register: int
    base_mv: float = 2600.0
    step_mv: float = 0.26855
    signed_bit: Optional[int] = None
    #: Which slave answers for this chip.  Carried on the profile rather than
    #: passed alongside it because the PiSugar3 moved: identifying the board now
    #: means trying more than one address, and a profile that did not know its
    #: own would have to be paired with one by every caller.
    address: int = I2C_ADDRESS
    #: Whether a register read needs a single combined (repeated-start)
    #: transaction rather than a write followed by a separate read.  MEASURED,
    #: not assumed: read the PiSugar3 the PiSugar2 way and every register
    #: returns 0x03 -- 0x02, 0x22, 0xA2 and 0xD0 all read 0x03 on this node,
    #: which is a device acknowledging the address while the register number
    #: never takes effect.  That is the worst shape of failure available here,
    #: because 0x03 is a number and a profile would happily convert it.
    combined: bool = False
    #: Whether the register pair IS the millivolt value, big-endian, rather
    #: than a 14-bit count to be scaled.  The PiSugar3 reports plain millivolts.
    direct_mv: bool = False

    def to_volts(self, low: int, high: int) -> float:
        if self.direct_mv:
            return ((high << 8) | low) / 1000.0
        if self.signed_bit is not None and high & self.signed_bit:
            # A negative offset: the count is stored two's-complement across
            # the top bits, so it is sign-extended before being subtracted.
            count = ((high | 0xC0) << 8) + low
            return (self.base_mv - count * self.step_mv) / 1000.0
        count = ((high & 0x1F) << 8) + low
        return (self.base_mv + count * self.step_mv) / 1000.0


#: PiSugar2 (standard).  Confirmed on this node by measurement.
IP5209: Final[ChipProfile] = ChipProfile(
    name="IP5209", low_register=0xA2, high_register=0xA3, signed_bit=0x20)

#: PiSugar2 Pro.  Kept so :func:`identify_chip` can tell the two apart rather
#: than assuming, and so a different node is not silently misread.
IP5312: Final[ChipProfile] = ChipProfile(
    name="IP5312", low_register=0xD0, high_register=0xD1)

#: PiSugar3, fitted to this node on 2026-10-17 in place of the PiSugar2.
#:
#: A different board in three ways at once, which is why the old profiles do not
#: merely read the wrong numbers but cannot read at all.  The single MCU answers
#: at 0x57 instead of the IP5209's 0x75; it wants a combined transaction; and it
#: reports the cell as plain big-endian millivolts rather than a count to be
#: scaled from a 2600 mV base.  Registers are the vendor's own, from
#: pisugar-core's pisugar3 driver: 0x22 is the voltage high byte and 0x23 the
#: low byte.  The RTC moved too -- it is inside this MCU rather than on a
#: separate SD3078 at 0x32, and 0x32 stopped answering when the board changed.
#:
#: Confirmed on this node by measurement before any of it was written down:
#: 0x75 and 0x32 no longer acknowledge at all, 0x57 does, firmware 0xE2 reads
#: "v1.3.4", and 0x22/0x23 read 3860 mV while the pack was on external power.
PISUGAR3: Final[ChipProfile] = ChipProfile(
    name="PiSugar3", low_register=0x23, high_register=0x22,
    address=0x57, combined=True, direct_mv=True)

#: Register 1 of the PiSugar3's global control, and the two bits of it this
#: project reads.  Nothing here writes them -- see the module docstring -- but
#: reading them is how the node can say whether it will come back by itself.
PISUGAR3_CTR1: Final[int] = 0x02
#: Set when the board will power the Pi up again once external power returns.
PISUGAR3_RESTORE_BIT: Final[int] = 1 << 4
#: Set while external power is connected.  Useful as a sanity check on the
#: register as a whole: it has to agree with whether the truck is awake.
PISUGAR3_PLUGGED_BIT: Final[int] = 1 << 7


@dataclass
class BatteryReading:
    volts: Optional[float]
    chip: str
    plausible: bool
    detail: str = ""


#: ``ioctl`` request that binds an open /dev/i2c-N to a slave address.
_I2C_SLAVE: Final[int] = 0x0703
#: ``ioctl`` request that submits several messages as one combined transaction,
#: and the flag that marks one of them a read.  Needed by the PiSugar3.
_I2C_RDWR: Final[int] = 0x0707
_I2C_M_RD: Final[int] = 0x0001

#: ``(address, register, combined) -> byte``.  The third argument is the
#: transaction style and is required, not defaulted: see :func:`open_i2c_reader`.
Reader = Callable[[int, int, bool], int]


def open_i2c_reader(bus: int = 1) -> Reader:
    """Return a register reader backed by ``/dev/i2c-<bus>``.

    Deliberately the standard library and nothing else.  Pulling in an I2C
    package to spell two ioctls would add a dependency to a project that
    currently has two, for no capability.  Group membership (``i2c``) is enough,
    so this needs no privilege either.

    Two transaction styles, because the hardware needs both.  The IP5209 and
    IP5312 are happy with a write of the register number followed by a separate
    one-byte read.  The PiSugar3 is not: done that way every one of its
    registers reads 0x03, so it needs a single combined transaction with a
    repeated start, which is ``I2C_RDWR`` with a write message and a read
    message submitted together.  ``combined`` has no default on purpose -- the
    penalty for the wrong style is not an error but a plausible-looking
    constant, and a default is how a caller gets it wrong silently.
    """
    import ctypes
    import fcntl
    import os

    class _Msg(ctypes.Structure):
        _fields_ = [("addr", ctypes.c_uint16), ("flags", ctypes.c_uint16),
                    ("len", ctypes.c_uint16),
                    ("buf", ctypes.POINTER(ctypes.c_uint8))]

    class _Ioctl(ctypes.Structure):
        _fields_ = [("msgs", ctypes.POINTER(_Msg)), ("nmsgs", ctypes.c_uint32)]

    path = f"/dev/i2c-{bus}"
    fd = os.open(path, os.O_RDWR)

    def read(address: int, register: int, combined: bool) -> int:
        if combined:
            out = (ctypes.c_uint8 * 1)(register)
            back = (ctypes.c_uint8 * 1)()
            pair = (_Msg * 2)(_Msg(address, 0, 1, out),
                              _Msg(address, _I2C_M_RD, 1, back))
            fcntl.ioctl(fd, _I2C_RDWR, _Ioctl(pair, 2))
            return back[0]
        fcntl.ioctl(fd, _I2C_SLAVE, address)
        os.write(fd, bytes([register]))
        return os.read(fd, 1)[0]

    return read


def read_voltage(reader: Reader, chip: ChipProfile,
                 address: Optional[int] = None) -> BatteryReading:
    """Read the cell voltage, refusing a result that cannot be a cell voltage.

    *address* overrides the profile's own, for a node that has moved a board to
    a second bus address.  Left alone it is the profile's, because which slave
    answers is a property of the chip and not of the caller.
    """
    at = chip.address if address is None else address
    try:
        low = reader(at, chip.low_register, chip.combined)
        high = reader(at, chip.high_register, chip.combined)
    except Exception as exc:  # noqa: BLE001 - any bus failure is the same answer
        return BatteryReading(None, chip.name, False, f"i2c read failed: {exc}")
    volts = chip.to_volts(low, high)
    if not PLAUSIBLE_MIN_V <= volts <= PLAUSIBLE_MAX_V:
        return BatteryReading(
            volts, chip.name, False,
            f"{volts:.3f} V is outside {PLAUSIBLE_MIN_V}-{PLAUSIBLE_MAX_V} V; "
            "treating the reading as untrustworthy rather than acting on it")
    return BatteryReading(volts, chip.name, True)


#: Every board this node has carried, newest first.  Order is not precedence --
#: :func:`identify_chip` refuses an ambiguous answer rather than taking the
#: first -- it is only the order the candidates are read in.
PROFILES: Final[tuple[ChipProfile, ...]] = (PISUGAR3, IP5209, IP5312)


def identify_chip(reader: Reader,
                  address: Optional[int] = None) -> Optional[ChipProfile]:
    """Decide which board is present by which profile reads plausibly.

    Every candidate is read and the one returning a voltage a lithium cell could
    actually hold is chosen.  If more than one looks plausible, none is trusted:
    an ambiguous identification is worse than none, because everything after it
    would be a confident misreading.

    This now spans boards and not just the two PiSugar2 power ICs, because the
    PiSugar3 answers at a different address with a different transaction style
    and a different encoding.  A reader that cannot reach an address raises, and
    :func:`read_voltage` turns that into an implausible reading -- so a board
    that is simply absent rules itself out without special handling, which is
    how 0x75 and 0x32 rule themselves out on this node now.
    """
    plausible = [c for c in PROFILES
                 if read_voltage(reader, c, address).plausible]
    return plausible[0] if len(plausible) == 1 else None


class BatteryWatch:
    def __init__(self, *, reader: Reader,
                 chip: Optional[ChipProfile] = None,
                 shutdown_v: float = 3.40,
                 interval_s: float = 30.0,
                 consecutive: int = 5,
                 dry_run: bool = False,
                 action: str = "stop-collector",
                 shutdown: Optional[Callable[[], None]] = None,
                 logger=print) -> None:
        if not PLAUSIBLE_MIN_V <= shutdown_v <= PLAUSIBLE_MAX_V:
            raise ValueError(
                f"shutdown_v {shutdown_v} is outside the plausible cell range")
        if interval_s <= 0:
            raise ValueError("interval_s must be greater than zero")
        if consecutive < 1:
            raise ValueError("consecutive must be at least 1")
        if action not in ACTIONS:
            raise ValueError(f"action must be one of {sorted(ACTIONS)}")
        self.reader = reader
        self.chip = chip
        self.shutdown_v = shutdown_v
        self.interval_s = interval_s
        self.consecutive = consecutive
        self.dry_run = dry_run
        self.action = action
        self._shutdown = shutdown or (
            self._poweroff if action == "poweroff" else self._stop_collector)
        self.log = logger
        self.running = True
        #: The run of consecutive low readings, and what they were.
        self.low_streak: list[float] = []

    def stop(self, *_args) -> None:
        self.running = False

    @staticmethod
    def _poweroff() -> None:
        """Halt the OS.  Strands this node -- see the module docstring."""
        subprocess.run(["/usr/bin/systemctl", "poweroff"], check=False)

    @staticmethod
    def _stop_collector() -> None:
        """Stop vehicle polling and leave the node up and reachable.

        SIGTERM rather than a service stop: the collector installs a handler
        that closes the SQLite session and flushes the raw log, it runs as this
        same user, and signalling it needs no privilege.  A node that is still
        reachable can be told what to do next; a halted one cannot.
        """
        subprocess.run(["/usr/bin/pkill", "-TERM", "-f", "hummer_obd.collector"],
                       check=False)

    def _charging(self) -> bool:
        """True when the cell is gaining charge across the low streak.

        Detected from the trend rather than from a status register, because a
        rising voltage is the observation that matters and it needs no register
        whose meaning would have to be assumed.  A pack that is below the
        threshold but recovering is on a charger, and powering the node off
        then would strand it for no reason.
        """
        return len(self.low_streak) >= 2 and self.low_streak[-1] > self.low_streak[0]

    def sample(self) -> BatteryReading:
        chip = self.chip or identify_chip(self.reader)
        if chip is None:
            return BatteryReading(None, "unknown", False,
                                  "no power IC read a plausible cell voltage")
        self.chip = chip
        return read_voltage(self.reader, chip)

    def evaluate(self, reading: BatteryReading) -> Optional[str]:
        """Return the reason to shut down, or ``None`` to keep running."""
        if not reading.plausible or reading.volts is None:
            # An untrustworthy reading breaks the streak.  A flapping bus must
            # not accumulate towards a shutdown.
            self.low_streak.clear()
            return None
        if reading.volts > self.shutdown_v:
            self.low_streak.clear()
            return None
        self.low_streak.append(reading.volts)
        if len(self.low_streak) < self.consecutive:
            return None
        if self._charging():
            self.log(f"  below {self.shutdown_v:.2f} V but rising "
                     f"({self.low_streak[0]:.3f} -> {self.low_streak[-1]:.3f} V); "
                     "on charge, not shutting down")
            return None
        window = self.interval_s * (len(self.low_streak) - 1)
        return (f"{len(self.low_streak)} consecutive readings at or below "
                f"{self.shutdown_v:.2f} V over {window:.0f}s "
                f"(last {self.low_streak[-1]:.3f} V)")

    def run(self, max_cycles: int = 0) -> int:
        self.log(f"battery watch: {self.action} below {self.shutdown_v:.2f} V after "
                 f"{self.consecutive} readings {self.interval_s:g}s apart"
                 f"{' [DRY RUN]' if self.dry_run else ''}")
        cycles = 0
        while self.running:
            reading = self.sample()
            cycles += 1
            if reading.plausible and reading.volts is not None:
                self.log(f"  {reading.volts:.3f} V [{reading.chip}]")
            else:
                self.log(f"  unusable reading: {reading.detail}")
            reason = self.evaluate(reading)
            if reason:
                self.log(f"LOW BATTERY ({self.action}): {reason}")
                if self.dry_run:
                    self.log("  dry run: taking no action")
                    self.low_streak.clear()
                else:
                    self._shutdown()
                    if self.action == "poweroff":
                        return 0
                    # The node stays up, so the streak is cleared and the watch
                    # keeps going: if the cell recovers, nothing more is needed,
                    # and if it does not, saying so again is the only useful
                    # thing left to do.
                    self.low_streak.clear()
            if max_cycles and cycles >= max_cycles:
                return 0
            end = time.monotonic() + self.interval_s
            while self.running and time.monotonic() < end:
                time.sleep(min(1.0, end - time.monotonic()))
        return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Watch the PiSugar2 cell and power the node down cleanly")
    parser.add_argument("--bus", type=int, default=1)
    parser.add_argument("--shutdown-v", type=float, default=3.40,
                        help="cell voltage at or below which to shut down")
    parser.add_argument("--interval-s", type=float, default=30.0)
    parser.add_argument("--consecutive", type=int, default=5,
                        help="consecutive low readings required before acting")
    parser.add_argument("--once", action="store_true",
                        help="report one reading and exit; never shuts down")
    parser.add_argument("--dry-run", action="store_true",
                        help="log the decision but take no action")
    parser.add_argument("--on-low", choices=sorted(ACTIONS), default="stop-collector",
                        help="stop-collector (default) ends vehicle polling and "
                             "leaves the node reachable; poweroff halts the OS, "
                             "which on a PiSugar2 STRANDS the node -- this "
                             "hardware cannot power itself back on")
    args = parser.parse_args(argv)
    try:
        reader = open_i2c_reader(args.bus)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: no I2C bus {args.bus}: {exc}", file=sys.stderr)
        return 2
    if args.once:
        chip = identify_chip(reader)
        if chip is None:
            print("ERROR: no power IC read a plausible cell voltage", file=sys.stderr)
            return 2
        reading = read_voltage(reader, chip)
        print(f"{reading.volts:.3f} V [{reading.chip}]")
        return 0
    watch = BatteryWatch(reader=reader, shutdown_v=args.shutdown_v,
                         interval_s=args.interval_s, consecutive=args.consecutive,
                         dry_run=args.dry_run, action=args.on_low)
    import signal
    signal.signal(signal.SIGTERM, watch.stop)
    signal.signal(signal.SIGINT, watch.stop)
    return watch.run()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
