#!/usr/bin/python3 -I
"""PiSugar3 reserve shutdown and late systemd power cut (standard library only).

Installed as a root-owned standalone file; never imports user-writable code.
The legacy battery.py reader stays read-only and supports PiSugar2 as before.
Register operations follow PiSugar's pisugar-core/src/pisugar3.rs and datasheet.
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import math
import os
from pathlib import Path
import signal
import subprocess
import time
from dataclasses import dataclass

ADDRESS = 0x57
CTR1 = 0x02
RESTORE = 0x10
OUTPUT = 0x20
PLUGGED = 0x80
WRITE_ENABLE = 0x0B
MARKER = Path("/run/hummer-low-battery")


class I2C:
    """Combined register reads and explicit register/value writes on bus 1."""

    class Message(ctypes.Structure):
        _fields_ = [("addr", ctypes.c_uint16), ("flags", ctypes.c_uint16),
                    ("len", ctypes.c_uint16),
                    ("buf", ctypes.POINTER(ctypes.c_uint8))]

    class Transfer(ctypes.Structure):
        _fields_ = [("msgs", ctypes.c_void_p), ("nmsgs", ctypes.c_uint32)]

    def __init__(self):
        self.fd = os.open("/dev/i2c-1", os.O_RDWR | os.O_CLOEXEC)

    def close(self):
        os.close(self.fd)

    def transfer(self, *messages):
        array = (self.Message * len(messages))(*messages)
        fcntl.ioctl(self.fd, 0x0707,
                    self.Transfer(ctypes.cast(array, ctypes.c_void_p), len(array)))

    def read(self, register):
        out = (ctypes.c_uint8 * 1)(register)
        back = (ctypes.c_uint8 * 1)()
        self.transfer(self.Message(ADDRESS, 0, 1, out),
                      self.Message(ADDRESS, 1, 1, back))
        return back[0]

    def write(self, register, value):
        data = (ctypes.c_uint8 * 2)(register, value)
        self.transfer(self.Message(ADDRESS, 0, 2, data))

    def protected_write(self, register, value):
        try:
            self.write(WRITE_ENABLE, 0x29)
            self.write(register, value)
        finally:
            self.write(WRITE_ENABLE, 0x00)


@dataclass(frozen=True)
class State:
    volts: float
    percent: int
    control: int

    @property
    def plugged(self):
        return bool(self.control & PLUGGED)

    @property
    def restore(self):
        return bool(self.control & RESTORE)


def read_state(bus):
    # Reject a changing external-power indication rather than accumulate it.
    first = bus.read(CTR1)
    volts = ((bus.read(0x22) << 8) | bus.read(0x23)) / 1000.0
    percent = bus.read(0x2A)
    control = bus.read(CTR1)
    if (first ^ control) & (PLUGGED | RESTORE | OUTPUT):
        raise ValueError("power state changed during reading")
    if not 3.0 <= volts <= 4.5 or not 0 <= percent <= 100:
        raise ValueError(f"implausible battery reading: {volts:.3f} V, {percent}%")
    if not control & OUTPUT:
        raise ValueError("PiSugar3 output is not enabled; wrong board or bad reading")
    return State(volts, percent, control)


def identify(bus):
    version = bytes(bus.read(0xE2 + i) for i in range(15)).split(b"\0")[0]
    # The write-protection and delayed-cut commands require this firmware.
    text = version.decode("ascii")
    parts = tuple(int(p) for p in text.removeprefix("v").split("."))
    if not text.startswith("v") or len(parts) != 3 or parts < (1, 2, 4):
        raise ValueError(f"unsupported PiSugar3 firmware: {text!r}")
    return text, read_state(bus)


def ensure_restore(bus):
    _, state = identify(bus)
    if not state.restore:
        bus.protected_write(CTR1, state.control | RESTORE)
        if not read_state(bus).restore:
            raise RuntimeError("auto-power-on did not read back enabled")


class Reserve:
    def __init__(self, percent=10, voltage=3.40, consecutive=3):
        if not 5 <= percent <= 10:
            raise ValueError("reserve must be between 5 and 10 percent")
        if not math.isfinite(voltage) or not 3.3 <= voltage <= 3.6:
            raise ValueError("voltage floor must be between 3.3 and 3.6 V")
        if consecutive < 2:
            raise ValueError("at least two consecutive readings are required")
        self.percent, self.voltage, self.consecutive = percent, voltage, consecutive
        self.streak = 0

    def evaluate(self, state):
        if (state is None or state.plugged or not state.restore
                or (state.percent > self.percent and state.volts > self.voltage)):
            self.streak = 0
            return False
        self.streak += 1
        return self.streak >= self.consecutive


def request_shutdown(bus, reserve):
    # A fresh reading closes the gap between the threshold sample and poweroff.
    if not reserve.evaluate(read_state(bus)):
        return False
    MARKER.write_text("automatic PiSugar3 reserve shutdown\n", encoding="ascii")
    try:
        subprocess.run(["/usr/bin/systemctl", "poweroff"], check=True)
    except BaseException:
        MARKER.unlink(missing_ok=True)
        raise
    return True


def watch(bus, args):
    reserve = Reserve(args.reserve_percent, args.voltage_floor, args.consecutive)
    if not math.isfinite(args.interval_s) or args.interval_s <= 0:
        raise ValueError("interval must be finite and positive")
    version, _ = identify(bus)
    print(f"PiSugar3 {version}: reserve={reserve.percent}%, floor={reserve.voltage:.2f} V, "
          f"{reserve.consecutive} readings {args.interval_s:g}s apart; dry_run={args.dry_run}",
          flush=True)
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while running:
        try:
            state = read_state(bus)
            print(f"{state.volts:.3f} V {state.percent}% external={state.plugged} "
                  f"restore={state.restore}", flush=True)
            if reserve.evaluate(state):
                print("LOW BATTERY: graceful reserve shutdown", flush=True)
                if args.dry_run:
                    reserve.streak = 0
                elif request_shutdown(bus, reserve):
                    return 0
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            reserve.evaluate(None)
            print(f"unusable reading / shutdown failed: {exc}", flush=True)
        deadline = time.monotonic() + args.interval_s
        while running and time.monotonic() < deadline:
            time.sleep(max(0, min(1, deadline - time.monotonic())))
    return 0


def late_shutdown(bus, action):
    if action not in ("poweroff", "halt"):
        return 0  # Never cut output on reboot or kexec.
    if (os.geteuid() != 0
            or Path("/proc/1/exe").resolve().name != "systemd-shutdown"
            or not os.statvfs("/").f_flag & os.ST_RDONLY):
        raise RuntimeError("output cut requires late systemd shutdown and read-only root")
    # A transient bus error must not turn an otherwise clean shutdown into a
    # permanently halted node. Bound retries well inside systemd's hook limit.
    for attempt in range(5):
        try:
            return finish_poweroff(bus)
        except OSError:
            if attempt == 4:
                raise
            time.sleep(0.2)
    return 2  # Unreachable; keeps the terminal result explicit.


def finish_poweroff(bus):
    _, state = identify(bus)
    if state.plugged and MARKER.exists():
        # Vehicle input returned while the OS was shutting down. Reboot from
        # this final phase instead of leaving a halted Pi on a live supply.
        subprocess.run(["/usr/bin/systemctl", "reboot", "--force", "--force"], check=True)
        return 0
    ensure_restore(bus)
    os.sync()
    # Set delay to zero explicitly: bit 5 is the vendor driver's output cut,
    # and newer firmware also uses it to trigger the delay at register 0x09.
    bus.protected_write(0x09, 0)
    control = bus.read(CTR1)
    if control & PLUGGED and MARKER.exists():
        subprocess.run(["/usr/bin/systemctl", "reboot", "--force", "--force"], check=True)
        return 0
    bus.protected_write(CTR1, (control | RESTORE) & ~OUTPUT)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "ensure-restore", "watch",
                                           "poweroff", "halt", "reboot", "kexec"))
    parser.add_argument("--reserve-percent", type=int, default=10)
    parser.add_argument("--voltage-floor", type=float, default=3.40)
    parser.add_argument("--consecutive", type=int, default=3)
    parser.add_argument("--interval-s", type=float, default=30)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.action in ("reboot", "kexec"):
        return 0
    bus = None
    try:
        bus = I2C()
        if args.action == "check":
            version, state = identify(bus)
            print(f"PiSugar3 {version}: {state.volts:.3f} V {state.percent}% "
                  f"external={state.plugged} restore={state.restore} "
                  f"CTR1=0x{state.control:02X}")
        elif args.action == "ensure-restore":
            if os.geteuid() != 0:
                raise PermissionError("ensure-restore requires root")
            ensure_restore(bus)
        elif args.action == "watch":
            if not args.dry_run and os.geteuid() != 0:
                raise PermissionError("watch requires root unless --dry-run is set")
            return watch(bus, args)
        else:
            return late_shutdown(bus, args.action)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"PiSugar3 power: {exc}", flush=True)
        return 2
    finally:
        if bus is not None:
            bus.close()


if __name__ == "__main__":
    raise SystemExit(main())
