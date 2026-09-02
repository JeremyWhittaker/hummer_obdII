"""PiSugar2 battery watch and graceful shutdown.

The node runs on a PiSugar2 pack in a vehicle.  A lithium cell that is run flat
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
* a cell that is charging is never shut down, even below the threshold; and
* nothing here ever writes to the power IC.  Every I2C access is a read.

Hardware identification was done by measurement rather than by the label on the
box.  A PiSugar2 carries an IP5209 and a PiSugar2 Pro carries an IP5312, and
they report battery voltage from different registers.  On this node the IP5209
registers read a plausible cell voltage while the IP5312 registers read zero,
which would be 2.6 V -- below the voltage at which the Pi could have taken the
reading.  :data:`IP5209` is therefore the confirmed chip, and
:func:`identify_chip` re-checks that at run time rather than assuming it.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Final, Optional

__all__ = [
    "BatteryReading",
    "ChipProfile",
    "IP5209",
    "IP5312",
    "I2C_ADDRESS",
    "PLAUSIBLE_MIN_V",
    "PLAUSIBLE_MAX_V",
    "open_i2c_reader",
    "read_voltage",
    "identify_chip",
    "BatteryWatch",
    "main",
]

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

    def to_volts(self, low: int, high: int) -> float:
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


@dataclass
class BatteryReading:
    volts: Optional[float]
    chip: str
    plausible: bool
    detail: str = ""


#: ``ioctl`` request that binds an open /dev/i2c-N to a slave address.
_I2C_SLAVE: Final[int] = 0x0703


def open_i2c_reader(bus: int = 1) -> Callable[[int, int], int]:
    """Return a register reader backed by ``/dev/i2c-<bus>``.

    Deliberately the standard library and nothing else.  A register read is a
    write of the register number followed by a one-byte read, which ``fcntl``
    and ``os`` do directly; pulling in an I2C package to spell that would add a
    dependency to a project that currently has two, for no capability.  Group
    membership (``i2c``) is enough, so this needs no privilege either.
    """
    import fcntl
    import os

    path = f"/dev/i2c-{bus}"
    fd = os.open(path, os.O_RDWR)

    def read(address: int, register: int) -> int:
        fcntl.ioctl(fd, _I2C_SLAVE, address)
        os.write(fd, bytes([register]))
        return os.read(fd, 1)[0]

    return read


def read_voltage(reader: Callable[[int, int], int], chip: ChipProfile,
                 address: int = I2C_ADDRESS) -> BatteryReading:
    """Read the cell voltage, refusing a result that cannot be a cell voltage."""
    try:
        low = reader(address, chip.low_register)
        high = reader(address, chip.high_register)
    except Exception as exc:  # noqa: BLE001 - any bus failure is the same answer
        return BatteryReading(None, chip.name, False, f"i2c read failed: {exc}")
    volts = chip.to_volts(low, high)
    if not PLAUSIBLE_MIN_V <= volts <= PLAUSIBLE_MAX_V:
        return BatteryReading(
            volts, chip.name, False,
            f"{volts:.3f} V is outside {PLAUSIBLE_MIN_V}-{PLAUSIBLE_MAX_V} V; "
            "treating the reading as untrustworthy rather than acting on it")
    return BatteryReading(volts, chip.name, True)


def identify_chip(reader: Callable[[int, int], int],
                  address: int = I2C_ADDRESS) -> Optional[ChipProfile]:
    """Decide which power IC is present by which one reads plausibly.

    Both candidates are read and the one returning a voltage a lithium cell
    could actually hold is chosen.  If both look plausible, neither is trusted:
    an ambiguous identification is worse than none, because everything after it
    would be a confident misreading.
    """
    candidates = [c for c in (IP5209, IP5312)
                  if read_voltage(reader, c, address).plausible]
    return candidates[0] if len(candidates) == 1 else None


class BatteryWatch:
    def __init__(self, *, reader: Callable[[int, int], int],
                 chip: Optional[ChipProfile] = None,
                 shutdown_v: float = 3.40,
                 interval_s: float = 30.0,
                 consecutive: int = 5,
                 dry_run: bool = False,
                 shutdown: Optional[Callable[[], None]] = None,
                 logger=print) -> None:
        if not PLAUSIBLE_MIN_V <= shutdown_v <= PLAUSIBLE_MAX_V:
            raise ValueError(
                f"shutdown_v {shutdown_v} is outside the plausible cell range")
        if interval_s <= 0:
            raise ValueError("interval_s must be greater than zero")
        if consecutive < 1:
            raise ValueError("consecutive must be at least 1")
        self.reader = reader
        self.chip = chip
        self.shutdown_v = shutdown_v
        self.interval_s = interval_s
        self.consecutive = consecutive
        self.dry_run = dry_run
        self._shutdown = shutdown or self._poweroff
        self.log = logger
        self.running = True
        #: The run of consecutive low readings, and what they were.
        self.low_streak: list[float] = []

    def stop(self, *_args) -> None:
        self.running = False

    @staticmethod
    def _poweroff() -> None:
        subprocess.run(["/usr/bin/systemctl", "poweroff"], check=False)

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
        self.log(f"battery watch: shutdown below {self.shutdown_v:.2f} V after "
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
                self.log(f"SHUTDOWN: {reason}")
                if self.dry_run:
                    self.log("  dry run: not powering off")
                    self.low_streak.clear()
                else:
                    self._shutdown()
                    return 0
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
                        help="log the decision but never power off")
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
                         dry_run=args.dry_run)
    import signal
    signal.signal(signal.SIGTERM, watch.stop)
    signal.signal(signal.SIGINT, watch.stop)
    return watch.run()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
