"""Read the Uniden R8's published state, for display beside vehicle telemetry.

The radar detector is logged by a sibling project on the same Pi. It publishes
a small JSON state file and keeps a SQLite history; this reads both and never
writes to either.

Everything here treats that input as untrusted. Not because the sibling is
suspect -- it is the same operator's code on the same machine -- but because it
is written by a different process on its own schedule, and a reader that
assumes well-formed input is one truncated write away from taking the dashboard
down with it. The e-paper display already reads this file with the same
posture; this follows it.

Three specific rules, each earned:

* **Pin the schema to exactly 1.** The sibling's tests pin its key sets as
  literals precisely because this project consumes them. A ``>=`` comparison
  would silently accept a schema 2 whose fields moved.
* **Compute age here, from ``updated_at``, on our own clock.** The file's own
  ``stale`` flag is packet age at write time, so it stops updating when the
  writer dies -- exactly when staleness matters most.
* **Allowlist every enumerated value.** Band and direction reach a display, and
  a display is not the place to find out a field was free text.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

STATE_PATH = Path("/home/jeremy/unidenr8/.state/state.json")
HISTORY_PATH = Path("/home/jeremy/unidenr8/.state/history.db")

#: The only schema whose field names this code knows.
SCHEMA = 1

#: A state file larger than this is not a state file.
MAX_BYTES = 64 * 1024

#: Past this age the reading is reported stale rather than shown as current.
MAX_AGE_S = 90.0

#: A clock that disagrees by more than this is wrong, not early.
MAX_SKEW_S = 30.0

BANDS = frozenset({"X", "K", "KA", "LASER", "MRCD", "MRCT", "RT3", "RT4",
                   "K POP", "KA POP"})
DIRECTIONS = frozenset({"front", "side", "rear", "unknown"})

#: How many recent alerts to return. A dashboard panel, not an audit log.
MAX_ALERTS = 12


def _age_s(updated_at: Any, now: Optional[datetime] = None) -> Optional[float]:
    """Seconds since the file said it was written, or ``None`` if unusable."""
    if not isinstance(updated_at, str):
        return None
    try:
        stamp = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    delta = ((now or datetime.now(timezone.utc)) - stamp).total_seconds()
    # A file stamped in the future is a clock problem, not a fresh reading.
    return None if delta < -MAX_SKEW_S else delta


def read_state(path: Path = STATE_PATH, *, now: Optional[datetime] = None) -> dict:
    """The detector's current state, or a dict saying why there is none."""
    try:
        raw = path.read_bytes()
    except OSError:
        return {"available": False, "reason": "no state file"}
    if len(raw) > MAX_BYTES:
        return {"available": False, "reason": "state file implausibly large"}
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {"available": False, "reason": "state file unreadable"}
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return {"available": False, "reason": "unknown state schema"}

    age = _age_s(doc.get("updated_at"), now)
    if age is None:
        return {"available": False, "reason": "state file has no usable timestamp"}

    link = doc.get("link") if isinstance(doc.get("link"), dict) else {}
    tele = doc.get("telemetry") if isinstance(doc.get("telemetry"), dict) else {}
    coll = doc.get("collector") if isinstance(doc.get("collector"), dict) else {}
    counters = doc.get("counters") if isinstance(doc.get("counters"), dict) else {}

    def flag(value):
        # gps_locked is genuinely tri-state: true, false, or unevaluable. None
        # must survive as None -- collapsing it into false would report "no
        # satellite lock" for a detector that never said either way.
        return value if isinstance(value, bool) else None

    return {
        "available": True,
        "age_s": round(age, 1),
        "stale": age > MAX_AGE_S,
        "connected": flag(link.get("connected")),
        "compatible": flag(link.get("compatible")),
        "mode": coll.get("mode") if isinstance(coll.get("mode"), str) else None,
        "voltage": tele.get("voltage") if isinstance(tele.get("voltage"), (int, float))
                   and not isinstance(tele.get("voltage"), bool) else None,
        "gps_locked": flag(tele.get("gps_locked")),
        "alert_packets": counters.get("alert_packets")
                         if isinstance(counters.get("alert_packets"), int) else None,
        "alerts": _alerts(doc.get("alerts")),
    }


def _alerts(raw: Any) -> list[dict]:
    """The live alert list, with every enumerated field allowlisted."""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:8]:
        if not isinstance(item, dict):
            continue
        band = item.get("band")
        strength = item.get("strength")
        direction = item.get("direction")
        freq = item.get("frequency_ghz")
        out.append({
            "band": band if band in BANDS else "unknown",
            "strength": strength if isinstance(strength, int)
                        and not isinstance(strength, bool)
                        and 1 <= strength <= 8 else None,
            "direction": direction if direction in DIRECTIONS else "unknown",
            # Null for LASER and the multi-radar codes, which carry no frequency.
            "frequency_ghz": float(freq) if isinstance(freq, (int, float))
                             and not isinstance(freq, bool) else None,
            "muted": item.get("muted") if isinstance(item.get("muted"), bool) else None,
        })
    return out


def recent_alerts(path: Path = HISTORY_PATH, *, limit: int = MAX_ALERTS,
                  location: bool = False) -> list[dict]:
    """The most recent completed alerts from the sibling's history.

    Opened read-only through a URI so this can never migrate or write the
    sibling's database -- an older schema raises rather than being upgraded
    underneath its owner.

    Coordinates are withheld unless *location* is set, on the same switch that
    governs the vehicle's own position. The detector's database does record
    them, and a dashboard that leaked position through the radar panel while
    the telemetry panel withheld it would be a hole in one wall of the room.
    """
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT at, band, strength, frequency_ghz, direction, max_strength,"
            "       duration_s, lat, lon"
            "  FROM alert_events WHERE kind = 'alert_end'"
            "  ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out = []
    for r in rows:
        item = {
            "at": r["at"] if isinstance(r["at"], str) else None,
            "band": r["band"] if r["band"] in BANDS else "unknown",
            "strength": r["max_strength"] if isinstance(r["max_strength"], int)
                        else (r["strength"] if isinstance(r["strength"], int) else None),
            "frequency_ghz": r["frequency_ghz"]
                             if isinstance(r["frequency_ghz"], (int, float)) else None,
            "direction": r["direction"] if r["direction"] in DIRECTIONS else "unknown",
            "duration_s": r["duration_s"]
                          if isinstance(r["duration_s"], (int, float)) else None,
        }
        if location and isinstance(r["lat"], (int, float)) and isinstance(r["lon"], (int, float)):
            item["lat"] = r["lat"]
            item["lon"] = r["lon"]
        out.append(item)
    return out


def snapshot(*, location: bool = False, state_path: Path = STATE_PATH,
             history_path: Path = HISTORY_PATH,
             now: Optional[datetime] = None) -> dict:
    """Everything the dashboard shows about the detector."""
    state = read_state(state_path, now=now)
    state["recent"] = recent_alerts(history_path, location=location)
    return state
