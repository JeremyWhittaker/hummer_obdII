"""Review a raw probe transcript: pair every request with its answer.

Read-only and offline: it opens the JSONL transcript, never the serial port.
The VIN is masked in the output, so the review can be pasted into a report.

    PYTHONPATH=src python3 scripts/review_raw_log.py logs/raw/probe-*.jsonl
"""

from __future__ import annotations

import sys

from hummer_obd.decode import (
    decode_ascii_item,
    decode_dtcs,
    decode_pid,
    decode_vin,
    mask_vin,
    parse_reply,
    supported_pids,
)
from hummer_obd.rawlog import decode_record, iter_records
from hummer_obd.safety import is_safe


def review(path: str) -> int:
    pairs: list[tuple[str, bytes]] = []
    pending = None
    events = corrupt = 0
    for record in iter_records(path):
        kind = record.get("kind")
        if kind == "corrupt":
            corrupt += 1
            continue
        if kind == "event":
            events += 1
            continue
        data = decode_record(record)  # verifies hex == base64
        if record["dir"] == "tx":
            pending = data.decode("ascii", "replace").strip()
        else:
            pairs.append((pending or "?", data))

    print(f"### {path}")
    print(f"    {len(pairs)} request/response pairs, {events} events, {corrupt} corrupt lines")

    print("\n### every command that reached the port, in order")
    print("   ", " ".join(cmd for cmd, _ in pairs))

    print("\n### adapter identity and link")
    for want in ("ATI", "AT@1", "AT@2", "STI", "STDI", "ATRV", "ATDP", "ATDPN"):
        for cmd, raw in pairs:
            if cmd == want:
                print(f"  {want:6s} -> {' / '.join(parse_reply(raw).lines)}")
                break

    print("\n### protocol negotiation and PID support")
    for cmd, raw in pairs:
        if cmd in ("0100", "0120", "0140", "0160", "0180", "01A0", "01C0"):
            reply = parse_reply(raw)
            pids = supported_pids(reply, cmd[2:4]) if reply.ok else []
            print(f"  {cmd} [{reply.status}] {len(pids)} pids: {' '.join(pids) if pids else '-'}")

    print("\n### current data")
    for cmd, raw in pairs:
        if cmd.startswith("01") and len(cmd) == 4 and cmd[2:4] not in ("00", "20", "40", "60", "80", "A0", "C0"):
            value = decode_pid(cmd[2:4], parse_reply(raw))
            print(f"  {cmd} {value.name}: {value.value} {value.unit} [{value.status}]")

    print("\n### diagnostic trouble codes (read-only services)")
    for cmd, raw in pairs:
        if cmd in ("03", "07", "0A"):
            reply = parse_reply(raw)
            codes = decode_dtcs(cmd, reply) or "(none)"
            print(f"  mode {cmd} [{reply.status}] codes={codes} raw={' / '.join(reply.lines)}")

    print("\n### vehicle information (service 09)")
    for cmd, raw in pairs:
        if cmd.startswith("09"):
            reply = parse_reply(raw)
            if cmd == "0902":
                print(f"  {cmd} [{reply.status}] VIN {mask_vin(decode_vin(reply))}")
            else:
                text = decode_ascii_item(reply, int(cmd[2:4], 16))
                label = {"04": "calibration ID", "0A": "ECU name"}.get(cmd[2:4], "item")
                print(f"  {cmd} [{reply.status}] {label}: {text!r}"
                      f" ({len(reply.frames)} responding ECU frame(s))")

    print("\n### NO DATA / error responses")
    misses = [(cmd, parse_reply(raw).marker or parse_reply(raw).status)
              for cmd, raw in pairs if parse_reply(raw).status in ("no_data", "error")]
    print("   ", misses if misses else "none - every request was answered")

    print("\n### safety audit of the whole transcript")
    unsafe = [cmd for cmd, _ in pairs if not is_safe(cmd)]
    print("    commands that would fail the read-only gate:", unsafe or "none")
    forbidden = [cmd for cmd, _ in pairs
                 if cmd[:2] in ("04", "08", "2E", "27", "2F", "31", "22")]
    print("    clear/actuate/UDS-write/enhanced requests present:", forbidden or "none")
    return 1 if (unsafe or forbidden or corrupt) else 0


if __name__ == "__main__":
    sys.exit(max(review(p) for p in sys.argv[1:]) if len(sys.argv) > 1 else 2)
