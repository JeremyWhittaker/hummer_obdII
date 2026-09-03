"""Ask every module what it supports, using the standard's own discovery calls.

This project refuses to sweep identifiers, and that refusal is right: guessing
vendor identifiers puts unsourced requests on a live vehicle's bus and calls the
result research. But refusing to guess is not the same as refusing to ask, and
SAE J1979 defines a mechanism for asking. Service 01 PID `00` returns a bitmap
of which PIDs the responder supports, `20` points at the next bank, and so on;
service 09 and service 06 advertise themselves the same way.

Those are not guesses. They are the questions the standard exists to answer, and
a module's reply is authoritative about itself in a way no external source can
be. Everything read here was named by the vehicle first.

What is new is *per-module*. The project enumerated supported PIDs once, through
the functional broadcast, which returns whatever the first or loudest responder
says. But this vehicle names eight modules, they do not support the same things,
and the difference is invisible to a broadcast: a PID that only the brake
controller answers looks identical to one nothing answers, and a PID answered by
four modules looks like a PID answered by one. Addressing each module in turn
and asking it what it supports produces a map the broadcast cannot.

The bank walk only advances when a bitmap actually points at the next bank. That
matters on a live bus: asking for all seven banks unconditionally would put six
pointless requests on the wire and make a module supporting one bank
indistinguishable from one that timed out six times.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .decode import (
    parse_reply,
    supported_mids,
    supported_pids,
    supported_service09_pids,
)
from .safety import validate_command
from .session import SUPPORT_MIDS_06, SUPPORT_PIDS_01
from .transport import Transport, TransportError

__all__ = ["MODULES", "ModuleReport", "census", "format_census", "main"]

#: The eight addresses this vehicle named for itself, answering service 09
#: PID 0A behind their own receive filters.  Nothing here was chosen from a
#: list of GM addresses found elsewhere: the truck supplied every one.
MODULES: tuple[tuple[str, str], ...] = (
    ("17", "DMCM-DriveMotorCtrl"),
    ("1D", "DMC2-DriveMotorCtrl2"),
    ("1E", "DMC3-DriveMotorCtrl3"),
    ("28", "BSCM-BrakeSystem"),
    ("40", "BCM-BodyControl"),
    ("45", "Gateway Module - GWM"),
    ("CB", "BSM-BatterySysMngr"),
    ("CD", "BSM-BatterySysMngr (second)"),
)

#: Legislated OBD runs at priority 0x18, not the 0x14 the enhanced reads use.
_PRIORITY = "ATCP18"

#: Bitmap PIDs point at the next bank; they are not readings, and reading them
#: back as data would report a bitmap as a measurement.
_BITMAP_PIDS = frozenset({"00", "20", "40", "60", "80", "A0", "C0"})


@dataclass
class ModuleReport:
    """What one module said about itself."""

    address: str
    name: str
    service01: list[str] = field(default_factory=list)
    service09: list[str] = field(default_factory=list)
    service06: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    answered: bool = False

    @property
    def readable_pids(self) -> list[str]:
        """Supported service 01 PIDs that are data rather than bitmap pointers."""
        return [p for p in self.service01 if p not in _BITMAP_PIDS]


def _address_commands(address: str) -> tuple[str, ...]:
    """Point the adapter at one module for legislated services."""
    return (
        _PRIORITY,
        f"ATSHDA{address}F1",
        f"ATCRA18DAF1{address}",
    )


def _walk(transport: Transport, commands: tuple[str, ...], decoder,
          timeout: float, say) -> list[str]:
    """Walk a bank chain, stopping when a bitmap does not point onward."""
    found: list[str] = []
    for command in commands:
        base = command[2:4]
        try:
            reply = parse_reply(
                transport.send(validate_command(command), timeout=timeout).data
            )
        except TransportError as exc:
            say(f"    {command}: {exc}")
            break
        items = decoder(reply, base)
        say(f"    {command} -> {len(items)} advertised")
        if not items:
            break
        found.extend(items)
        next_bank = f"{int(base, 16) + 0x20:02X}"
        if next_bank not in items:
            break
    return sorted(set(found))


def census(transport: Transport, *, modules=MODULES, timeout: float = 8.0,
           say=lambda m: None) -> list[ModuleReport]:
    """Ask each module, in turn, what it supports.

    Only support bitmaps are sent.  Nothing here reads a value, so a module
    that advertises nothing costs exactly one request.
    """
    reports: list[ModuleReport] = []
    for address, name in modules:
        say(f"\n-- {address}  {name}")
        report = ModuleReport(address=address, name=name)
        try:
            for command in _address_commands(address):
                transport.send(validate_command(command), timeout=timeout)
        except TransportError as exc:
            report.errors.append(f"addressing: {exc}")
            reports.append(report)
            say(f"    could not address: {exc}")
            continue

        report.service01 = _walk(transport, SUPPORT_PIDS_01, supported_pids,
                                 timeout, say)
        try:
            reply = parse_reply(
                transport.send(validate_command("0900"), timeout=timeout).data
            )
            report.service09 = supported_service09_pids(reply, "00")
            say(f"    0900 -> {len(report.service09)} advertised")
        except TransportError as exc:
            report.errors.append(f"0900: {exc}")
        report.service06 = _walk(transport, SUPPORT_MIDS_06, supported_mids,
                                 timeout, say)

        report.answered = bool(report.service01 or report.service09
                               or report.service06)
        reports.append(report)
    return reports


def format_census(reports: list[ModuleReport]) -> str:
    """The census as a table, silences included."""
    out: list[str] = []
    out.append("=" * 78)
    out.append("PER-MODULE SUPPORT CENSUS -- what each module says it supports")
    out.append("=" * 78)
    out.append("")
    out.append(f"  {'addr':<5} {'module':<30} {'svc01':>6} {'svc09':>6} {'svc06':>6}")
    for r in reports:
        mark = "" if r.answered else "   (silent)"
        out.append(
            f"  {r.address:<5} {r.name:<30} "
            f"{len(r.readable_pids):>6} {len(r.service09):>6} "
            f"{len(r.service06):>6}{mark}"
        )
    out.append("")
    for r in reports:
        if not r.answered:
            continue
        out.append(f"-- {r.address} {r.name} " + "-" * max(0, 60 - len(r.name)))
        if r.readable_pids:
            out.append(f"   service 01: {' '.join(r.readable_pids)}")
        if r.service09:
            out.append(f"   service 09: {' '.join(r.service09)}")
        if r.service06:
            out.append(f"   service 06: {' '.join(r.service06)}")
        if r.errors:
            out.append(f"   errors: {'; '.join(r.errors[:3])}")
        out.append("")

    silent = [r for r in reports if not r.answered]
    if silent:
        out.append(f"silent to legislated discovery: "
                   f"{', '.join(r.address for r in silent)}")
    # The comparison the broadcast could never make.
    everyone = [set(r.readable_pids) for r in reports if r.answered]
    if len(everyone) > 1:
        shared = set.intersection(*everyone)
        union = set.union(*everyone)
        out.append(f"PIDs every answering module supports: "
                   f"{' '.join(sorted(shared)) or '(none)'}")
        out.append(f"PIDs supported by at least one module: {len(union)}")
        for r in reports:
            if not r.answered:
                continue
            unique = set(r.readable_pids) - set.union(
                *[set(o.readable_pids) for o in reports
                  if o.answered and o.address != r.address]
            )
            if unique:
                out.append(f"  only {r.address} supports: {' '.join(sorted(unique))}")
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ask each module what it supports, using only the "
                    "legislated support bitmaps. Sends no vendor identifier "
                    "and guesses nothing."
    )
    parser.add_argument("--device", default="/dev/rfcomm0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--output", help="write the census JSON here")
    parser.add_argument(
        "--confirm", action="store_true",
        help="required: this opens the serial device and transmits",
    )
    args = parser.parse_args(argv)

    if not args.confirm:
        print("DRY RUN - nothing is transmitted and no serial device is opened.")
        print(f"would address {len(MODULES)} modules and ask each for its")
        print("service 01, 09 and 06 support bitmaps. Re-run with --confirm.")
        return 0

    # Imported here so a dry run needs no serial library at all.
    from .rawlog import RawLog
    from .transport import SerialTransport

    raw = RawLog("logs/discover-raw.jsonl", session_id="discover")
    transport = SerialTransport(
        args.device, raw, baudrate=args.baud, validator=validate_command,
    )
    started = datetime.now(timezone.utc).isoformat()
    try:
        transport.open()
        for command in ("ATZ", "ATE0", "ATL0", "ATS0", "ATH1", "ATAL", "ATSP7"):
            transport.send(validate_command(command), timeout=args.timeout)
        reports = census(transport, timeout=args.timeout, say=lambda m: print(m, flush=True))
    except TransportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    finally:
        transport.close()

    print()
    print(format_census(reports))

    if args.output:
        payload = {
            "started_utc": started,
            "modules": [
                {
                    "address": r.address, "name": r.name,
                    "service01": r.service01, "service09": r.service09,
                    "service06": r.service06, "errors": r.errors,
                    "answered": r.answered,
                }
                for r in reports
            ],
        }
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        print(f"\ncensus written to {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
