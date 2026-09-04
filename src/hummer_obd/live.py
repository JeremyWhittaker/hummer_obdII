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
import statistics
import sys
import time
from typing import Optional

from . import drive
from .analyze import read_session, sane

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
    "array_2b43": ("per-module array 0x2B43 (raw)", ""),
    "array_2af1": ("per-module array 0x2AF1 (raw)", ""),
    "cell_extra_raw": ("0x2AF5 trailing bytes (raw)", ""),
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

#: Structure measured in ``0x2B43``'s 26 values over 1288 samples.  Every
#: position tracks state of charge at +0.995 or better, so they are 26 parallel
#: measurements of the same quantity rather than a mixed record.  Two blocks
#: separate cleanly: the spread *across* positions 2-25 is 2.1x the spread
#: *within* either block, which is not what one undifferentiated set looks
#: like.  Positions 0-1 sit ~1.5 units below the rest in every sample.
#:
#: Twelve modules in series at eight cells each is 96 cells, and 96.0 is
#: exactly what ``pack_v / cell_avg_v`` measures (mean 95.991, sd 0.041 over
#: 297 samples).  Two blocks of twelve is also how a 400 V/800 V switchable
#: pack is wired.  That is the reading; the labels below stay deliberately
#: non-committal about it, because a plausible story is not a measurement.
_ARRAY_BLOCKS = ((0, 2, "?"), (2, 14, "A"), (14, 26, "B"))


