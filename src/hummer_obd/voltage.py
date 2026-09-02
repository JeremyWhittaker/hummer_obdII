"""Zero-CAN-traffic 12 V watch.

The only thing standing between this project and continuous collection is a
physical question: with the Pi and the adapter permanently powered from an
always-live OBD-II port, does the vehicle still sleep, and what does that do to
the 12 V battery?

``ATRV`` answers the second half without touching the first.  It reads the
voltage on pin 16 of the J1962 connector inside the adapter.  It needs no
protocol, no ECU and no bus arbitration, so it can be sampled while the vehicle
is asleep, locked and under observation without putting a single byte on the
CAN bus.  ``ATCS`` adds the adapter's CAN error counters, which is how a silent
bus is told apart from a broken one.

The safety property this module exists to guarantee is narrower than the rest
of the project's: not merely "read-only", but **"nothing reaches the vehicle at
all"**.  :data:`WATCH_COMMANDS` is fixed, every entry is asserted to be an
adapter command at import time, and there is no way to add a vehicle service
to it from the command line.
"""

from __future__ import annotations

import argparse
import csv
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Optional

from .config import load_config
from .decode import parse_reply
from .rawlog import RawLog
from .safety import UnsafeCommandError, validate_command
from .transport import SerialTransport, TransportError

__all__ = ["WATCH_COMMANDS", "VoltageWatch", "assert_no_vehicle_traffic", "main"]

#: Reset, quiet the echo, read connector voltage, read CAN error counters.
#: Every one of these acts on the OBDLink and nothing else.
WATCH_COMMANDS: Final[tuple[str, ...]] = ("ATZ", "ATE0", "ATRV", "ATCS")

#: Commands that reach the vehicle start with a hex service byte.  An adapter
#: command starts with ``AT`` or ``ST``.  That is the whole distinction, and it
#: is checked rather than assumed.
_ADAPTER_PREFIXES: Final[tuple[str, ...]] = ("AT", "ST")


def assert_no_vehicle_traffic(commands=WATCH_COMMANDS) -> tuple[str, ...]:
    """Return *commands* only if none of them can reach the vehicle.

    Runs at import time so a future edit that slips ``0100`` into the watch
    fails immediately and loudly, rather than quietly transmitting onto a
    sleeping bus during an overnight observation.
    """
    checked = []
    for command in commands:
        safe = validate_command(command)  # the normal read-only gate
        if not safe.startswith(_ADAPTER_PREFIXES):
            raise UnsafeCommandError(
                f"refused {command!r}: the voltage watch may only send adapter "
                f"commands, and {safe} is a vehicle service request"
            )
        checked.append(safe)
    return tuple(checked)


# Checked once, here, so importing the module is the assertion.
WATCH_COMMANDS = assert_no_vehicle_traffic(WATCH_COMMANDS)

CSV_FIELDS: Final[tuple[str, ...]] = ("ts_utc", "volts", "can_status", "status")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _volts(text: str) -> Optional[float]:
    """Parse an ``ATRV`` reply such as ``13.9V``.

    A reply that is not a voltage is reported as missing rather than coerced:
    an invented 0.0 V in a battery trend would look like a dead battery.
    """
    cleaned = text.strip().upper().rstrip("V").strip()
    try:
        return float(cleaned)
    except (TypeError, ValueError):
        return None


