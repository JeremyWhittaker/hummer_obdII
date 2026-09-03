"""A text view of every sensor this node can collect, and whether it is answering.

Run it while the recorder is running.  It shows one line per column: the value,
the unit, and -- the reason this exists -- **how long since that sensor last
answered**.  A sensor that has gone quiet looks completely different from one
that is reporting zero, which is the distinction that matters when something is
wrong and the difference the CSV alone cannot show you.

    hummer-obd-live                 # one snapshot
    hummer-obd-live --watch         # refresh until you stop it

This never opens the serial device.  It reads the session CSV that
``hummer_obd.drive`` is already writing, for two reasons: two processes on one
RFCOMM device would corrupt both streams, and reading the recorder's own output
shows what the recorder is *actually getting* rather than what a second,
differently-configured connection would get.  So it is safe to run at any time,
including while driving, and it adds exactly zero traffic to the vehicle.

The map of which module supplies which column is derived from
``drive.GROUPS`` and ``drive.DECODERS`` at import, not written out by hand.
This file therefore cannot drift from the recorder the way the prose
inventories in the README and the unit defaults both did.
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import time
from typing import Optional

from . import drive
from .analyze import read_session

__all__ = ["column_sources", "snapshot", "render", "main"]

#: Presentation only: a human label and a unit for each column.  A column
#: missing from here still displays, just without the niceties -- the set of
#: columns itself always comes from ``drive.COLUMNS``.
LABELS: dict[str, tuple[str, str]] = {
    "utc": ("sample timestamp", ""),
    "elapsed_s": ("seconds into session", "s"),
    "volts": ("12 V rail (adapter)", "V"),
    "speed_kph": ("vehicle speed", "km/h"),
    "odometer_km": ("odometer", "km"),
    "soc_pct": ("state of charge", "%"),
    "energy_kwh": ("energy remaining", "kWh"),
    "range_mi": ("estimated range", "mi"),
    "dist_since_chg_mi": ("distance since charge", "mi"),
    "temp_f": ("battery temperature", "F"),
    "charger_5401_raw": ("charger 0x5401 (raw)", ""),
    "power_kw": ("power, energy slope", "kW"),
    "cell_avg_v": ("cell voltage, average", "V"),
    "cell_min_v": ("cell voltage, lowest", "V"),
    "cell_max_v": ("cell voltage, highest", "V"),
    "cell_spread_mv": ("cell spread", "mV"),
    "pack_v": ("pack voltage", "V"),
    "pack_a": ("pack current", "A"),
    "hv_power_kw": ("power, volts x amps", "kW"),
    "dmc2_v": ("drive motor 12 V", "V"),
    "wheel_fl_kph": ("wheel speed, front left", "km/h"),
    "wheel_fr_kph": ("wheel speed, front right", "km/h"),
    "wheel_rl_kph": ("wheel speed, rear left", "km/h"),
    "wheel_rr_kph": ("wheel speed, rear right", "km/h"),
    "brake_kpa": ("brake pressure", "kPa"),
    "steering_deg": ("steering angle", "deg"),
    "lateral_g": ("lateral acceleration", "g"),
    "longitudinal_g": ("longitudinal acceleration", "g"),
    "array_2b43": ("per-cell array 0x2B43 (raw)", ""),
}

#: Friendly names for the modules the recorder addresses.
MODULE_NAMES: dict[str, str] = {
    "battery": "battery manager",
    "chassis": "brake / chassis controller",
    "pack_power": "pack power",
    "drive_motor": "drive motor controller",
}

#: Columns the recorder computes rather than reads from any module.
DERIVED: dict[str, str] = {
    "power_kw": "energy slope",
    "hv_power_kw": "V x A",
}

#: Columns that belong to the row itself rather than to the vehicle.
BOOKKEEPING = ("utc", "elapsed_s")


def _decoder_columns(did: str) -> tuple[str, ...]:
    """Which CSV columns a decoder produces, asked of the decoder itself.

    Feeding it a run of zero bytes is enough to learn the *shape* of its
    output, which is all this needs.  Deriving the map beats writing it down:
    every hand-maintained inventory in this project has drifted from the code
    at least once, and this one would be read while something was already
    going wrong.
    """
    decoder = drive.DECODERS.get(did)
    if decoder is None:
        return ()
    for width in (32, 16, 8, 4, 2, 1):
        try:
            result = decoder(bytes(width))
        except Exception:  # a guard rejecting this length is not an error here
            continue
        if result:
            return tuple(result)
    return ()


def column_sources() -> dict[str, tuple[str, str]]:
    """Column -> (which module supplies it, which identifier carries it).

    Grouped by module rather than by identifier on purpose: when something is
    wrong it is usually a whole module that has stopped answering, and that
    reads as one silent block instead of scattered dashes.
    """
    sources: dict[str, tuple[str, str]] = {}
    for name in BOOKKEEPING:
        sources[name] = ("recorder bookkeeping", "")
    sources["volts"] = ("adapter -- ATRV, never reaches the CAN bus", "ATRV")
    for request, column in drive.STANDARD_PIDS:
        sources[column] = ("standard OBD, broadcast to every module", request)
    for group in drive.GROUPS:
        # 'ATSHDACBF1' -> the module byte is characters 6:8 ("CB").
        module = group.address[0][6:8]
        friendly = MODULE_NAMES.get(group.name, group.name)
        for did in group.dids:
            for column in _decoder_columns(did):
                sources[column] = (f"{friendly} (module {module})", f"0x{did}")
    for column, how in DERIVED.items():
        sources[column] = ("computed by the recorder, not read from a module", how)
    return sources


def snapshot(rows: list[dict]) -> dict:
    """The newest value of every column, and how stale each one is.

    A column is reported from the most recent row that actually carried it, so
    a sensor that answered a minute ago still shows its last reading rather
    than a blank -- with the age beside it, so a stale number is never mistaken
    for a live one.
    """
    if not rows:
        return {"rows": 0, "columns": {}}
    newest = rows[-1]
    now = newest.get("elapsed_s")
    columns: dict[str, dict] = {}
    for name in drive.COLUMNS:
        value = None
        age = None
        seen = 0
        for row in reversed(rows):
            candidate = row.get(name)
            if candidate is not None and candidate != "":
                seen += 1
                if value is None:
                    value = candidate
                    stamp = row.get("elapsed_s")
                    if isinstance(now, (int, float)) and isinstance(stamp, (int, float)):
                        age = now - stamp
        columns[name] = {
            "value": value,
            "age_s": age,
            "samples": seen,
            "of": len(rows),
        }
    periods = [
        b - a
        for a, b in zip(
            [r.get("elapsed_s") for r in rows], [r.get("elapsed_s") for r in rows[1:]]
        )
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b > a
    ]
    return {
        "rows": len(rows),
        "newest_utc": newest.get("utc"),
        "elapsed_s": now,
        "period_s": round(sum(periods) / len(periods), 2) if periods else None,
        "columns": columns,
    }


def _format_value(name: str, value) -> str:
    if value is None:
        return "--"
    if isinstance(value, float):
        text = f"{value:g}"
    else:
        text = str(value)
    if len(text) > 22:
        text = text[:19] + "..."
    return text


def _format_age(age: Optional[float], period: Optional[float]) -> str:
    """Age, and a word for whether that age is normal."""
    if age is None:
        return "        --"
    if age < 0.05:
        return "      live"
    if age < 60:
        text = f"{age:.0f}s ago"
    else:
        text = f"{int(age // 60)}m{int(age % 60):02d}s ago"
    return f"{text:>10}"


def render(snap: dict, *, path: str = "", sources: Optional[dict] = None,
           stale_after: float = 30.0) -> str:
    """The whole node's sensor state as one page of text."""
    sources = sources if sources is not None else column_sources()
    out: list[str] = []
    out.append("=" * 78)
    out.append("HUMMER TELEMETRY -- what this node can collect, and what is answering")
    out.append("=" * 78)
    if not snap.get("rows"):
        out.append("")
        out.append(f"  no samples yet in {path or '(no session file)'}")
        out.append("  the recorder writes a row only while the vehicle is awake.")
        return "\n".join(out)

    period = snap.get("period_s")
    out.append(f"session : {os.path.basename(path) if path else '(unknown)'}")
    out.append(f"rows    : {snap['rows']}    newest: {snap.get('newest_utc')}")
    out.append(f"sampling: every {period}s" if period else "sampling: unknown")
    out.append("")

    # Group columns by the module that supplies them, so a whole module going
    # quiet is visible as a block rather than as scattered dashes.
    grouped: dict[str, list[str]] = {}
    for name in drive.COLUMNS:
        grouped.setdefault(sources.get(name, ("unknown", ""))[0], []).append(name)

    quiet: list[str] = []
    for source, names in grouped.items():
        out.append(f"-- {source} " + "-" * max(0, 75 - len(source)))
        for name in names:
            info = snap["columns"][name]
            label, unit = LABELS.get(name, (name, ""))
            detail = sources.get(name, ("", ""))[1]
            value = _format_value(name, info["value"])
            if unit and info["value"] is not None:
                value = f"{value} {unit}"
            age = _format_age(info["age_s"], period)
            seen = f"{info['samples']}/{info['of']}"
            flag = ""
            if info["value"] is None:
                flag = "  NEVER ANSWERED"
                quiet.append(name)
            elif info["age_s"] is not None and info["age_s"] > stale_after:
                flag = "  STALE"
                quiet.append(name)
            out.append(
                f"  {label:<28} {detail:<8} {value:>15} {age} {seen:>7}{flag}"
            )
        out.append("")

    out.append("=" * 78)
    if quiet:
        out.append(f"NOT ANSWERING ({len(quiet)}): {', '.join(quiet)}")
    else:
        out.append("every column is answering")
    return "\n".join(out)


