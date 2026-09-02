"""Read-only raw probe.

This is the first thing that ever talks to the vehicle.  It runs a fixed,
short, read-only conversation and writes every byte to an append-only raw log,
then prints a summary that is safe to paste into a report (the VIN is masked).

Nothing here is allowed to write to the vehicle: every command goes through
:func:`hummer_obd.safety.validate_command` inside the transport.

Usage::

    python3 -m hummer_obd.probe --device /dev/rfcomm0
    python3 -m hummer_obd.probe --replay logs/raw/probe-*.jsonl   # offline review
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config
from .decode import PID_DECODERS, decode_cvns, decode_vin, mask_vin, parse_reply
from .rawlog import RawLog
from .safety import UnsafeCommandError, validate_all
from .session import AdapterSession
from .storage import Storage
from .transport import SerialTransport, TransportError

#: Fallback service 01 PIDs, used only when the vehicle will not answer its own
#: support bitmap.  When the bitmap *is* readable the probe asks for what the
#: vehicle actually advertises instead, which is the only way to find readings
#: a generic list does not contain.
PROBE_PIDS = ["05", "0C", "0D", "11", "1F", "2F", "42", "46", "5B", "5C"]

#: Support-bitmap PIDs point at the next bank; they are not readings.
SUPPORT_BITMAP_PIDS = frozenset({"00", "20", "40", "60", "80", "A0", "C0"})

#: Fallback service 09 items: 02 VIN, 04 calibration ID, 0A ECU name.
PROBE_SERVICE09 = ["02", "04", "0A"]


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_probe(args) -> int:
    cfg = load_config(args.config, root=args.root) if args.config else load_config(root=args.root)
    device = args.device or cfg.adapter.device
    session_uid = f"probe-{_stamp()}"
    raw_dir = cfg.path(cfg.collector.raw_log_dir)
    raw_path = Path(raw_dir) / f"{session_uid}.jsonl"

    print(f"# session {session_uid}")
    print(f"# device  {device}")
    print(f"# rawlog  {raw_path}")

    summary: dict = {"session": session_uid, "device": device, "raw_log": str(raw_path)}
    with RawLog(raw_path, session_uid, meta={"device": device, "probe": True}) as rawlog:
        transport = SerialTransport(
            device,
            rawlog,
            baudrate=cfg.adapter.baudrate,
            read_timeout_s=cfg.adapter.read_timeout_s,
            command_timeout_s=cfg.adapter.command_timeout_s,
        )
        try:
            transport.open()
        except TransportError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

        session = AdapterSession(transport, logger=lambda m: print(f"  {m}"))
        try:
            print("## adapter fingerprint")
            fp = session.initialize()
            print("## protocol negotiation")
            session.negotiate_protocol(timeout=args.protocol_timeout)
            summary["adapter"] = {
                "ATI": fp.adapter_id,
                "AT@1": fp.device_description,
                "AT@2": fp.device_identifier,
                "STI": fp.stn_version,
                "STDI": fp.stn_device_id,
                "ATRV": fp.voltage,
                "protocol": fp.protocol,
                "protocol_number": fp.protocol_number,
            }

            print("## supported service 01 PIDs")
            supported = session.supported_service01_pids()
            summary["supported_pids"] = supported
            print(f"  {len(supported)} PIDs: {' '.join(supported) if supported else '(none)'}")

            # Ask for exactly what this vehicle advertises.  A fixed generic
            # list silently skips readings the vehicle does have (this truck
            # advertises an odometer) while wasting bus traffic on readings it
            # does not.  The bitmap pointers are excluded: they are not data.
            if supported:
                targets = [p for p in supported if p not in SUPPORT_BITMAP_PIDS]
                skipped = [p for p in PROBE_PIDS if p not in supported]
            else:
                targets = list(PROBE_PIDS)
                skipped = []
            summary["probed_pids"] = targets

            print("## current data")
            samples = {}
            for pid in skipped:
                # Record the "vehicle does not have this" answer in the same
                # shape as a reading, so the absence is evidence too.
                meta = PID_DECODERS.get(pid, {})
                samples[pid] = {
                    "name": meta.get("name", f"PID {pid}"),
                    "value": None,
                    "unit": meta.get("unit", ""),
                    "status": "not_supported",
                    "raw": "",
                }
                print(f"  {pid} {meta.get('name', '')}: not advertised by the vehicle")
            for pid in targets:
                value, reply = session.read_pid(pid)
                samples[pid] = {
                    "name": value.name,
                    "value": value.value,
                    "unit": value.unit,
                    "status": value.status,
                    "raw": value.raw_hex,
                }
                print(f"  {pid} {value.name}: {value.value} {value.unit} [{value.status}]")
            summary["samples"] = samples

            print("## diagnostic trouble codes (read-only)")
            dtcs = {}
            for mode in ("03", "07", "0A"):
                codes, reply = session.read_dtcs(mode)
                dtcs[mode] = {"codes": codes, "status": reply.status, "lines": reply.lines}
                print(f"  mode {mode}: {codes if codes else '(none)'} [{reply.status}]")
            summary["dtcs"] = dtcs

            print("## vehicle information (service 09)")
            vin, vin_reply = session.read_vin()
            summary["vin_masked"] = mask_vin(vin)
            summary["vin_status"] = vin_reply.status
            print(f"  VIN: {mask_vin(vin)} [{vin_reply.status}]")
            advertised09 = session.supported_service09_items()
            summary["supported_service09"] = advertised09
            # 02 is handled above and stays masked; never read it through the
            # generic loop, which would put a VIN in the summary.
            items09 = [p for p in (advertised09 or PROBE_SERVICE09)
                       if p not in ("00", "02")]
            info = {}
            for pid in items09:
                text, reply = session.read_service09_item(pid)
                if pid == "06":
                    # CVNs are four-byte binary check values.  Rendering them
                    # as ASCII produces noise that reads like a decode failure
                    # when the reply was in fact perfectly good.
                    value = decode_cvns(reply)
                else:
                    value = text
                info[pid] = {"value": value, "status": reply.status}
                print(f"  09{pid}: {value!r} [{reply.status}]")
            summary["service09"] = info
        finally:
            transport.close()

    if args.summary:
        Path(args.summary).parent.mkdir(parents=True, exist_ok=True)
        Path(args.summary).write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        print(f"# summary written to {args.summary}")

    if args.database:
        with Storage(cfg.path(args.database)) as store:
            sid = store.start_session(
                session_uid,
                adapter_id=summary.get("adapter", {}).get("ATI", ""),
                protocol=summary.get("adapter", {}).get("protocol", ""),
                raw_log_path=str(raw_path),
                notes="raw read-only probe",
            )
            store.add_vehicle_info(sid, "VIN", summary.get("vin_masked", ""), "see raw log")
            store.end_session(sid)
    return 0


def run_command_set(args) -> int:
    """Run exactly the operator-supplied, prevalidated read-only commands.

    The entire set is validated before the serial device is opened. This makes
    the operation all-or-nothing: one unsafe or malformed entry prevents every
    transmission. Raw TX/RX bytes are still recorded by :class:`SerialTransport`.
    """
    try:
        commands = validate_all(args.commands)
    except UnsafeCommandError as exc:
        print(f"REFUSED before opening serial transport: {exc}", file=sys.stderr)
        return 2

    cfg = load_config(args.config, root=args.root) if args.config else load_config(root=args.root)
    device = args.device or cfg.adapter.device
    session_uid = f"command-probe-{_stamp()}"
    raw_dir = cfg.path(cfg.collector.raw_log_dir)
    raw_path = Path(raw_dir) / f"{session_uid}.jsonl"
    summary: dict = {
        "session": session_uid,
        "device": device,
        "raw_log": str(raw_path),
        "commands": [],
    }

    print(f"# session {session_uid}")
    print(f"# device  {device}")
    print(f"# rawlog  {raw_path}")

    with RawLog(raw_path, session_uid, meta={"device": device, "command_probe": True}) as rawlog:
        transport = SerialTransport(
            device,
            rawlog,
            baudrate=cfg.adapter.baudrate,
            read_timeout_s=cfg.adapter.read_timeout_s,
            command_timeout_s=cfg.adapter.command_timeout_s,
        )
        try:
            transport.open()
            for command in commands:
                timeout = args.protocol_timeout if not command.startswith(("AT", "ST")) else 6.0
                response = transport.send(command, timeout=timeout)
                reply = parse_reply(response.data)
                item = {
                    "command": command,
                    "status": reply.status,
                    "timed_out": response.timed_out,
                    "elapsed_s": round(response.elapsed_s, 3),
                }
                if command == "0902":
                    item["vin_masked"] = mask_vin(decode_vin(reply))
                    display = f"VIN {item['vin_masked']}"
                else:
                    item["lines"] = reply.lines
                    display = " / ".join(reply.lines) or "(empty)"
                summary["commands"].append(item)
                print(f"  {command:6s} [{reply.status}] {display}")
        except TransportError as exc:
            summary["error"] = str(exc)
            print(f"ERROR: {exc}", file=sys.stderr)
            return_code = 2
        else:
            return_code = 0
        finally:
            transport.close()

    if args.summary:
        Path(args.summary).parent.mkdir(parents=True, exist_ok=True)
        Path(args.summary).write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        print(f"# summary written to {args.summary}")
    return return_code


def replay(paths) -> int:
    """Summarise an existing raw log without touching hardware."""
    from .rawlog import decode_record, iter_records

    for path in paths:
        print(f"# {path}")
        tx = rx = corrupt = 0
        for record in iter_records(path):
            kind = record.get("kind")
            if kind == "corrupt":
                corrupt += 1
                print(f"  !! corrupt line {record.get('lineno')}")
                continue
            if kind == "event":
                print(f"  [{record['seq']:>4}] event {record['event']} {record.get('payload', {})}")
                continue
            data = decode_record(record)
            direction = record["dir"]
            tx += direction == "tx"
            rx += direction == "rx"
            print(f"  [{record['seq']:>4}] {direction} {len(data):>4}B {record['display']}")
        print(f"# {tx} tx, {rx} rx, {corrupt} corrupt")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only OBD raw probe")
    parser.add_argument("--device", help="serial device (default: config adapter.device)")
    parser.add_argument("--config", help="path to hummer.toml")
    parser.add_argument("--root", default=".", help="project root for relative paths")
    parser.add_argument("--summary", help="write a JSON summary here (VIN masked)")
    parser.add_argument("--database", help="also record the session in this SQLite database")
    parser.add_argument("--protocol-timeout", type=float, default=20.0)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--replay", nargs="+", help="summarise existing raw logs and exit")
    mode.add_argument(
        "--commands", nargs="+", metavar="COMMAND",
        help="run exactly this prevalidated read-only command set and exit",
    )
    args = parser.parse_args(argv)
    if args.replay:
        return replay(args.replay)
    if args.commands:
        return run_command_set(args)
    return run_probe(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