class VoltageWatch:
    def __init__(self, cfg, *, output: Path, interval_s: float = 300.0,
                 duration_s: float = 0.0, logger=print) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be greater than zero")
        if duration_s < 0:
            raise ValueError("duration_s must not be negative")
        self.cfg = cfg
        self.output = Path(output)
        self.interval_s = interval_s
        self.duration_s = duration_s
        self.log = logger
        self.running = True
        self.samples = 0

    def stop(self, *_args) -> None:
        self.running = False

    def _sleep(self, seconds: float, deadline: Optional[float]) -> None:
        """Sliced wait, so a stop request is noticed inside a 5 minute gap."""
        end = time.monotonic() + max(0.0, seconds)
        while self.running:
            now = time.monotonic()
            remaining = end - now
            if deadline is not None:
                remaining = min(remaining, deadline - now)
            if remaining <= 0:
                return
            time.sleep(min(1.0, remaining))

    def _append(self, row: dict) -> None:
        new = not self.output.exists()
        self.output.parent.mkdir(parents=True, exist_ok=True)
        with open(self.output, "a", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
            if new:
                writer.writeheader()
            writer.writerow(row)
            fh.flush()

    def sample_once(self, transport) -> dict:
        """One pass over the adapter commands.  Never touches the vehicle."""
        row = {"ts_utc": _now(), "volts": "", "can_status": "", "status": "ok"}
        for command in WATCH_COMMANDS:
            response = transport.send(command, timeout=6.0)
            reply = parse_reply(response.data)
            text = " / ".join(reply.lines)
            if command == "ATRV":
                volts = _volts(reply.lines[-1] if reply.lines else "")
                row["volts"] = "" if volts is None else f"{volts:g}"
                if volts is None:
                    row["status"] = "no_voltage"
            elif command == "ATCS":
                row["can_status"] = text
        return row

    def run(self) -> int:
        raw_path = (Path(self.cfg.path(self.cfg.collector.raw_log_dir))
                    / f"voltage-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl")
        deadline = time.monotonic() + self.duration_s if self.duration_s else None
        self.log(f"voltage watch: every {self.interval_s:g}s, "
                 f"{'no time limit' if deadline is None else f'for {self.duration_s:g}s'}, "
                 f"commands {' '.join(WATCH_COMMANDS)} (no vehicle traffic)")
        with RawLog(raw_path, "voltage-watch", meta={"role": "voltage_watch"}) as rawlog:
            transport = SerialTransport(
                self.cfg.adapter.device,
                rawlog,
                baudrate=self.cfg.adapter.baudrate,
                read_timeout_s=self.cfg.adapter.read_timeout_s,
                command_timeout_s=self.cfg.adapter.command_timeout_s,
                sleeper=lambda seconds: self._sleep(seconds, deadline),
            )
            while self.running:
                try:
                    if not transport.is_open:
                        transport.open()
                    row = self.sample_once(transport)
                except TransportError as exc:
                    # The adapter being unreachable is itself a data point: it
                    # is what a powered-down connector looks like.
                    row = {"ts_utc": _now(), "volts": "", "can_status": "",
                           "status": f"unreachable: {exc}"[:120]}
                    transport.close()
                self._append(row)
                self.samples += 1
                self.log(f"  {row['ts_utc']} {row['volts'] or '-'}V "
                         f"{row['can_status'] or '-'} [{row['status']}]")
                if deadline is not None and time.monotonic() >= deadline:
                    break
                self._sleep(self.interval_s, deadline)
            transport.close()
        self.log(f"voltage watch finished after {self.samples} samples")
        return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Watch 12 V connector voltage without transmitting to the vehicle")
    parser.add_argument("--config", help="path to hummer.toml")
    parser.add_argument("--root", default=".", help="project root for relative paths")
    parser.add_argument("--device", help="serial device (default: config adapter.device)")
    parser.add_argument("--output", default="evidence/voltage-watch.csv",
                        help="CSV to append to")
    parser.add_argument("--interval-s", type=float, default=300.0)
    parser.add_argument("--duration-s", type=float, default=0.0,
                        help="stop after this many seconds; 0 means no limit")
    args = parser.parse_args(argv)
    cfg = load_config(args.config, root=args.root) if args.config else load_config(root=args.root)
    if args.device:
        cfg.adapter.device = args.device
    watch = VoltageWatch(cfg, output=cfg.path(args.output),
                         interval_s=args.interval_s, duration_s=args.duration_s)
    signal.signal(signal.SIGTERM, watch.stop)
    signal.signal(signal.SIGINT, watch.stop)
    try:
        return watch.run()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