def newest_session(directory: str) -> Optional[str]:
    found = sorted(glob.glob(os.path.join(directory, "drive-*.csv")))
    return found[-1] if found else None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Live text view of every sensor this node collects. "
                    "Never opens the serial device: it reads the session the "
                    "recorder is already writing, so it is safe to run at any "
                    "time and adds no traffic to the vehicle."
    )
    parser.add_argument(
        "--dir", default="evidence/sessions",
        help="where session CSVs live (default: evidence/sessions)",
    )
    parser.add_argument("--session", default=None, help="a specific session file")
    parser.add_argument(
        "--watch", action="store_true",
        help="keep refreshing instead of printing once",
    )
    parser.add_argument(
        "--interval", type=float, default=3.0,
        help="seconds between refreshes in --watch (default: 3)",
    )
    parser.add_argument(
        "--window", type=int, default=200,
        help="how many recent rows to consider when looking for a last value "
             "(default: 200)",
    )
    parser.add_argument(
        "--stale-after", type=float, default=30.0,
        help="flag a column STALE once its last value is this old (default: 30)",
    )
    args = parser.parse_args(argv)

    def once() -> int:
        path = args.session or newest_session(args.dir)
        if not path:
            print(f"no drive-*.csv in {args.dir}", file=sys.stderr)
            return 2
        try:
            rows, _warnings, _header = read_session(path)
        except OSError as exc:
            print(f"cannot read {path}: {exc}", file=sys.stderr)
            return 2
        print(render(snapshot(rows[-args.window:]), path=path))
        return 0

    if not args.watch:
        return once()

    # Re-resolve the newest session every pass: the recorder starts a new file
    # each time the vehicle wakes, and a watch pinned to the old one would sit
    # there looking healthy and increasingly stale.
    try:
        while True:
            if sys.stdout.isatty():
                print("\033[H\033[J", end="")
            code = once()
            if code != 0:
                print("(waiting for a session to appear)")
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
