"""Which recorded field responded when somebody did something to the vehicle.

The recorder samples fifty-three columns every eight seconds or so, and
seventeen of them are raw payload bytes whose meaning is not claimed. Correlating
those against the vehicle's *other* numbers can only show that two of its outputs
move together. What identifies a field is an **outside intervention**: switch the
climate system on, plug in, open a door, and see what moves.

So the method is a square wave, not a nudge. Hold a state, change it, hold the
new one, change back. Two things follow from that and both are easy to get
wrong:

**Start with the biggest step available.** A cabin setpoint moved one degree may
change nothing measurable; the climate system switched off and back on is a
multi-kilowatt step this recorder cannot miss. Find *which* field responds with
the coarse step, then use fine steps to find its resolution. Doing it the other
way round produces a null result that means nothing.

**Hold each state long enough to have samples.** At an eight-second cycle a
two-minute hold is fifteen samples. This tool refuses to call a difference a
response when either side is thinner than :data:`MIN_SAMPLES`, because with five
samples a difference is a coincidence with a decimal point.

What it reports is *association in time*, and nothing more. A field that moves
when the climate system starts may be measuring compressor current, or cabin
temperature, or the pack heater reacting, or the 12 V load — the experiment
separates it from everything that did **not** move, which is progress, and does
not identify it on its own.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys
from datetime import datetime
from typing import Optional

from .analyze import read_session, sane
from .experiment import DEFAULT_MARKS, load_marks

__all__ = ["segment", "responses", "format_report", "MIN_SAMPLES", "main"]

#: Below this many samples on either side, a difference is not reported as a
#: response.  Eight-second cycle, so this is about two minutes of holding.
MIN_SAMPLES: int = 8

#: Columns that describe the recording rather than the vehicle.  A segment
#: always differs in these, and reporting them would bury the real answers.
_BOOKKEEPING = frozenset({"utc", "elapsed_s"})


def _parse(stamp: str) -> Optional[datetime]:
    if not stamp:
        return None
    text = stamp.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def segment(rows: list[dict], marks: list[dict]) -> list[dict]:
    """Split rows into the intervals between marks.

    Rows before the first mark form an unnamed leading segment; it is kept
    rather than dropped, because "what it was doing before anyone touched it" is
    the baseline every comparison rests on.
    """
    stamped = []
    for row in rows:
        when = _parse(row.get("utc", ""))
        if when is not None:
            stamped.append((when, row))
    stamped.sort(key=lambda pair: pair[0])

    edges = []
    for entry in marks:
        when = _parse(entry.get("utc", ""))
        if when is not None and entry.get("label"):
            edges.append((when, entry["label"]))
    edges.sort(key=lambda pair: pair[0])

    segments: list[dict] = [{"label": "(before the first mark)", "start": None,
                             "rows": []}]
    for when, label in edges:
        segments.append({"label": label, "start": when, "rows": []})

    for when, row in stamped:
        index = 0
        for i, seg in enumerate(segments):
            if seg["start"] is not None and when >= seg["start"]:
                index = i
        segments[index]["rows"].append(row)
    return [s for s in segments if s["rows"]]


def _variance_effect(ratio: float) -> float:
    """A field that went flat-to-variable, or variable-to-flat, scored.

    Expressed on the same scale as the mean-shift effect so the two can be
    compared: a tenfold change in spread reads as roughly 3.
    """
    if ratio <= 0:
        return 0.0
    swing = ratio if ratio >= 1 else 1.0 / ratio
    return min(6.0, (swing - 1.0) ** 0.5)


def _summarise(rows: list[dict], column: str) -> dict:
    values = [r[column] for r in rows if r.get(column) not in (None, "")]
    numeric = [v for v in values if isinstance(v, (int, float))]
    out = {"n": len(values), "values": [str(v) for v in values]}
    if numeric:
        out["mean"] = statistics.mean(numeric)
        out["min"] = min(numeric)
        out["max"] = max(numeric)
        out["sd"] = statistics.pstdev(numeric) if len(numeric) > 1 else 0.0
        out["kind"] = "numeric"
    elif values:
        out["distinct_count"] = len({str(v) for v in values})
        out["kind"] = "text"
    return out


def responses(segments: list[dict], columns: list[str]) -> list[dict]:
    """For each adjacent pair of segments, what differs and by how much."""
    out: list[dict] = []
    for before, after in zip(segments, segments[1:]):
        changes = []
        for column in columns:
            if column in _BOOKKEEPING:
                continue
            a = _summarise(before["rows"], column)
            b = _summarise(after["rows"], column)
            if a["n"] < MIN_SAMPLES or b["n"] < MIN_SAMPLES:
                continue
            if a.get("kind") == "numeric" and b.get("kind") == "numeric":
                delta = b["mean"] - a["mean"]
                # Pooled spread, not the larger of the two.  Dividing by the
                # larger one suppresses exactly the response that matters most:
                # a field that sat at a constant zero and then started swinging
                # has a huge `sd` afterwards, and scaling by it hid `speed_kph`
                # from a report about a drive.  Found by validating this tool
                # against a drive, where the answer was known in advance.
                pooled = ((a["sd"] ** 2 + b["sd"] ** 2) / 2) ** 0.5
                spread = max(pooled, 1e-9)
                # A field that was flat and became variable is a response even
                # when its mean barely moves -- the pack current of a stationary
                # vehicle, say.  Reported alongside rather than folded in.
                became_variable = (b["sd"] + 1e-9) / (a["sd"] + 1e-9)
                changes.append({
                    "column": column, "kind": "numeric",
                    "before": round(a["mean"], 4), "after": round(b["mean"], 4),
                    "delta": round(delta, 4),
                    "sd_before": round(a["sd"], 4), "sd_after": round(b["sd"], 4),
                    "spread_ratio": round(became_variable, 2),
                    "effect": round(max(abs(delta) / spread,
                                        _variance_effect(became_variable)), 2),
                    "n_before": a["n"], "n_after": b["n"],
                })
            elif a.get("kind") == "text" and b.get("kind") == "text":
                # Hex payloads need a different question. "Are there new values"
                # is useless for a field like `array_2b43`, which carries 779
                # distinct values across the corpus -- ANY two windows are nearly
                # disjoint, so every raw column claims a response and drowns out
                # the real ones. It did exactly that on this tool's first run.
                #
                # The useful question is whether the field is *stable within* a
                # segment and *different between* them. A column holding one
                # value throughout, then a different one, is a strong response;
                # one churning through fifty values in each is telling you
                # nothing about your intervention.
                before_set, after_set = set(a["values"]), set(b["values"])
                overlap = len(before_set & after_set) / max(
                    len(before_set | after_set), 1)
                churn = max(len(before_set) / max(a["n"], 1),
                            len(after_set) / max(b["n"], 1))
                stability = max(0.0, 1.0 - churn)
                effect = (1.0 - overlap) * stability * 4.0
                if effect > 0:
                    changes.append({
                        "column": column, "kind": "text",
                        "before": sorted(before_set)[:3],
                        "after": sorted(after_set)[:3],
                        "new_values": sorted(after_set - before_set)[:4],
                        "overlap": round(overlap, 3),
                        "stability": round(stability, 3),
                        "effect": round(effect, 2),
                        "n_before": a["n"], "n_after": b["n"],
                    })
        changes.sort(key=lambda c: -c["effect"])
        out.append({
            "from": before["label"], "to": after["label"],
            "n_before": len(before["rows"]), "n_after": len(after["rows"]),
            "changes": changes,
        })
    return out


def format_report(result: list[dict], *, top: int = 12,
                  minimum: float = 1.0) -> str:
    out = ["=" * 78,
           "RESPONSE -- what moved when the vehicle was interfered with",
           "=" * 78, ""]
    if not result:
        out += ["No marked intervals with rows on both sides.", "",
                "Record marks while the recorder is running:",
                "    hummer-obd-experiment mark \"hvac max cool on\"",
                "and hold each state at least two minutes -- at an eight-second",
                "cycle that is fifteen samples, and fewer is a coincidence with",
                "a decimal point."]
        return "\n".join(out)

    for step in result:
        out.append(f"-- {step['from']}  ->  {step['to']} " + "-" * 20)
        out.append(f"   {step['n_before']} samples before, {step['n_after']} after")
        strong = [c for c in step["changes"] if c["effect"] >= minimum]
        if not strong:
            out += ["   nothing moved by more than the noise it had to stand out",
                    "   from. That is a result: whatever changed, none of these",
                    "   fields is measuring it.", ""]
            continue
        for change in strong[:top]:
            if change["kind"] == "numeric":
                out.append(f"     {change['column']:<22} "
                           f"{change['before']:>12} -> {change['after']:<12} "
                           f"delta {change['delta']:+.4f}  effect {change['effect']:.1f}x")
            else:
                out.append(f"     {change['column']:<22} stable within each "
                           f"segment ({change['stability']:.2f}), "
                           f"{change['overlap']:.0%} overlap  effect "
                           f"{change['effect']:.1f}")
                out.append(f"       {' -> '.join((','.join(change['before'][:2]), ','.join(change['after'][:2])))}")
        if len(strong) > top:
            out.append(f"     ... and {len(strong) - top} more above the threshold")
        out.append("")

    out += ["=" * 78,
            "This is association in time and nothing more. A field that moves",
            "when the climate system starts may be measuring compressor current,",
            "cabin temperature, the pack heater reacting, or the 12 V load. What",
            "the experiment gives you is separation from everything that did NOT",
            "move; identifying it needs a second intervention that changes one of",
            "those and not the others.",
            "",
            "'effect' is the change divided by the noise it had to stand out",
            "from, so a small move on a steady field outranks a large one on a",
            "field that swings anyway."]
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show which recorded columns changed between marked events. "
                    "Offline: opens no serial device."
    )
    parser.add_argument("--dir", default="evidence/sessions")
    parser.add_argument("--marks", default=DEFAULT_MARKS)
    parser.add_argument("--minimum", type=float, default=1.0,
                        help="report changes at or above this effect size")
    parser.add_argument("--json", dest="json_path")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    marks = load_marks(args.marks)
    if not marks:
        print(f"no marks in {args.marks}; nothing to compare", file=sys.stderr)
        return 2

    rows: list[dict] = []
    columns: list[str] = []
    for path in sorted(glob.glob(os.path.join(args.dir, "drive-*.csv"))):
        found, _warnings, header = read_session(path)
        rows.extend(r for r in found if sane(r))
        for name in header:
            if name not in columns:
                columns.append(name)
    if not rows:
        print(f"no session rows in {args.dir}", file=sys.stderr)
        return 2

    result = responses(segment(rows, marks), columns)
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
    if not args.quiet:
        print(format_report(result, minimum=args.minimum))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
