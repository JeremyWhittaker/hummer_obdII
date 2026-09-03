"""Offline analysis of a recorded drive/charge session.

This module never opens a serial device and never touches the vehicle.  It
reads a CSV written by :mod:`hummer_obd.drive` and reports what the session
actually shows: how far, how much energy, how efficiently, how the pack and
cells behaved, and -- first, because it decides how much the rest is worth --
how good the capture itself was.

The capture-quality section exists because a recorder that is running looks
identical to a recorder that is running *well*.  A session can be healthy in
the journal while sampling at half its intended rate, or while missing minutes
to dropped Bluetooth reads.  Those are visible only by measuring the timestamps
the session actually produced, so that is what happens here before any physical
quantity is reported.

Two sign conventions collide in one CSV and are normalised here rather than
averaged away:

``power_kw``
    the slope of ``energy_kwh``, which is energy *remaining*.  Discharging
    makes it fall, so this column is **negative while discharging** and
    positive while charging.

``hv_power_kw``
    ``pack_v * pack_a``, whose sign follows the measured current.  The drive
    recorder documents "negative is charging", so this column is **positive
    while discharging** -- the opposite of the one above.

Both are kept, and their disagreement is reported instead of hidden, because
two independent routes to the same quantity is the check that caught a
mislabelled identifier earlier in this project.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Optional

__all__ = [
    "KM_PER_MILE",
    "read_session",
    "analyze",
    "format_report",
    "main",
]

KM_PER_MILE: float = 1.609344

#: Columns whose values are physical quantities.  Anything not listed is
#: carried through as text (``utc``) or left alone (``array_2b43``, which is an
#: opaque per-cell array rather than a scalar).
_TEXT_COLUMNS = frozenset({"utc", "array_2b43", "charger_5401_raw"})

#: A sample period more than this multiple of the median is treated as a gap
#: rather than as jitter.
_GAP_FACTOR: float = 3.0

#: The four wheel-speed columns.  These come from the brake controller as an
#: enhanced read, which on this vehicle answers far more reliably than the
#: standard ``speed_kph`` PID.
_WHEEL_COLUMNS = ("wheel_fl_kph", "wheel_fr_kph", "wheel_rl_kph", "wheel_rr_kph")

#: Below this speed a sample is counted as stopped rather than moving.  The
#: vehicle reports integer km/h, so anything under 1 is a standstill.
_MOVING_KPH: float = 1.0


def _number(text: Optional[str]) -> Optional[float]:
    """A float from a session cell, or ``None`` if there is no number in it.

    Session cells are not always bare numbers.  The ``volts`` column is written
    straight from the adapter's ``ATRV`` reply and so arrives as ``"13.8V"``
    -- a unit suffix inside an otherwise numeric column, which
    ``float()`` rejects outright.  Rather than special-case one column, any
    trailing unit letters are stripped, because a reading that is present
    should not be discarded over its formatting.
    """
    if text is None:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    # Strip trailing unit characters ("13.8V", "95.0F"), keeping sign,
    # digits, decimal point and exponent.
    while cleaned and cleaned[-1] not in "0123456789.":
        cleaned = cleaned[:-1]
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def read_session(path: str | Path) -> tuple[list[dict], list[str], list[str]]:
    """Rows and column order from a session CSV.

    Returns the rows with numeric columns coerced to floats, the warnings
    raised while reading, and the header as written.  A torn final line -- which is what abrupt power loss mid-row
    looks like -- is dropped with a warning rather than allowed to poison the
    analysis, since the recorder fsyncs each row and a partial row can only
    ever be the last one.
    """
    rows: list[dict] = []
    warnings: list[str] = []
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        header = list(reader.fieldnames or [])
        for raw in reader:
            # A short row means the writer was interrupted partway through it.
            if raw.get("utc") is None or None in raw.values():
                warnings.append(
                    "the last row is incomplete, which is what losing power "
                    "mid-write looks like; it was dropped"
                )
                continue
            row: dict = {}
            for key, value in raw.items():
                if key in _TEXT_COLUMNS:
                    row[key] = (value or "").strip() or None
                else:
                    row[key] = _number(value)
            rows.append(row)
    return rows, warnings, header


def _series(rows: list[dict], key: str) -> list[float]:
    """Every present numeric value for *key*, in order."""
    return [r[key] for r in rows if isinstance(r.get(key), (int, float))]


def _first_last(rows: list[dict], key: str) -> tuple[Optional[float], Optional[float]]:
    present = _series(rows, key)
    if not present:
        return None, None
    return present[0], present[-1]


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _mean(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None:
        return None
    rounded = round(value, digits)
    # Trapezoidal integration of a clamped series can land on -0.0, which
    # reads as a negative quantity in a report about energy direction.
    return 0.0 if rounded == 0 else rounded


def _integrate(rows: list[dict], key: str, *, only_negative=False, only_positive=False) -> float:
    """Trapezoidal integral of *key* against ``elapsed_s``, in unit-hours.

    Used for energy from power and distance from speed.  Sampling here is
    coarse -- a handful of seconds between points -- so this is a total worth
    quoting and not an instantaneous figure worth trusting.  Clamping to one
    sign is how regenerated energy is separated from consumed energy.
    """
    total = 0.0
    previous_t: Optional[float] = None
    previous_v: Optional[float] = None
    for row in rows:
        t = row.get("elapsed_s")
        v = row.get(key)
        if not isinstance(t, (int, float)) or not isinstance(v, (int, float)):
            continue
        if previous_t is not None and t > previous_t:
            a, b = previous_v, v
            if only_negative:
                a, b = min(a, 0.0), min(b, 0.0)
            elif only_positive:
                a, b = max(a, 0.0), max(b, 0.0)
            total += (a + b) / 2.0 * (t - previous_t) / 3600.0
        previous_t, previous_v = t, v
    return total


#: Cells in series, measured as ``pack_v / cell_avg_v`` over 297 samples:
#: mean 95.991, standard deviation 0.041.  Two identifiers on two different
#: modules, so the ratio is not one decoder's artefact.
EXPECTED_SERIES_CELLS: float = 96.0

#: Usable capacity the vehicle works from, as ``energy_kwh / (soc_pct/100)``.
#: Held between 191.84 and 191.94 kWh across an eleven-point swing in charge.
EXPECTED_PACK_KWH: float = 191.9


def _ratio(rows: list[dict], top: str, bottom: str,
           scale: float = 1.0) -> Optional[dict]:
    """Mean and spread of one column divided by another.

    Two independently scaled fields whose ratio holds constant is the strongest
    evidence available here that both scalings are right, because a wrong scale
    on either would make the ratio drift as the underlying quantity moved.
    """
    values = [
        r[top] / (r[bottom] * scale)
        for r in rows
        if isinstance(r.get(top), (int, float))
        and isinstance(r.get(bottom), (int, float))
        and r[bottom]
    ]
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    sd = variance ** 0.5
    return {
        "mean": round(mean, 4),
        "sd": round(sd, 4),
        "samples": len(values),
        "cv_pct": round(100 * sd / mean, 3) if mean else None,
    }


def _cross_checks(rows: list[dict]) -> dict:
    """Relationships between columns that should hold whatever the vehicle did.

    Every one of these divides one decoded field by another decoded field, so
    they check the *decoders* rather than the drive.  If a scaling silently
    changes -- a byte offset moves, a divisor is edited, a source is
    re-interpreted -- these numbers move away from figures that have already
    been measured across a thousand samples, and the session says so instead of
    quietly reporting wrong physics.
    """
    checks: dict = {}
    series = _ratio(rows, "pack_v", "cell_avg_v")
    if series:
        series["expected"] = EXPECTED_SERIES_CELLS
        checks["series_cells"] = series
    capacity = _ratio(rows, "energy_kwh", "soc_pct", scale=0.01)
    if capacity:
        capacity["expected"] = EXPECTED_PACK_KWH
        checks["implied_pack_kwh"] = capacity
    at_full = _ratio(rows, "range_mi", "soc_pct", scale=0.01)
    if at_full:
        checks["range_at_full_mi"] = at_full
    efficiency = _ratio(rows, "range_mi", "energy_kwh")
    if efficiency:
        checks["vehicle_efficiency_mi_per_kwh"] = efficiency
    rail = _ratio(rows, "volts", "dmc2_v")
    if rail:
        checks["adapter_over_module_volts"] = rail
    return checks


def _sampling(rows: list[dict], expected_period_s: Optional[float]) -> dict:
    """How well the session was actually sampled.

    Reported before anything physical, because every derived quantity inherits
    the resolution measured here.
    """
    stamps = [r["elapsed_s"] for r in rows if isinstance(r.get("elapsed_s"), (int, float))]
    periods = [b - a for a, b in zip(stamps, stamps[1:]) if b > a]
    median = _median(periods)
    gaps = []
    if median:
        for index, (a, period) in enumerate(zip(stamps, periods)):
            if period > median * _GAP_FACTOR:
                gaps.append({
                    "after_elapsed_s": _round(a, 1),
                    "after_utc": rows[index].get("utc"),
                    "seconds": _round(period, 1),
                })
    return {
        "samples": len(rows),
        "median_period_s": _round(median, 2),
        "min_period_s": _round(min(periods), 2) if periods else None,
        "max_period_s": _round(max(periods), 2) if periods else None,
        "expected_period_s": _round(expected_period_s, 2),
        "gap_count": len(gaps),
        "gap_seconds_total": _round(sum(g["seconds"] for g in gaps), 1),
        "gaps": gaps[:20],
    }


def analyze(rows: list[dict], *, path: str = "", expected_period_s: Optional[float] = None,
            extra_warnings: Optional[list[str]] = None) -> dict:
    """A report on one session.

    Every figure is derived only from what the session recorded.  Where a
    quantity needs a column the session does not have, the figure is ``None``
    rather than a guess.
    """
    warnings: list[str] = list(extra_warnings or [])
    report: dict = {"session": {"path": path, "rows": len(rows)}}

    if not rows:
        report["warnings"] = warnings + ["the session has no rows"]
        return report

    elapsed = _series(rows, "elapsed_s")
    duration = (elapsed[-1] - elapsed[0]) if len(elapsed) >= 2 else 0.0
    report["session"].update({
        "start_utc": rows[0].get("utc"),
        "end_utc": rows[-1].get("utc"),
        "duration_s": _round(duration, 1),
        "duration_hms": f"{int(duration // 3600):d}:{int(duration % 3600 // 60):02d}:{int(duration % 60):02d}",
    })

    report["sampling"] = _sampling(rows, expected_period_s)

    # --- motion ------------------------------------------------------------
    #
    # Four routes to distance, because the obvious one is the least reliable
    # here.  `odometer_km` and `speed_kph` are standard OBD PIDs, and on
    # 2026-09-03 they answered in only 8 of 79 rows while every enhanced read
    # answered in all 79.  A report that keys distance off the odometer alone
    # therefore under-reports a real drive to nearly zero, which is exactly
    # what happened: 12.6 miles read as 0.06.  So each route is computed, the
    # densest trustworthy one is used, and the rest are shown beside it.
    odo_start, odo_end = _first_last(rows, "odometer_km")
    distance_km = None
    if odo_start is not None and odo_end is not None:
        distance_km = odo_end - odo_start
    distance_from_speed_km = _integrate(rows, "speed_kph")
    # The four wheels are an enhanced read and answer when `speed_kph` does
    # not, so their mean is the speed trace that actually survives a session.
    # Built into throwaway rows rather than written back: the caller's rows are
    # also what the completeness section reports on, and a derived column
    # invented here would be listed there as if the vehicle had sent it.
    wheel_rows = []
    for row in rows:
        corners = [row[c] for c in _WHEEL_COLUMNS if isinstance(row.get(c), (int, float))]
        if corners and isinstance(row.get("elapsed_s"), (int, float)):
            wheel_rows.append({"elapsed_s": row["elapsed_s"],
                               "wheel_mean_kph": sum(corners) / len(corners)})
    distance_from_wheels_km = _integrate(wheel_rows, "wheel_mean_kph")
    # `dist_since_chg_mi` is an enhanced read and is already in miles, but it
    # resets to zero when the vehicle charges.  A negative delta is that reset,
    # not a reversing truck, so it is discarded rather than reported.
    chg_start, chg_end = _first_last(rows, "dist_since_chg_mi")
    distance_since_charge_mi = None
    if chg_start is not None and chg_end is not None and chg_end >= chg_start:
        distance_since_charge_mi = chg_end - chg_start
    speeds = _series(rows, "speed_kph")
    moving = [s for s in speeds if s >= _MOVING_KPH]
    report["motion"] = {
        "odometer_start_km": odo_start,
        "odometer_end_km": odo_end,
        "distance_km": _round(distance_km, 2),
        "distance_mi": _round(distance_km / KM_PER_MILE, 2) if distance_km is not None else None,
        "distance_from_speed_km": _round(distance_from_speed_km, 2),
        "distance_from_wheels_km": _round(distance_from_wheels_km, 2),
        "distance_since_charge_mi": _round(distance_since_charge_mi, 2),
        "max_speed_kph": max(speeds) if speeds else None,
        "max_speed_mph": _round(max(speeds) / KM_PER_MILE, 1) if speeds else None,
        "mean_moving_kph": _round(_mean(moving), 1),
        "moving_samples": len(moving),
        "stopped_samples": len(speeds) - len(moving),
        # Deliberately not "or None": a session that genuinely never turned a
        # wheel reported zero, and zero is a reading rather than a gap.
        "max_wheel_kph": max(_wheels) if (_wheels := [
            v for key in ("wheel_fl_kph", "wheel_fr_kph", "wheel_rl_kph", "wheel_rr_kph")
            for v in _series(rows, key)
        ]) else None,
    }

    # Pick the distance every derived figure will use, in order of how much
    # this vehicle's data can be trusted to carry it, and record the choice so
    # a reader is never guessing which number fed the efficiency.
    distance_mi = None
    distance_basis = None
    if distance_km:
        distance_mi, distance_basis = distance_km / KM_PER_MILE, "odometer_km"
    elif distance_since_charge_mi:
        distance_mi, distance_basis = distance_since_charge_mi, "dist_since_chg_mi"
    elif distance_from_wheels_km:
        distance_mi, distance_basis = distance_from_wheels_km / KM_PER_MILE, "wheel speeds"
    elif distance_from_speed_km:
        distance_mi, distance_basis = distance_from_speed_km / KM_PER_MILE, "speed_kph"
    report["motion"]["distance_used_mi"] = _round(distance_mi, 2)
    report["motion"]["distance_basis"] = distance_basis

    # --- energy ------------------------------------------------------------
    e_start, e_end = _first_last(rows, "energy_kwh")
    soc_start, soc_end = _first_last(rows, "soc_pct")
    energy_used = (e_start - e_end) if (e_start is not None and e_end is not None) else None
    energy = {
        "energy_start_kwh": e_start,
        "energy_end_kwh": e_end,
        "energy_used_kwh": _round(energy_used, 3),
        "soc_start_pct": soc_start,
        "soc_end_pct": soc_end,
        "soc_drop_pct": _round(soc_start - soc_end, 3) if (soc_start is not None and soc_end is not None) else None,
    }
    # energy_kwh is energy remaining and soc_pct is that as a percentage, so
    # their ratio is the pack size the vehicle itself believes in.  It is a
    # cross-check on both fields rather than a specification figure.
    if e_start is not None and soc_start:
        energy["implied_pack_kwh"] = _round(e_start / (soc_start / 100.0), 1)
    if distance_mi and energy_used and energy_used > 0:
        energy["efficiency_mi_per_kwh"] = _round(distance_mi / energy_used, 2)
        energy["consumption_kwh_per_100mi"] = _round(energy_used / distance_mi * 100.0, 1)
    # Regenerated energy, from the pack's own current.  hv_power_kw is positive
    # while discharging, so the negative excursions are what came back in.
    regen_kwh = -_integrate(rows, "hv_power_kw", only_negative=True)
    drawn_kwh = _integrate(rows, "hv_power_kw", only_positive=True)
    if _series(rows, "hv_power_kw"):
        energy["regen_kwh_from_pack_current"] = _round(regen_kwh, 3)
        energy["drawn_kwh_from_pack_current"] = _round(drawn_kwh, 3)
        energy["net_kwh_from_pack_current"] = _round(drawn_kwh - regen_kwh, 3)
    report["energy"] = energy

    # --- pack --------------------------------------------------------------
    pack_v = _series(rows, "pack_v")
    pack_a = _series(rows, "pack_a")
    hv_kw = _series(rows, "hv_power_kw")
    report["pack"] = {
        "v_min": min(pack_v) if pack_v else None,
        "v_max": max(pack_v) if pack_v else None,
        "v_mean": _round(_mean(pack_v), 2),
        "v_sag": _round(max(pack_v) - min(pack_v), 2) if pack_v else None,
        "a_min": min(pack_a) if pack_a else None,
        "a_max": max(pack_a) if pack_a else None,
        # Positive is discharge for this column.
        "peak_discharge_kw": _round(max(hv_kw), 2) if hv_kw else None,
        "peak_regen_kw": _round(-min(hv_kw), 2) if hv_kw and min(hv_kw) < 0 else None,
        "mean_kw": _round(_mean(hv_kw), 2),
    }

    # --- the two power routes, compared ------------------------------------
    # Normalised so both mean "power leaving the pack": the slope column is
    # negated, the product column is not.
    pairs = [
        (row["hv_power_kw"], -row["power_kw"])
        for row in rows
        if isinstance(row.get("hv_power_kw"), (int, float))
        and isinstance(row.get("power_kw"), (int, float))
    ]
    if pairs:
        diffs = [abs(a - b) for a, b in pairs]
        report["power_cross_check"] = {
            "convention": "both normalised to positive = power leaving the pack "
                          "(hv_power_kw as recorded, power_kw negated)",
            "samples_compared": len(pairs),
            "mean_abs_difference_kw": _round(_mean(diffs), 2),
            "max_abs_difference_kw": _round(max(diffs), 2),
            "mean_hv_power_kw": _round(_mean([a for a, _ in pairs]), 2),
            "mean_slope_power_kw": _round(_mean([b for _, b in pairs]), 2),
        }

    # --- cells -------------------------------------------------------------
    spread = _series(rows, "cell_spread_mv")
    report["cells"] = {
        "spread_mv_min": min(spread) if spread else None,
        "spread_mv_max": max(spread) if spread else None,
        "spread_mv_mean": _round(_mean(spread), 2),
        "cell_min_v_seen": min(_series(rows, "cell_min_v")) if _series(rows, "cell_min_v") else None,
        "cell_max_v_seen": max(_series(rows, "cell_max_v")) if _series(rows, "cell_max_v") else None,
    }

    # --- thermal and chassis ----------------------------------------------
    temps = _series(rows, "temp_f")
    report["thermal"] = {
        "temp_f_min": min(temps) if temps else None,
        "temp_f_max": max(temps) if temps else None,
    }
    lat = _series(rows, "lateral_g")
    lon = _series(rows, "longitudinal_g")
    brake = _series(rows, "brake_kpa")
    steer = _series(rows, "steering_deg")
    report["chassis"] = {
        "max_abs_lateral_g": _round(max(abs(v) for v in lat), 3) if lat else None,
        "max_abs_longitudinal_g": _round(max(abs(v) for v in lon), 3) if lon else None,
        "brake_kpa_min": min(brake) if brake else None,
        "brake_kpa_max": max(brake) if brake else None,
        "steering_deg_min": min(steer) if steer else None,
        "steering_deg_max": max(steer) if steer else None,
    }

    # --- completeness ------------------------------------------------------
    completeness = {}
    for key in rows[0]:
        present = sum(1 for r in rows if r.get(key) is not None)
        completeness[key] = f"{present}/{len(rows)}"
        if present == 0:
            warnings.append(f"column '{key}' is empty for the whole session")
        elif present < len(rows) * 0.5:
            warnings.append(
                f"column '{key}' is present in only {present} of {len(rows)} "
                f"rows; anything derived from it is thin"
            )
    report["completeness"] = completeness

    # --- warnings ----------------------------------------------------------
    sampling = report["sampling"]
    median = sampling["median_period_s"]
    if expected_period_s and median and median > expected_period_s * 1.5:
        warnings.append(
            f"the median sample period was {median}s against an expected "
            f"{_round(expected_period_s, 2)}s, so the capture is coarser than "
            f"intended"
        )
    if sampling["gap_count"]:
        warnings.append(
            f"{sampling['gap_count']} gap(s) longer than {_GAP_FACTOR}x the "
            f"median period, totalling {sampling['gap_seconds_total']}s of the "
            f"session with no samples"
        )
    if distance_km is not None and distance_from_speed_km:
        if distance_km > 0.5 and abs(distance_km - distance_from_speed_km) > distance_km * 0.25:
            warnings.append(
                f"the odometer says {_round(distance_km, 2)} km and integrated "
                f"speed says {_round(distance_from_speed_km, 2)} km; at this "
                f"sample rate they should agree more closely than that"
            )
    cross = report.get("power_cross_check")
    if cross and cross["mean_hv_power_kw"] is not None:
        scale = max(abs(cross["mean_hv_power_kw"]), 1.0)
        if cross["mean_abs_difference_kw"] > scale * 0.5:
            warnings.append(
                "the two power routes disagree by more than half the measured "
                "magnitude; one of pack current or the energy slope is not "
                "what it is labelled"
            )
    checks = _cross_checks(rows)
    if checks:
        report["cross_checks"] = checks
        for key, expected, tolerance, label in (
            ("series_cells", EXPECTED_SERIES_CELLS, 0.5, "cells in series"),
            ("implied_pack_kwh", EXPECTED_PACK_KWH, 5.0, "usable pack capacity"),
        ):
            got = checks.get(key)
            if got and abs(got["mean"] - expected) > tolerance:
                warnings.append(
                    f"{label} measured {got['mean']} against an expected "
                    f"{expected}; a decoder scaling may have changed, because "
                    f"this ratio is a property of the pack rather than of the "
                    f"drive"
                )

    report["warnings"] = warnings
    return report


def _line(label: str, value, unit: str = "") -> str:
    if value is None:
        return f"  {label:<34} --"
    return f"  {label:<34} {value}{unit}"


def format_report(report: dict) -> str:
    """The report as text, capture quality first."""
    out: list[str] = []
    session = report.get("session", {})
    out.append(f"session: {session.get('path') or '(unnamed)'}")
    out.append(_line("rows", session.get("rows")))
    out.append(_line("start (UTC)", session.get("start_utc")))
    out.append(_line("end (UTC)", session.get("end_utc")))
    out.append(_line("duration", session.get("duration_hms")))

    if "sampling" in report:
        out.append("")
        out.append("capture quality")
        s = report["sampling"]
        out.append(_line("median sample period", s.get("median_period_s"), " s"))
        out.append(_line("expected sample period", s.get("expected_period_s"), " s"))
        shortest, longest = s.get("min_period_s"), s.get("max_period_s")
        out.append(_line(
            "shortest / longest period",
            None if shortest is None and longest is None else f"{shortest} / {longest}",
            " s",
        ))
        out.append(_line("gaps", s.get("gap_count")))
        out.append(_line("seconds lost to gaps", s.get("gap_seconds_total"), " s"))

    for title, keys in (
        ("motion", ("distance_used_mi", "distance_basis",
                    "distance_km", "distance_mi", "distance_since_charge_mi",
                    "distance_from_wheels_km", "distance_from_speed_km",
                    "max_speed_kph", "max_speed_mph", "mean_moving_kph",
                    "moving_samples", "stopped_samples", "max_wheel_kph")),
        ("energy", ("energy_start_kwh", "energy_end_kwh", "energy_used_kwh",
                    "soc_start_pct", "soc_end_pct", "soc_drop_pct",
                    "implied_pack_kwh", "efficiency_mi_per_kwh",
                    "consumption_kwh_per_100mi", "drawn_kwh_from_pack_current",
                    "regen_kwh_from_pack_current", "net_kwh_from_pack_current")),
        ("pack", ("v_min", "v_max", "v_mean", "v_sag", "a_min", "a_max",
                  "peak_discharge_kw", "peak_regen_kw", "mean_kw")),
        ("cells", ("spread_mv_min", "spread_mv_max", "spread_mv_mean",
                   "cell_min_v_seen", "cell_max_v_seen")),
        ("thermal", ("temp_f_min", "temp_f_max")),
        ("chassis", ("max_abs_lateral_g", "max_abs_longitudinal_g",
                     "brake_kpa_min", "brake_kpa_max",
                     "steering_deg_min", "steering_deg_max")),
    ):
        block = report.get(title)
        if not block:
            continue
        out.append("")
        out.append(title)
        for key in keys:
            if key in block:
                out.append(_line(key.replace("_", " "), block[key]))

    checks = report.get("cross_checks")
    if checks:
        out.append("")
        out.append("cross-checks  (ratios between decoded fields: these test the")
        out.append("               decoders, not the drive)")
        for key, unit in (("series_cells", " cells in series"),
                          ("implied_pack_kwh", " kWh usable"),
                          ("range_at_full_mi", " mi at 100%"),
                          ("vehicle_efficiency_mi_per_kwh", " mi/kWh assumed"),
                          ("adapter_over_module_volts", " ATRV / module volts")):
            got = checks.get(key)
            if not got:
                continue
            expected = f"  (expected {got['expected']})" if "expected" in got else ""
            out.append(
                f"  {got['mean']:>10}{unit:<24} sd {got['sd']:<8} "
                f"n={got['samples']}{expected}"
            )

    cross = report.get("power_cross_check")
    if cross:
        out.append("")
        out.append("power, two independent routes")
        out.append(f"  {cross['convention']}")
        out.append(_line("samples compared", cross.get("samples_compared")))
        out.append(_line("mean pack V x A", cross.get("mean_hv_power_kw"), " kW"))
        out.append(_line("mean energy slope", cross.get("mean_slope_power_kw"), " kW"))
        out.append(_line("mean |difference|", cross.get("mean_abs_difference_kw"), " kW"))

    warnings = report.get("warnings") or []
    out.append("")
    if warnings:
        out.append(f"warnings ({len(warnings)})")
        for warning in warnings:
            out.append(f"  - {warning}")
    else:
        out.append("warnings: none")
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline analysis of a drive session CSV; never opens the "
                    "adapter or the vehicle"
    )
    parser.add_argument("session", nargs="?", help="path to a drive-*.csv session file")
    parser.add_argument(
        "--dir",
        default=None,
        help="analyse the newest drive-*.csv in this directory instead of a named file",
    )
    parser.add_argument(
        "--expected-period-s",
        type=float,
        default=None,
        help="the sample period the recorder was configured for, so the report "
             "can say whether it was achieved (cycle time + DRIVE_INTERVAL_S)",
    )
    parser.add_argument("--json", dest="json_path", help="also write the report as JSON here")
    parser.add_argument("--quiet", action="store_true", help="suppress the text report")
    args = parser.parse_args(argv)

    target: Optional[Path] = None
    if args.session:
        target = Path(args.session)
    elif args.dir:
        candidates = sorted(Path(args.dir).glob("drive-*.csv"))
        if not candidates:
            print(f"ERROR: no drive-*.csv in {args.dir}", file=sys.stderr)
            return 2
        target = candidates[-1]
    else:
        parser.error("give a session file or --dir")

    try:
        rows, warnings, _header = read_session(target)
    except OSError as exc:
        print(f"ERROR: cannot read {target}: {exc}", file=sys.stderr)
        return 2

    report = analyze(
        rows, path=str(target), expected_period_s=args.expected_period_s,
        extra_warnings=warnings,
    )

    if args.json_path:
        try:
            Path(args.json_path).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json_path).write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            # A report that was produced is still a report.
            print(f"WARNING: could not write {args.json_path}: {exc}", file=sys.stderr)

    if not args.quiet:
        print(format_report(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