def _expand_array(text: str) -> list[tuple[str, str]]:
    """One entry per value, each with its drift from its own block's median.

    The drift column is the point.  Absolute values move together as the pack
    charges and discharges, so they say little; a single value pulling away
    from its neighbours is the earliest visible sign of one module going bad,
    and it shows up here long before it moves the pack-wide min/max envelope.
    """
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        return [("(unparseable)", text[:40])]
    out: list[tuple[str, str]] = []
    for start, stop, block in _ARRAY_BLOCKS:
        chunk = raw[start:stop]
        if not chunk:
            continue
        middle = sorted(chunk)[len(chunk) // 2]
        for offset, value in enumerate(chunk):
            drift = value - middle
            mark = "" if abs(drift) <= 1 else ("   <-- drifting" if abs(drift) > 2 else "   <-- watch")
            out.append(
                (f"value {start + offset:02d} (block {block})",
                 f"{value}  {drift:+d} from block median{mark}")
            )
    return out


def _expand_flat_array(text: str) -> list[tuple[str, str]]:
    """One entry per value, with its drift from the array's median.

    No block structure is imposed here.  ``0x2B43``'s two blocks were measured
    before they were used; nothing has measured any structure in this array, so
    it is compared against itself as a whole rather than against groups that
    have only been assumed.
    """
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        return [("(unparseable)", text[:40])]
    if not raw:
        return []
    middle = sorted(raw)[len(raw) // 2]
    out = []
    for index, value in enumerate(raw):
        drift = value - middle
        mark = "" if abs(drift) <= 1 else ("   <-- drifting" if abs(drift) > 2 else "   <-- watch")
        out.append((f"value {index:02d}", f"{value}  {drift:+d} from median{mark}"))
    return out


def _expand_bytes(text: str) -> list[tuple[str, str]]:
    """Each byte of a preserved-but-undecoded field, as hex and decimal."""
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        return [("(unparseable)", text[:40])]
    return [(f"byte {i}", f"0x{v:02X}  = {v}") for i, v in enumerate(raw)]


#: Columns holding several values in one cell, and how to break them out.
#: Nothing here is decoded into units -- these are the raw values the vehicle
#: sent, made individually visible instead of collapsed into one opaque string.
EXPANSIONS: dict[str, tuple[str, object]] = {
    "array_2af1": (
        "0x2AF1 -- 24 per-module values, broken out (source calls these module "
        "temperatures; the scaling is NOT proven, so these are raw)",
        _expand_flat_array,
    ),
    "array_2b43": (
        "0x2B43 -- 26 per-module values, broken out "
        "(all track charge at +0.995; drift is what matters)",
        _expand_array,
    ),
    "cell_extra_raw": (
        "0x2AF5 -- the 4 trailing bytes, undecoded but no longer discarded",
        _expand_bytes,
    ),
    "charger_5401_raw": (
        "0x5401 -- raw, kept unscaled until a charge session calibrates it",
        _expand_bytes,
    ),
}


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
    # Read the addressing out of STANDARD_ADDRESS rather than describing it:
    # these PIDs were a functional broadcast until they were pointed at one
    # module, and a label saying "broadcast" would have survived that change
    # and quietly lied about it.
    standard_header = next(
        (c for c in drive.STANDARD_ADDRESS if c.startswith("ATSH")), ""
    )
    if standard_header.startswith("ATSHDA"):
        standard_where = f"standard OBD, asked of module {standard_header[6:8]}"
    elif standard_header:
        standard_where = "standard OBD, broadcast to every module"
    else:
        standard_where = "standard OBD"
    for request, column in drive.STANDARD_PIDS:
        sources[column] = (standard_where, request)
    # PID 01 is not in STANDARD_PIDS because it is not a scalar -- four bytes
    # of packed flags, two of which become columns.  Attributing them here
    # rather than letting them fall through as "unknown source" is the whole
    # reason this function reads the recorder's own tables instead of keeping
    # a list beside them.
    for column in drive.MONITOR_STATUS_COLUMNS:
        sources[column] = (standard_where, drive.MONITOR_STATUS_PID)
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


#: The zero point of ``0x2429``, measured rather than assumed: this exact value
#: is held across 1083 stationary samples corpus-wide and is the only value
#: present in 7 of the 8 sessions that carry the field.  Above it is drive
#: torque, below it is regen.  No newtons-per-count is published -- the fitted
#: constant is not a divisor a designer would pick -- so this reports signed
#: counts from zero and nothing more.
TORQUE_ZERO: int = 22534

#: Consecutive-sample current steps smaller than this are not used for the
#: resistance fit.  The estimator is dV/dI between adjacent samples, so a small
#: step divides sensor noise by a small number and the result explodes.
_MIN_STEP_AMPS: float = 20.0

#: Below this many usable steps the resistance figure is not reported at all.
_MIN_STEPS: int = 5


def _num(row: dict, name: str):
    """A float from a session cell, or ``None``."""
    value = row.get(name)
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    while text and text[-1] not in "0123456789.":
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return None


def _hex(row: dict, *names: str):
    """An int from the first of *names* the row carries, read as hex.

    Takes several names because a column rename leaves every session already on
    disk carrying the old header, and a live view that only knows the new one
    would show a blank for the whole back catalogue.
    """
    for name in names:
        value = row.get(name)
        if value is None or value == "":
            continue
        try:
            return int(str(value).strip(), 16)
        except ValueError:
            return None
    return None


def _last(rows: list[dict], name: str):
    for row in reversed(rows):
        value = _num(row, name)
        if value is not None:
            return value
    return None


def _last_hex(rows: list[dict], *names: str):
    for row in reversed(rows):
        value = _hex(row, *names)
        if value is not None:
            return value
    return None


def _series(rows: list[dict], name: str) -> list[float]:
    out = []
    for row in rows:
        value = _num(row, name)
        if value is not None:
            out.append(value)
    return out


def pack_resistance(rows: list[dict]) -> Optional[tuple[float, int, float]]:
    """Pack DC internal resistance in milliohms, from consecutive steps.

    Terminal voltage is ``OCV - I*R``, so between two adjacent samples the
    open-circuit term cancels and ``-dV/dI`` is the resistance.  That
    cancellation is why this is used rather than regressing voltage on current
    directly: the level regression carries the state-of-charge trend into the
    slope, and on one recorded session it returns the wrong sign entirely.

    Returns ``(milliohms, n_steps, correlation)``, or ``None`` when the session
    has not yet produced enough current movement to measure anything.
    """
    steps: list[tuple[float, float]] = []
    for before, after in zip(rows, rows[1:]):
        v0, i0 = _num(before, "pack_v"), _num(before, "pack_a")
        v1, i1 = _num(after, "pack_v"), _num(after, "pack_a")
        if None in (v0, i0, v1, i1):
            continue
        di = i1 - i0
        if abs(di) < _MIN_STEP_AMPS:
            continue
        steps.append((di, v1 - v0))
    if len(steps) < _MIN_STEPS:
        return None
    # Least squares through the origin: a step of no current must produce no
    # change in voltage, so an intercept here would be fitting noise.
    denom = sum(di * di for di, _ in steps)
    if denom <= 0:
        return None
    slope = sum(di * dv for di, dv in steps) / denom
    xs = [di for di, _ in steps]
    ys = [dv for _, dv in steps]
    try:
        r = statistics.correlation(xs, ys)
    except (statistics.StatisticsError, ValueError):
        r = float("nan")
    return (-slope * 1000.0, len(steps), r)


def derive(rows: list[dict]) -> dict:
    """Every quantity this project has established how to compute.

    The raw table above this one answers "is the vehicle talking".  This
    answers "what does it mean", and it is deliberately separate: a derived
    number is only as good as the grading behind it, and mixing the two invites
    reading a level-1 guess with the same confidence as a level-4 measurement.
    Anything not yet established is left out rather than estimated.
    """
    if not rows:
        return {}
    out: dict = {}

    # Pack state is read from SANE rows only.  The last row of a session is
    # very often the vehicle going to sleep with the contactors open, where
    # pack_v reads about 1 V -- which would show a 1.06 V pack and 0.3 cells
    # in series, and look like a decode fault rather than a sleeping truck.
    good = [r for r in rows if sane(r)] or rows

    # -- pack, all level 4 ---------------------------------------------------
    pack_v = _last(good, "pack_v")
    pack_a = _last(good, "pack_a")
    cell_avg = _last(good, "cell_avg_v")
    soc = _last(good, "soc_pct")
    energy = _last(good, "energy_kwh")
    out["pack_v"] = pack_v
    out["pack_a"] = pack_a
    out["pack_kw"] = (pack_v * pack_a / 1000.0) if None not in (pack_v, pack_a) else None
    out["cell_avg_v"] = cell_avg
    out["cell_spread_mv"] = _last(good, "cell_spread_mv")
    out["soc_pct"] = soc
    out["energy_kwh"] = energy
    out["series_cells"] = (pack_v / cell_avg) if pack_v and cell_avg else None
    # Usable capacity implied by where the pack sits right now.  Corpus median
    # is 190.5 kWh with sd 0.89 over 6961 rows, so a live value far from that
    # is a reading problem rather than a discovery.
    out["implied_kwh"] = (energy / (soc / 100.0)) if energy and soc else None

    # -- measured constants --------------------------------------------------
    out["resistance"] = pack_resistance(good)

    # -- motion and energy over this session ---------------------------------
    odo = _series(good, "odometer_km")
    out["distance_km"] = (max(odo) - min(odo)) if len(odo) >= 2 else None
    ek = _series(good, "energy_kwh")
    out["energy_used_kwh"] = (max(ek) - min(ek)) if len(ek) >= 2 else None

    # Efficiency is measured over the MOVING window, not the whole session.
    # A session that parked with the air conditioning on for forty minutes
    # drained several kWh against zero distance, and charging that to the
    # drive turns a real 42 kWh/100km into a meaningless 63.
    moving = [
        i for i, r in enumerate(good)
        if (_num(r, "speed_kph") or _num(r, "wheel_fl_kph") or 0) > 1
    ]
    if len(moving) >= 2:
        span = good[moving[0]:moving[-1] + 1]
        d_odo = _series(span, "odometer_km")
        d_ek = _series(span, "energy_kwh")
        if len(d_odo) >= 2 and len(d_ek) >= 2:
            km = max(d_odo) - min(d_odo)
            kwh = max(d_ek) - min(d_ek)
            out["drive_km"] = km
            out["drive_kwh"] = kwh
            if km > 0.05 and kwh > 0:
                out["kwh_per_100km"] = kwh / km * 100.0
                out["mi_per_kwh"] = (km * 0.621371) / kwh
    speeds = _series(rows, "speed_kph")
    out["speed_max_kph"] = max(speeds) if speeds else None
    out["speed_now_kph"] = _last(rows, "speed_kph")
    kw = _series(rows, "hv_power_kw")
    out["kw_peak_drive"] = max(kw) if kw else None
    out["kw_peak_regen"] = min(kw) if kw else None

    # Energy split by direction, trapezoid over the samples that carry both a
    # power and a timestamp.  Regen fraction is the honest headline here; the
    # absolute kWh depend on a 7-9 s poll that misses short events.
    #
    # Integrated over the MOVING span for the same reason efficiency is: a
    # parked air-conditioning load is drawn energy that no amount of braking
    # could ever return, so including it silently deflates the fraction.
    span_for_energy = (
        good[moving[0]:moving[-1] + 1] if len(moving) >= 2 else good
    )
    drawn = returned = 0.0
    for before, after in zip(span_for_energy, span_for_energy[1:]):
        p0, p1 = _num(before, "hv_power_kw"), _num(after, "hv_power_kw")
        t0, t1 = _num(before, "elapsed_s"), _num(after, "elapsed_s")
        if None in (p0, p1, t0, t1) or t1 <= t0:
            continue
        dt = (t1 - t0) / 3600.0
        mid = (p0 + p1) / 2.0
        if mid >= 0:
            drawn += mid * dt
        else:
            returned += -mid * dt
    out["kwh_drawn"] = drawn or None
    out["kwh_regen"] = returned or None
    out["regen_pct"] = (returned / drawn * 100.0) if drawn > 0 else None

    # -- the torque signal, zero-referenced ----------------------------------
    torque = _last_hex(rows, "field_2429_raw")
    if torque is not None:
        out["torque_counts"] = torque - TORQUE_ZERO
        out["torque_dir"] = (
            "drive" if torque > TORQUE_ZERO + 30
            else "regen" if torque < TORQUE_ZERO - 30
            else "neutral"
        )

    # -- the 12 V domain -----------------------------------------------------
    out["volts_adapter"] = _last(rows, "volts")
    out["volts_module"] = _last(rows, "module_voltage")
    out["volts_dmc2"] = _last(rows, "dmc2_v")

    # -- vehicle state -------------------------------------------------------
    # 0x4127 tracks whether the powertrain is reporting road speed.  1048 is
    # the state in which no speed is reported at all -- in 477 corpus samples
    # at that value, not one carries a speed.
    state_word = _last_hex(rows, "field_4127_raw", "batt_temp_a_raw")
    out["state_word"] = state_word
    out["powertrain"] = {
        1048: "down (no road speed reported)",
        246: "reporting road speed",
        234: "awake, stationary",
        601: "cabin heat active",
    }.get(state_word)
    charger = None
    for row in reversed(rows):
        value = row.get("charger_5401_raw")
        if value not in (None, ""):
            charger = str(value).strip()
            break
    out["charger_raw"] = charger
    out["charging"] = (charger not in (None, "", "00")) if charger is not None else None

    # -- since-last-charge accumulators --------------------------------------
    out["thermal_energy"] = _last_hex(rows, "thermal_energy_raw")
    out["thermal_distance"] = _last_hex(rows, "thermal_distance_raw")
    out["regen_counter"] = _last_hex(rows, "regen_field_raw")
    out["dist_since_charge_mi"] = _last(rows, "dist_since_chg_mi")
    return out


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


def _fmt(value, spec: str = ".2f", dash: str = "--") -> str:
    """A number formatted, or a dash.  Never a bare ``None`` on screen."""
    if value is None:
        return dash
    try:
        return format(value, spec)
    except (TypeError, ValueError):
        return str(value)


def render_derived(d: dict) -> list[str]:
    """The derived block: what the raw columns above actually mean.

    Every line here is downstream of a grading in ``confidence.py``.  Where a
    quantity is established the number is given plainly; where only the
    behaviour is established and not the units -- the accumulators, the torque
    signal -- the raw count is shown and no unit is invented for it.
    """
    if not d:
        return []
    out: list[str] = []
    out.append("-- DERIVED -- what the numbers above mean " + "-" * 36)

    out.append("  TRACTION PACK                              96S, three routes agree")
    out.append(
        f"    pack              {_fmt(d.get('pack_v'))} V"
        f"   {_fmt(d.get('pack_a'), '+.2f')} A"
        f"   {_fmt(d.get('pack_kw'), '+.2f')} kW"
    )
    out.append(
        f"    cells             {_fmt(d.get('series_cells'), '.1f')} in series"
        f"   avg {_fmt(d.get('cell_avg_v'), '.4f')} V"
        f"   spread {_fmt(d.get('cell_spread_mv'), '.1f')} mV"
    )
    out.append(
        f"    charge            {_fmt(d.get('soc_pct'), '.3f')} %"
        f"   {_fmt(d.get('energy_kwh'))} kWh"
        f"   implies {_fmt(d.get('implied_kwh'), '.1f')} kWh pack"
    )
    res = d.get("resistance")
    if res:
        milliohms, n, r = res
        out.append(
            f"    resistance        {milliohms:.2f} mOhm"
            f"   {milliohms / 96.0:.4f} mOhm/cell"
            f"   (n={n} steps, r={r:+.3f})"
        )
    else:
        out.append(
            "    resistance        -- (needs current steps above 20 A; "
            "parked sessions cannot)"
        )

    out.append("  MOTION AND ENERGY")
    out.append(
        f"    speed             now {_fmt(d.get('speed_now_kph'), '.1f')}"
        f"   peak {_fmt(d.get('speed_max_kph'), '.1f')} kph"
    )
    out.append(
        f"    whole session     {_fmt(d.get('distance_km'), '.2f')} km"
        f"   {_fmt(d.get('energy_used_kwh'), '.2f')} kWh used (parked draw included)"
    )
    if d.get("kwh_per_100km"):
        out.append(
            f"    while moving      {_fmt(d.get('drive_km'), '.2f')} km"
            f"   {_fmt(d.get('drive_kwh'), '.2f')} kWh"
        )
        out.append(
            f"    efficiency        {_fmt(d.get('kwh_per_100km'), '.1f')} kWh/100km"
            f"   {_fmt(d.get('mi_per_kwh'), '.2f')} mi/kWh   (moving only)"
        )
    if d.get("regen_pct") is not None:
        out.append(
            f"    regen             {_fmt(d.get('kwh_regen'), '.2f')} kWh back of"
            f" {_fmt(d.get('kwh_drawn'), '.2f')} drawn"
            f"   = {_fmt(d.get('regen_pct'), '.1f')} %"
        )
    out.append(
        f"    peak power        {_fmt(d.get('kw_peak_drive'), '+.1f')} drive"
        f"   {_fmt(d.get('kw_peak_regen'), '+.1f')} regen  kW"
    )
    if d.get("torque_counts") is not None:
        out.append(
            f"    torque signal     {d['torque_counts']:+d} counts from zero"
            f"   ({d.get('torque_dir')})   0x2429, no unit established"
        )

    out.append("  12 V DOMAIN                                one rail, stable offsets")
    out.append(
        f"    adapter ATRV      {_fmt(d.get('volts_adapter'))} V"
        f"   PID 0142 {_fmt(d.get('volts_module'), '.3f')} V"
        f"   0x33E5 {_fmt(d.get('volts_dmc2'))} V"
    )
    out.append("                      PID 0142 is the instrument: 1 mV vs 0.1 V steps")

    out.append("  VEHICLE STATE")
    word = d.get("state_word")
    out.append(
        f"    powertrain        {d.get('powertrain') or 'unknown'}"
        + (f"   (0x4127 = {word})" if word is not None else "")
    )
    charging = d.get("charging")
    out.append(
        f"    charging          "
        f"{'yes' if charging else 'no' if charging is not None else '--'}"
        f"   (0x5401 = {d.get('charger_raw') or '--'})"
    )

    out.append("  SINCE LAST CHARGE                          counters, units unknown")
    out.append(
        f"    thermal energy    {d.get('thermal_energy') if d.get('thermal_energy') is not None else '--'}"
        f"   distance counter {d.get('thermal_distance') if d.get('thermal_distance') is not None else '--'}"
        f"   regen {d.get('regen_counter') if d.get('regen_counter') is not None else '--'}"
    )
    out.append(f"    distance          {_fmt(d.get('dist_since_charge_mi'))} mi")
    out.append("")
    return out


def render(snap: dict, *, path: str = "", sources: Optional[dict] = None,
           stale_after: float = 30.0, expand: bool = True,
           derived: Optional[dict] = None) -> str:
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
    # The derived block goes FIRST.  The raw table below answers "is the
    # vehicle talking"; this answers "what is it saying", which is what a
    # person watching actually wants, and it should not need scrolling past
    # fifty rows of hex to reach.
    if derived:
        out.extend(render_derived(derived))

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

    # Break out the columns that hold many values in one cell.  Grouping them
    # kept the CSV narrow, which is the right trade for storage and the wrong
    # one for looking at: 26 per-module values collapsed to one hex string are
    # captured but not visible, and something you cannot see you will not check.
    if expand:
        for column, (title, expander) in EXPANSIONS.items():
            info = snap["columns"].get(column)
            if not info or info["value"] is None:
                continue
            out.append(f"-- {title} " + "-" * max(0, 75 - len(title)))
            for label, value in expander(str(info["value"])):
                out.append(f"  {label:<28} {value}")
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
        "--compact", action="store_true",
        help="do not break multi-value columns out into their individual "
             "values (0x2B43's 26 per-module readings and the raw byte fields)",
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
        # The snapshot is windowed so a stale column still shows its last
        # value; the derived figures use the WHOLE session, because distance,
        # efficiency and resistance are properties of the drive rather than of
        # the last few minutes of it.
        print(render(snapshot(rows[-args.window:]), path=path,
                     stale_after=args.stale_after, expand=not args.compact,
                     derived=derive(rows)))
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
