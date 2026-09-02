"""Read-only raw probe.

This is the first thing that ever talks to the vehicle.  It runs a fixed,
short, read-only conversation and writes every byte to an append-only raw log,
then prints a summary that is safe to paste into a report (the VIN is masked).

Nothing here is allowed to write to the vehicle: every command goes through
:func:`hummer_obd.safety.validate_command` inside the transport.

Usage::

    python3 -m hummer_obd.probe --device /dev/rfcomm0
    python3 -m hummer_obd.probe --device /dev/rfcomm0 --max        # ask for everything
    python3 -m hummer_obd.probe --replay logs/raw/probe-*.jsonl   # offline review

``--max`` widens the questions, never the permissions: it adds service 02 and
service 06 (both on the read-only allowlist), keeps every module's answer to a
PID instead of the first, and asks each module for its own name.  It is opt-in
because it costs bus time, and because the quick probe has to stay quick.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config
from .decode import (
    PID_DECODERS,
    decode_cvns,
    decode_pid,
    decode_vin,
    ecu_from_header,
    mask_vin,
    parse_reply,
)
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


def _responding_ecus(reply) -> list[str]:
    """Addresses of the modules that answered, in the order they answered.

    ``headers`` rather than ``frame_headers``: a module whose multi-frame reply
    arrived truncated still spoke, and the question here is who is on the bus,
    not whose bytes survived reassembly.
    """
    return [ecu_from_header(header) for header in reply.headers if header]


def run_probe(args) -> int:
    cfg = load_config(args.config, root=args.root) if args.config else load_config(root=args.root)
    device = args.device or cfg.adapter.device
    session_uid = f"probe-{_stamp()}"
    raw_dir = cfg.path(cfg.collector.raw_log_dir)
    raw_path = Path(raw_dir) / f"{session_uid}.jsonl"

    print(f"# session {session_uid}")
    print(f"# device  {device}")
    print(f"# rawlog  {raw_path}")

    # Opt-in, because the extra questions cost bus time.  Read through getattr so
    # an older caller that builds its own argument object still runs.
    thorough = bool(getattr(args, "max", False))

    summary: dict = {
        "session": session_uid,
        "device": device,
        "raw_log": str(raw_path),
        "max": thorough,
    }
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
            per_ecu: dict[str, list] = {}
            # The decoded objects themselves, kept so --database can store the
            # per-module readings.  Without this, the richest data the probe
            # produces would exist only in a JSON file nobody queries.
            decoded_samples: list = []
            decoded_monitors: list = []
            # Which modules spoke at all.  This is free -- it comes out of
            # replies the probe already asked for -- so it is collected whether
            # or not the extra questions were requested.
            responders: set[str] = set()
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
                if thorough:
                    # The same single request, read for every answer it drew
                    # rather than only the first.
                    values, reply = session.read_pid_per_ecu(pid)
                    per_ecu[pid] = [
                        {
                            "ecu": item.ecu,
                            "name": item.name,
                            "value": item.value,
                            "unit": item.unit,
                            "status": item.status,
                            "raw": item.raw_hex,
                        }
                        for item in values
                    ]
                    # `samples` has to mean the same thing whether or not --max
                    # was given, so it is decoded exactly as read_pid decodes
                    # it: the first module's value, with *every* module's frame
                    # kept as the evidence behind it.  values[0] is the same
                    # number but carries only its own frame, so using it here
                    # would make --max the one mode that narrows the record.
                    # This is a second decode of a reply already in hand -- no
                    # extra bus traffic.
                    value = decode_pid(pid, reply)
                    decoded_samples.extend(values)
                else:
                    value, reply = session.read_pid(pid)
                    decoded_samples.append(value)
                responders.update(_responding_ecus(reply))
                samples[pid] = {
                    "name": value.name,
                    "value": value.value,
                    "unit": value.unit,
                    "status": value.status,
                    "raw": value.raw_hex,
                }
                print(f"  {pid} {value.name}: {value.value} {value.unit} [{value.status}]")
                if thorough and len(per_ecu[pid]) > 1:
                    for item in per_ecu[pid]:
                        print(f"      ecu {item['ecu'] or '?'}: {item['value']} [{item['status']}]")
            summary["samples"] = samples
            if thorough:
                summary["samples_by_ecu"] = per_ecu

            print("## diagnostic trouble codes (read-only)")
            dtcs = {}
            for mode in ("03", "07", "0A"):
                codes, reply = session.read_dtcs(mode)
                responders.update(_responding_ecus(reply))
                dtcs[mode] = {"codes": codes, "status": reply.status, "lines": reply.lines}
                print(f"  mode {mode}: {codes if codes else '(none)'} [{reply.status}]")
            summary["dtcs"] = dtcs
            stored_codes = sorted({code for read in dtcs.values() for code in read["codes"]})

            if thorough:
                print("## on-board monitoring test results (service 06)")
                mids = session.supported_monitor_mids()
                monitors: dict[str, dict] = {}
                for mid in mids:
                    tests, reply = session.read_monitor_tests(mid)
                    monitors[mid] = {
                        "status": reply.status,
                        # Written out field for field as the decoder defines
                        # them, so the summary reports the decode that happened
                        # rather than a hand-picked subset of it.
                        "tests": [asdict(test) for test in tests],
                    }
                    decoded_monitors.extend(tests)
                    print(f"  MID {mid}: {len(tests)} test(s) [{reply.status}]")
                summary["monitors"] = {"supported_mids": mids, "results": monitors}
                if not mids:
                    print("  (the vehicle advertises no monitor IDs)")

                print("## freeze frame (service 02)")
                # One request is always worth making, code or no code: the
                # support bitmap says what a freeze frame *would* hold.  It
                # exercises the whole service 02 path -- request, frame byte,
                # parse -- on a healthy vehicle, which otherwise could never
                # demonstrate the service at all without first developing a
                # fault.  Inducing one to test a decoder is not on the table.
                ff_supported = session.supported_freeze_frame_pids()
                print(f"  020000 supported: "
                      f"{' '.join(ff_supported) if ff_supported else '(none advertised)'}")
                if not stored_codes:
                    # The per-PID reads are a different matter.  A frame only
                    # exists because a code was set, so with no codes every one
                    # of those requests is guaranteed to answer "no data" and
                    # asking would be nothing but bus traffic.
                    reason = ("no stored, pending or permanent DTCs, so no module "
                              "is holding a freeze frame to read")
                    summary["freeze_frames"] = {
                        "skipped": reason,
                        "supported_pids": ff_supported,
                        "frames": {},
                    }
                    print(f"  frame reads skipped: {reason}")
                else:
                    frames = {}
                    for pid in targets:
                        value, reply = session.read_freeze_frame(pid, frame=0)
                        frames[pid] = {
                            "ecu": value.ecu,
                            "name": value.name,
                            "value": value.value,
                            "unit": value.unit,
                            "status": value.status,
                            "raw": value.raw_hex,
                        }
                        print(f"  {pid} frame 0: {value.value} {value.unit} "
                              f"[{value.status}]")
                    summary["freeze_frames"] = {
                        "skipped": "", "supported_pids": ff_supported, "frames": frames}

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

            ecus: dict = {"addresses": sorted(responders), "names": {}}
            if thorough:
                # Last, deliberately: this is the only step that narrows what the
                # adapter will hear, and running it after every other read means
                # a mistake here cannot quietly halve an earlier answer.
                print("## responding modules")
                ecus["names"] = session.ecu_name_map(sorted(responders))
                for address in ecus["addresses"]:
                    print(f"  {address}: {ecus['names'].get(address) or '(unnamed)'}")
            summary["ecus"] = ecus
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
            # Everything the probe actually decoded.  A probe that prints eight
            # per-module voltages and stores none of them is a probe whose best
            # output survives only outside the database.
            if decoded_samples:
                store.add_samples(sid, decoded_samples)
            if decoded_monitors:
                store.add_monitor_tests(sid, decoded_monitors)
            for mode, read in summary.get("dtcs", {}).items():
                store.add_dtc_read(sid, mode, read.get("codes", []), "")
            # The module map is vehicle identity rather than a reading: it says
            # which address is which module, which is what any later per-ECU
            # request has to be addressed by.
            # ``ecus`` is {"addresses": [...], "names": {addr: name}}, so the
            # names map is what carries the per-module identity.  Iterating the
            # outer dict instead would store two rows holding the repr of a
            # list and of a dict, and no row for any actual module.
            ecu_names = summary.get("ecus", {}).get("names", {})
            for address in summary.get("ecus", {}).get("addresses", []):
                store.add_vehicle_info(sid, f"ecu:{address}", ecu_names.get(address, ""), "")
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
    parser.add_argument(
        "--max", action="store_true",
        help="ask for everything the vehicle advertises: service 06 monitor results, "
             "freeze frames when a DTC exists, every module's answer to each PID, "
             "and the module name behind each responding address",
    )
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
