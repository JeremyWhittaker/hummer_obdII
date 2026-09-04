"""Find what an undecoded field is, by correlating it against what is known.

This project stores anything whose meaning is unproven as raw hex, and by
2026-09-03 that was seventeen columns: twenty-six per-module values in
``0x2B43``, twenty-four in ``0x2AF1``, nine single fields from module 40, four
more from the battery manager, and the four bytes ``0x2AF5`` returns beyond the
three cell voltages anyone had read.

The findings that decided what several of those are -- the +0.995 tracking of
``0x2B43`` against state of charge, the -0.81 between ``0x5401`` and pack
current -- were computed in a shell and survive only as prose in code comments.
Nothing in this repository could re-derive them. For a project whose stated
standard is measurement over plausible interpretation, that is the wrong way
round: the numbers should come from something a reader can run.

So this runs them. For every raw column it extracts every plausible field --
single bytes, big-endian 16-bit pairs signed and unsigned, 24-bit windows --
and correlates each against every quantity the vehicle reports directly.

Three things it does deliberately, each learned by getting them wrong first:

**It filters transitional rows and says how many it dropped.** Sleep and wake
edges produce a ``pack_v`` of 1.0 V and zeros across module-40 fields. The first
correlation run against module 40 gave +0.55 against pack voltage, which was
almost entirely those rows; filtered, the real figure was different and the
conclusion changed.

**It reports the range of the thing being correlated against.** Every
temperature correlation in the corpus rests on 5.4 degrees Fahrenheit of spread.
A correlation quoted without its span invites a reader to believe a scaling has
been established when what has been established is a direction.

**It reports constant fields rather than skipping them.** ``hv_temp_raw`` held
at 70 across 264 samples; two of ``0x2AF5``'s four unknown bytes held through a
97.8 kW pull that sagged the pack by 3 V. A field that does not move across that
range is not measuring it, and that is a finding -- arguably a stronger one than
a middling correlation.

Nothing here opens the serial device or touches the vehicle.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Callable, Optional

from .analyze import array_values, correlate, field_windows, read_session

__all__ = ["SANITY_FILTERS", "collect", "rank", "format_findings", "main"]

#: Rows that fail one of these are dropped before anything is correlated.
#:
#: A vehicle waking or going to sleep reports values that are not measurements
#: of anything: a pack voltage of 1.0 V, zeros across fields that were answering
#: a second earlier.  Correlating through those transitions produced a +0.55
#: that was pure artefact, so the filter is not a nicety -- it is the difference
#: between a finding and a mistake.  Each is stated as "what a plausible reading
#: looks like", never as a range chosen to make a number come out.
SANITY_FILTERS: dict[str, Callable[[float], bool]] = {
    # The pack is nominally ~400 V; anything under 300 is a module answering
    # mid-transition rather than a pack that has actually sagged that far.
    "pack_v": lambda v: v >= 300.0,
    # A plausible battery temperature in Fahrenheit.
    "temp_f": lambda v: -40.0 <= v <= 200.0,
    # State of charge is a percentage.
    "soc_pct": lambda v: 0.0 < v <= 100.0,
}

#: Columns worth correlating *against*: things this vehicle reports directly and
#: whose decoding is already cross-validated.
_TARGETS: tuple[str, ...] = (
    "soc_pct", "energy_kwh", "temp_f", "pack_v", "pack_a", "hv_power_kw",
    "cell_avg_v", "cell_min_v", "cell_max_v", "cell_spread_mv",
    "speed_kph", "range_mi", "dist_since_chg_mi",
)


def _numeric(row: dict, key: str) -> Optional[float]:
    value = row.get(key)
    return value if isinstance(value, (int, float)) else None


def sane(row: dict) -> bool:
    """Whether a row looks like measurements rather than a transition."""
    for key, ok in SANITY_FILTERS.items():
        value = _numeric(row, key)
        if value is not None and not ok(value):
            return False
    return True


def collect(rows: list[dict], columns: list[str]) -> dict:
    """Every candidate field's time series, paired with the targets beside it.

    Each column gets its **own** row subset, and the targets are collected from
    exactly those rows.  Building the two independently looks simpler and is
    wrong: a session recorded before a column existed still contributes target
    values, the two series then differ in length, and every pairing is silently
    skipped.  That produced an empty report against a corpus that visibly
    contains the relationship, which is the worst kind of failure -- a confident
    "nothing here" from a tool that never compared anything.
    """
    kept = [r for r in rows if sane(r)]
    per_column: dict[str, dict] = {}
    for column in columns:
        paired = [
            (row, values)
            for row, values in ((r, array_values(r.get(column))) for r in kept)
            if values is not None
        ]
        if not paired:
            continue
        fields: dict[str, list[float]] = {}
        for _row, values in paired:
            for window, (value,) in field_windows(values).items():
                fields.setdefault(window, []).append(value)
        targets: dict[str, list[float]] = {}
        for name in _TARGETS:
            series = [_numeric(row, name) for row, _ in paired]
            if any(v is not None for v in series):
                targets[name] = [
                    v if v is not None else float("nan") for v in series
                ]
        per_column[column] = {
            "rows": len(paired), "fields": fields, "targets": targets,
        }
    return {
        "rows_total": len(rows),
        "rows_kept": len(kept),
        "rows_dropped": len(rows) - len(kept),
        "columns": per_column,
    }


def rank(collected: dict, *, minimum: float = 0.5) -> list[dict]:
    """Every field, with its strongest correlations and its own behaviour."""
    findings: list[dict] = []
    for column, data in sorted(collected["columns"].items()):
        for window, series in sorted(data["fields"].items()):
            distinct = len(set(series))
            entry: dict = {
                "field": f"{column}/{window}",
                "samples": len(series),
                "min": min(series),
                "max": max(series),
                "distinct": distinct,
                "constant": distinct == 1,
                "correlations": [],
            }
            if distinct > 1:
                for target, values in data["targets"].items():
                    pairs = [
                        (f, t) for f, t in zip(series, values) if t == t
                    ]
                    if len(pairs) < 3:
                        continue
                    r = correlate([p[0] for p in pairs], [p[1] for p in pairs])
                    if r is None or abs(r) < minimum:
                        continue
                    span = max(p[1] for p in pairs) - min(p[1] for p in pairs)
                    entry["correlations"].append({
                        "target": target,
                        "r": round(r, 4),
                        "samples": len(pairs),
                        # Without this a reader cannot tell a relationship
                        # measured across a real range from one measured across
                        # nothing at all.
                        "target_span": round(span, 4),
                    })
                entry["correlations"].sort(key=lambda c: -abs(c["r"]))
            findings.append(entry)
    return findings


def format_findings(collected: dict, findings: list[dict], *,
                    minimum: float = 0.5) -> str:
    out: list[str] = []
    out.append("=" * 78)
    out.append("FIELD DECODE -- candidate fields against what the vehicle reports")
    out.append("=" * 78)
    out.append("")
    out.append(f"rows: {collected['rows_kept']} used, "
               f"{collected['rows_dropped']} dropped as transitional "
               f"(of {collected['rows_total']})")
    out.append(f"showing correlations at |r| >= {minimum}")
    out.append("")

    constant = [f for f in findings if f["constant"]]
    moving = [f for f in findings if not f["constant"]]

    ranked = [f for f in moving if f["correlations"]]
    ranked.sort(key=lambda f: -abs(f["correlations"][0]["r"]))
    if ranked:
        out.append("-- fields that track something " + "-" * 46)
        for f in ranked:
            out.append(f"  {f['field']:<24} {f['min']:>7} .. {f['max']:<8} "
                       f"{f['distinct']:>4} distinct")
            for c in f["correlations"][:3]:
                out.append(f"      r={c['r']:+.3f} vs {c['target']:<18} "
                           f"n={c['samples']:<5} target spanned {c['target_span']}")
        out.append("")

    quiet = [f for f in moving if not f["correlations"]]
    if quiet:
        out.append(f"-- moving, but matching nothing measured ({len(quiet)}) " + "-" * 24)
        out.append("     " + ", ".join(f["field"] for f in quiet[:14]))
        if len(quiet) > 14:
            out.append(f"     ... and {len(quiet) - 14} more")
        out.append("")

    if constant:
        out.append(f"-- constant across every sample ({len(constant)}) " + "-" * 30)
        out.append("   A field that does not move while the vehicle does is not")
        out.append("   measuring the vehicle. That is a finding, not a gap.")
        for f in constant[:20]:
            out.append(f"     {f['field']:<24} held at {f['min']}")
        if len(constant) > 20:
            out.append(f"     ... and {len(constant) - 20} more")
        out.append("")

    out.append("=" * 78)
    out.append("A correlation is a direction, not a scaling. Read the target span")
    out.append("beside each one: a strong r across a narrow span says very little.")
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Correlate undecoded raw fields against measured quantities. "
                    "Offline: never opens the serial device."
    )
    parser.add_argument("sessions", nargs="*", help="session CSVs to read")
    parser.add_argument("--dir", default=None,
                        help="read every drive-*.csv in this directory")
    parser.add_argument("--column", action="append", default=None,
                        help="restrict to one raw column (repeatable)")
    parser.add_argument("--minimum", type=float, default=0.5,
                        help="report correlations at or above this |r| (default 0.5)")
    parser.add_argument("--json", dest="json_path", help="write findings here")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if not args.sessions and not args.dir:
        parser.error("give session files or --dir")
    paths = list(args.sessions)
    if args.dir:
        paths.extend(sorted(glob.glob(os.path.join(args.dir, "drive-*.csv"))))
    if not paths:
        # A directory that holds no sessions is a different thing from being
        # called with no arguments, and gets analyze.py's treatment rather than
        # argparse's: say so on stderr and return, do not exit from inside a
        # library function.
        print(f"no drive-*.csv found in {args.dir}", file=sys.stderr)
        return 2

    rows: list[dict] = []
    for path in paths:
        try:
            found, _warnings, _header = read_session(path)
        except OSError as exc:
            print(f"cannot read {path}: {exc}", file=sys.stderr)
            return 2
        rows.extend(found)
    if not rows:
        print("no rows in the given sessions", file=sys.stderr)
        return 2

    # Any column carrying hex is a candidate; the recorder decides which those
    # are, so nothing here needs a hand-maintained list to fall behind.
    columns = args.column or sorted(
        {c for r in rows for c in r if array_values(r.get(c)) is not None}
    )
    collected = collect(rows, columns)
    findings = rank(collected, minimum=args.minimum)

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump({"rows": {k: collected[k] for k in
                                ("rows_total", "rows_kept", "rows_dropped")},
                       "findings": findings}, handle, indent=2, sort_keys=True)
    if not args.quiet:
        print(format_findings(collected, findings, minimum=args.minimum))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
