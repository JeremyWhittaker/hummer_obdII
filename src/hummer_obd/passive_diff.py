"""Compare two passive captures and say what differs, if anything.

`hummer-obd-passive` records whatever arrives at the diagnostic connector while
transmitting nothing but adapter configuration. A single capture answers "is
anything being said". This answers the more interesting question: **does what is
being said change when something happens to the vehicle?**

The method is one physical event per capture. Take a baseline with the vehicle
awake and nothing happening, then take another while exactly one thing occurs --
a door opens, the fob locks, the climate system starts, a charge begins -- and
compare. A frame identifier that appears in one and not the other, or whose
payload bytes move, is a lead about what the gateway forwards unsolicited.

Three things this deliberately does not do, and the reasons are not decorative:

**It never opens the serial device.** It reads transcripts that already exist. A
tool that both captures and compares is a tool that will eventually be pointed at
a vehicle to "just re-run the baseline".

**It never suggests replaying anything.** Observing that some identifier changes
when the doors lock is not evidence that sending it would lock the doors, and
this project does not transmit frames it did not source. Modern CAN security can
include freshness counters, sequence numbers and message authentication; a byte
pattern is not an instruction.

**It treats an empty capture as a result, not an error.** On 2026-09-04 a
thirty-second capture at this connector returned **zero bytes**
(`docs/VALIDATION.md`), so the expected outcome of comparing two captures here is
that there is nothing to compare -- and saying that clearly is more useful than a
report full of zeroes. This tool exists so that the day something does arrive,
the comparison is already written and does not have to be improvised.

The adapter's monitor output is ASCII over Bluetooth at 115200 baud, which caps
at a few hundred frames per second against a bus carrying thousands. **Frame
counts here are not bus load, and an identifier absent from a capture may simply
have been dropped.** Absence is weak evidence; presence is strong.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .rawlog import decode_record, iter_records

__all__ = [
    "Capture",
    "read_capture",
    "parse_frames",
    "compare",
    "format_report",
    "main",
]

#: A monitor line is a CAN identifier followed by payload bytes.  With ``ATH1``
#: the adapter prefixes each frame with its identifier; 3 hex digits for an
#: 11-bit ID, 8 for a 29-bit one, and this vehicle uses 29-bit.  Anything that
#: does not match is kept as an unparsed line rather than discarded, because a
#: line the parser does not understand is a fact about the capture.
_FRAME = re.compile(r"^([0-9A-F]{3}|[0-9A-F]{8})((?:[0-9A-F]{2})*)$")


@dataclass
class Capture:
    """One passive capture, reduced to what can be compared."""

    path: str
    label: str = ""
    #: Bytes the tool transmitted.  Always adapter configuration; recorded so a
    #: reader can confirm that for themselves rather than taking it on trust.
    tx_bytes: int = 0
    #: Bytes that arrived *during the monitor stream*.  Deliberately not "every
    #: rx byte between capture_start and capture_end": `monitor.py` logs the
    #: adapter's own reply to the stop character inside that window, and
    #: counting those ten bytes of prompt made a genuinely empty capture read as
    #: non-empty -- found by running this against the real 2026-09-04 capture.
    rx_bytes: int = 0
    #: Bytes already waiting when the capture began.  The monitor records them
    #: rather than discarding them, and they are unsolicited traffic, but they
    #: belong to the interval *before* this capture and are counted apart.
    residue_bytes: int = 0
    #: Bytes the adapter sent back after being told to stop -- its prompt.  Not
    #: vehicle traffic.
    drain_bytes: int = 0
    elapsed_s: Optional[float] = None
    stop_reason: str = ""
    can_counters: dict = field(default_factory=dict)
    #: identifier -> payloads seen, in order
    frames: dict = field(default_factory=dict)
    unparsed: list = field(default_factory=list)

    @property
    def empty(self) -> bool:
        """No vehicle traffic arrived during the stream.

        Keyed on both bytes and parsed frames: a capture can carry bytes that
        yield no frame (a truncated line, an adapter status word), and calling
        that "not empty" would overstate it.
        """
        return self.rx_bytes == 0 and not self.frames

    @property
    def frame_count(self) -> int:
        return sum(len(v) for v in self.frames.values())


def parse_frames(data: bytes) -> tuple[dict, list]:
    """Split monitor output into ``{identifier: [payload hex, ...]}``.

    Tolerant on purpose. The adapter interleaves prompts, blank lines and
    occasional status words with frames, and a strict parser would throw away
    exactly the anomalies worth noticing.
    """
    frames: dict = {}
    unparsed: list = []
    text = data.decode("ascii", "replace")
    for chunk in re.split(r"[\r\n]+", text):
        line = chunk.replace(" ", "").replace("\t", "").strip().upper()
        if not line or line in (">", "OK"):
            continue
        match = _FRAME.match(line)
        if match and match.group(2):
            frames.setdefault(match.group(1), []).append(match.group(2))
        else:
            unparsed.append(line[:64])
    return frames, unparsed


def read_capture(path: str) -> Capture:
    """Reduce a raw transcript to a :class:`Capture`.

    Only the bytes received *between* ``capture_start`` and ``capture_end`` are
    counted. Setup replies (``OK`` to ``ATE0`` and friends) are received bytes
    too, and folding them in would make every capture look non-empty.
    """
    cap = Capture(path=path)
    inside = False
    payload = bytearray()
    for record in iter_records(path):
        kind = record.get("kind")
        if kind == "event":
            event, data = record.get("event"), record.get("payload") or {}
            if event == "session_start":
                cap.label = (data.get("meta") or {}).get("label", "")
            elif event == "capture_start":
                inside = True
            elif event == "capture_end":
                inside = False
                cap.elapsed_s = data.get("elapsed_s")
                cap.stop_reason = data.get("stop_reason", "")
            continue
        if kind != "io":
            continue
        raw = decode_record(record)
        note = record.get("note", "")
        if record.get("dir") == "tx":
            cap.tx_bytes += len(raw)
        elif note.startswith("capture "):
            # The stream itself.  `monitor.flush()` writes exactly this note,
            # so selecting on it is precise where "anything inside the window"
            # is not.
            cap.rx_bytes += len(raw)
            payload += raw
        elif note == "pre-capture residue":
            cap.residue_bytes += len(raw)
        elif note == "post-stop drain":
            cap.drain_bytes += len(raw)
        elif inside:
            # Inside the window, note we do not recognise. Count it and say so
            # rather than silently choosing a side.
            cap.rx_bytes += len(raw)
            payload += raw
        else:
            text = raw.decode("ascii", "replace")
            if "T:" in text and "R:" in text:
                key = "after" if cap.can_counters.get("before") else "before"
                cap.can_counters[key] = text.strip()
    cap.frames, cap.unparsed = parse_frames(bytes(payload))
    return cap


def _changed_positions(payloads: Iterable[str]) -> list[int]:
    """Byte positions that are not identical across every payload."""
    seen = list(payloads)
    if len(seen) < 2:
        return []
    width = min(len(p) for p in seen) // 2
    return [i for i in range(width)
            if len({p[i * 2:i * 2 + 2] for p in seen}) > 1]


def compare(baseline: Capture, event: Capture) -> dict:
    """What differs between two captures."""
    ids = sorted(set(baseline.frames) | set(event.frames))
    rows = []
    for identifier in ids:
        b = baseline.frames.get(identifier, [])
        e = event.frames.get(identifier, [])
        rows.append({
            "id": identifier,
            "baseline_count": len(b),
            "event_count": len(e),
            "baseline_unique": len(set(b)),
            "event_unique": len(set(e)),
            "only_in": ("event" if not b else "baseline" if not e else ""),
            "new_payloads": sorted(set(e) - set(b))[:8],
            "changed_bytes": _changed_positions(b + e),
        })
    return {
        "baseline": {"path": baseline.path, "label": baseline.label,
                     "rx_bytes": baseline.rx_bytes, "frames": baseline.frame_count,
                     "elapsed_s": baseline.elapsed_s,
                     "residue_bytes": baseline.residue_bytes,
                     "drain_bytes": baseline.drain_bytes},
        "event": {"path": event.path, "label": event.label,
                  "rx_bytes": event.rx_bytes, "frames": event.frame_count,
                  "elapsed_s": event.elapsed_s,
                  "residue_bytes": event.residue_bytes,
                  "drain_bytes": event.drain_bytes},
        "both_empty": baseline.empty and event.empty,
        "identifiers": rows,
    }


def format_report(result: dict) -> str:
    b, e = result["baseline"], result["event"]
    out = ["=" * 78,
           "PASSIVE DIFF -- what changed at the connector between two captures",
           "=" * 78, "",
           f"  baseline  {b['label'] or '(no label)'}",
           f"            {b['rx_bytes']} bytes, {b['frames']} frames, {b['elapsed_s']}s",
           f"  event     {e['label'] or '(no label)'}",
           f"            {e['rx_bytes']} bytes, {e['frames']} frames, {e['elapsed_s']}s",
           ""]

    if result["both_empty"]:
        out += ["Both captures are empty. There is nothing to compare, and that is",
                "the expected result at this connector: a 30-second capture on",
                "2026-09-04 returned zero bytes, and the gateway forwards nothing",
                "unsolicited to pins 6 and 14.",
                "",
                "This is a measurement, not a failure. What it does NOT establish:",
                "that the vehicle's internal networks are quiet (they are not, behind",
                "the gateway), or that no state would ever produce traffic -- only",
                "that these two did not.",
                "=" * 78]
        return "\n".join(out)

    appeared = [r for r in result["identifiers"] if r["only_in"] == "event"]
    vanished = [r for r in result["identifiers"] if r["only_in"] == "baseline"]
    moved = [r for r in result["identifiers"]
             if not r["only_in"] and (r["changed_bytes"] or r["new_payloads"])]

    if appeared:
        out += [f"-- identifiers present ONLY during the event ({len(appeared)}) " + "-" * 22]
        for r in appeared:
            out.append(f"     {r['id']:<10} {r['event_count']:>5} frames, "
                       f"{r['event_unique']} unique payloads")
        out.append("")
    if vanished:
        out += [f"-- identifiers that stopped during the event ({len(vanished)}) " + "-" * 18]
        for r in vanished:
            out.append(f"     {r['id']:<10} {r['baseline_count']:>5} frames in baseline")
        out.append("")
    if moved:
        out += [f"-- identifiers whose payload moved ({len(moved)}) " + "-" * 28]
        for r in moved[:20]:
            out.append(f"     {r['id']:<10} bytes {r['changed_bytes']}  "
                       f"new payloads: {', '.join(r['new_payloads'][:3]) or 'none'}")
        out.append("")
    if not (appeared or vanished or moved):
        if not (b["frames"] or e["frames"]):
            out += ["Neither capture yielded a parsable frame, though bytes "
                    "arrived. Look at the transcripts directly:",
                    "  PYTHONPATH=src python3 scripts/review_raw_log.py <path>", ""]
        else:
            out += ["Both captures carry frames and nothing distinguishes them.", ""]

    out += ["=" * 78,
            "A frame that changes when something happens is a LEAD, not a command.",
            "This project does not replay frames. Modern CAN security can include",
            "freshness counters, sequence numbers and message authentication, so a",
            "byte pattern is not an instruction -- and observing a correlation is a",
            "long way from establishing a cause.",
            "",
            "Counts here are not bus load: ASCII over Bluetooth at 115200 caps at a",
            "few hundred frames per second. An absent identifier may simply have",
            "been dropped. Absence is weak evidence; presence is strong."]
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare two passive captures. Offline: never opens the "
                    "serial device and never transmits."
    )
    parser.add_argument("baseline", help="raw transcript of the baseline capture")
    parser.add_argument("event", help="raw transcript of the event capture")
    parser.add_argument("--json", dest="json_path", help="write the comparison here")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    try:
        baseline = read_capture(args.baseline)
        event = read_capture(args.event)
    except OSError as exc:
        print(f"cannot read: {exc}", file=sys.stderr)
        return 2

    result = compare(baseline, event)
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
    if not args.quiet:
        print(format_report(result))
    # A comparison that found nothing is still a successful comparison.
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
