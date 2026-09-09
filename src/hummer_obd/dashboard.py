"""Local, read-only browser view of the recorder's existing session files.

No serial connection, vehicle command, cloud upload, or location endpoint.
The listener defaults to loopback; use an SSH tunnel for remote viewing.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import analyze, live, r8
from .confidence import CONFIDENCE, LEVEL_NAMES

SESSION_NAME = re.compile(r"drive-\d{8}T\d{6}Z\.csv\Z")
MAX_SESSION_BYTES = 16 * 1024 * 1024
MAX_SESSION_ROWS = 20000
MAX_SESSIONS = 200
MAX_HISTORY = 600
# Only named recorder fields may leave this API. Raw identity transcripts are
# never part of the browser's data contract.
#
# Location is a separate, deliberate decision and defaults to WITHHELD. Fix
# quality (mode, satellites) says whether the receiver is working and reveals
# nothing about where the vehicle is; coordinates, altitude and heading say
# exactly that. The original author excluded all of it, and the default here
# keeps their behaviour: a reader that is merely started gets no position.
#
# --expose-location turns it on. It is a flag rather than a deletion so that
# publishing location is a visible act recorded in the command line that
# started the server, and so the safe behaviour survives someone reading this
# file and deleting the comment.
LOCATION_COLUMNS = ("gps_lat", "gps_lon", "gps_alt_m", "gps_speed_mps",
                    "gps_track_deg", "gps_epx_m", "gps_time")
FIX_QUALITY_COLUMNS = ("gps_mode", "gps_sats")


def public_columns(expose_location: bool = False) -> tuple[str, ...]:
    """Recorder fields permitted to leave this API."""
    allowed = set(FIX_QUALITY_COLUMNS)
    if expose_location:
        allowed |= set(LOCATION_COLUMNS)
    return tuple(c for c in live.drive.COLUMNS
                 if not c.startswith("gps_") or c in allowed)


#: Kept for callers that predate the flag; location withheld, as before.
PUBLIC_COLUMNS = public_columns(False)
CURRENT_FIELDS = {
    "pack_v": ("pack_v",), "pack_a": ("pack_a",),
    "pack_kw": ("pack_v", "pack_a"), "soc_pct": ("soc_pct",),
    "energy_kwh": ("energy_kwh",), "cell_avg_v": ("cell_avg_v",),
    "cell_spread_mv": ("cell_spread_mv", "cell_avg_v", "cell_min_v", "cell_max_v"),
    "series_cells": ("pack_v", "cell_avg_v"),
    "implied_kwh": ("energy_kwh", "soc_pct"),
    "speed_now_kph": ("speed_kph",),
    "volts_adapter": ("volts",), "volts_module": ("module_voltage",),
    "volts_dmc2": ("dmc2_v",),
}


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def _timestamp(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.timestamp() if stamp.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _utc(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _clean(value):
    """Strict JSON: unknown numeric quantities are null, never NaN/Infinity."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _valid(column: str, value) -> bool:
    if isinstance(value, (int, float)):
        if not _finite(value):
            return False
        check = analyze.SANITY_FILTERS.get(column)
        if check and not check(value):
            return False
        if column.endswith("_kph") and not 0 <= value <= 350:
            return False
        if column == "cell_spread_mv" and value < 0:
            return False
        # Coordinates have hard physical bounds, and a decoder fault here puts
        # the vehicle in the sea rather than producing an obviously silly
        # number. 0,0 is Null Island: a real place no vehicle of ours is in,
        # and the classic signature of a fix that failed open.
        if column == "gps_lat" and not -90.0 <= value <= 90.0:
            return False
        if column == "gps_lon" and not -180.0 <= value <= 180.0:
            return False
        if column == "gps_track_deg" and not 0.0 <= value < 360.0:
            return False
        if column in ("cell_min_v", "cell_max_v") and not 2 <= value <= 5:
            return False
        return True
    if column == "utc":
        return _timestamp(value) is not None
    # Raw fields are hex bytes, not arbitrary content from a CSV. This also
    # prevents a malformed source file becoming an accidental identity export.
    return (isinstance(value, str) and len(value) <= 256
            and bool(re.fullmatch(r"[0-9A-Fa-f ]+", value)))


def _history(rows: list[dict], *, location: bool = False) -> list[dict]:
    """Keep actual recent samples; no averaging across missing readings."""
    result = []
    for row in rows[-MAX_HISTORY:]:
        def reading(name):
            value = row.get(name)
            valid_row = name == "elapsed_s" or analyze.sane(row)
            return value if valid_row and _finite(value) and _valid(name, value) else None
        v, a = reading("pack_v"), reading("pack_a")
        point = {
            "elapsed_s": reading("elapsed_s"),
            "speed_kph": reading("speed_kph"),
            "pack_kw": v * a / 1000 if v is not None and a is not None else None,
            "soc_pct": reading("soc_pct"),
            "cell_spread_mv": reading("cell_spread_mv"),
        }
        if location:
            # Only when the operator asked for it. The trail is what a map is
            # drawn from, so it carries the same decision as the coordinates.
            point["gps_lat"] = reading("gps_lat")
            point["gps_lon"] = reading("gps_lon")
        result.append(point)
    return result


def energy_budget(rows: list[dict]) -> dict:
    """Separate observed moving energy from stationary accessory/charge load.

    This is sampled pack energy, not a wall-meter reading or an HVAC decoder.
    Every interval needs speed and power at both ends. Missing/invalid rows,
    clock reversals and large gaps break coverage instead of inventing energy.
    """
    periods = [b["elapsed_s"] - a["elapsed_s"] for a, b in zip(rows, rows[1:])
               if _finite(a.get("elapsed_s")) and _finite(b.get("elapsed_s"))
               and b["elapsed_s"] > a["elapsed_s"]]
    gap_limit = min(120.0, max(30.0, statistics.median(periods) * 3)) if periods else 30.0
    totals = dict.fromkeys(("observed_s", "moving_s", "stationary_s", "moving_drawn_kwh",
                           "moving_regen_kwh", "stationary_drawn_kwh", "stationary_energy_in_kwh"), 0.0)
    skipped = 0

    def pair(row):
        if not analyze.sane(row):
            return None
        v, a = row.get("pack_v"), row.get("pack_a")
        if not _finite(v) or not _finite(a) or not _valid("pack_v", v):
            return None
        speed = row.get("speed_kph")
        if not _finite(speed) or not _valid("speed_kph", speed):
            wheels = [row.get(c) for c in analyze._WHEEL_COLUMNS]
            wheels = [s for s in wheels if _finite(s) and 0 <= s <= 350]
            speed = statistics.mean(wheels) if len(wheels) == 4 else None
        return (v * a / 1000.0, speed) if speed is not None else None

    for before, after in zip(rows, rows[1:]):
        t0, t1 = before.get("elapsed_s"), after.get("elapsed_s")
        a, b = pair(before), pair(after)
        if (not _finite(t0) or not _finite(t1) or not 0 < t1 - t0 <= gap_limit
                or a is None or b is None):
            skipped += 1
            continue
        dt = t1 - t0
        p0, speed0 = a
        p1, speed1 = b
        if p0 * p1 < 0:
            # Linear zero crossing: two triangles, each with its own width.
            span = abs(p1 - p0)
            drawn = max(p0, p1) ** 2 / (2 * span) * dt / 3600
            returned = min(p0, p1) ** 2 / (2 * span) * dt / 3600
        else:
            mean = (p0 + p1) / 2
            drawn, returned = max(mean, 0) * dt / 3600, max(-mean, 0) * dt / 3600
        moving = max(speed0, speed1) >= 1
        totals["observed_s"] += dt
        totals["moving_s" if moving else "stationary_s"] += dt
        totals["moving_drawn_kwh" if moving else "stationary_drawn_kwh"] += drawn
        totals["moving_regen_kwh" if moving else "stationary_energy_in_kwh"] += returned
    covered = totals["observed_s"] > 0
    return {**{k: round(v, 5) if covered else None for k, v in totals.items()},
            "skipped_intervals": skipped}


def _insights(report: dict, derived: dict, signals: dict) -> list[dict]:
    result = []
    resistance = derived.get("resistance")
    if resistance and resistance[0] is not None and resistance[2] is not None:
        result.append({"title": "Pack response under load", "level": "info",
                       "detail": f"Estimated {resistance[0]:.2f} mΩ from "
                                 f"{resistance[1]} current steps (r={resistance[2]:.3f}). "
                                 "A session estimate, not a battery health diagnosis."})
    capacity = derived.get("implied_kwh")
    if capacity is not None:
        result.append({"title": "Energy / state-of-charge cross-check", "level": "info",
                       "detail": f"These paired readings imply {capacity:.1f} kWh at "
                                 "100%. This is not a measured capacity or degradation test."})
    missing = sum(s["status"] in ("missing", "invalid", "stale")
                  for c, s in signals.items() if c not in live.BOOKKEEPING)
    if missing:
        result.append({"title": "Signal coverage", "level": "notice",
                       "detail": f"{missing} fields are missing, invalid, or stale. "
                                 "See the signal table for the last answer and its source."})
    sampling = report.get("sampling", {})
    if sampling.get("gaps"):
        result.append({"title": "Capture gaps", "level": "notice",
                       "detail": "The recorder missed intervals. Integrated energy and "
                                 "distance cover observed intervals only."})
    return result


class SessionStore:
    """One cached session, bounded files and responses for a small Pi."""

    def __init__(self, directory: str | Path, stale_after: float = 45.0,
                 *, expose_location: bool = False):
        if not math.isfinite(stale_after) or stale_after <= 0:
            raise ValueError("stale-after must be positive and finite")
        self.directory = Path(directory).resolve()
        self.stale_after = stale_after
        self.expose_location = bool(expose_location)
        self.columns = public_columns(self.expose_location)
        self._lock = threading.Lock()
        self._cache_key = None
        self._cached = None

    def _paths(self) -> list[Path]:
        candidates = []
        for path in self.directory.glob("drive-*.csv"):
            try:
                if SESSION_NAME.fullmatch(path.name) and not path.is_symlink() and path.is_file():
                    candidates.append((path.stat().st_mtime_ns, path.name, path))
            except OSError:
                continue
        return [p for _, _, p in sorted(candidates, reverse=True)[:MAX_SESSIONS]]

    def sessions(self) -> dict:
        sessions = []
        for path in self._paths():
            try:
                sessions.append({"id": path.name, "modified_utc": _utc(path.stat().st_mtime)})
            except OSError:
                continue
        return {"sessions": sessions, "latest": sessions[0]["id"] if sessions else None}

    def snapshot(self, session: str = "latest", *, now: float | None = None) -> dict:
        with self._lock:
            return self._snapshot(session, time.time() if now is None else now)

    def _snapshot(self, session: str, now: float) -> dict:
        paths = self._paths()
        if session != "latest" and not SESSION_NAME.fullmatch(session):
            raise ValueError("invalid session id")
        path = next((p for p in paths if p.name == session), None) if session != "latest" else (
            paths[0] if paths else None)
        if path is None:
            if session != "latest":
                raise FileNotFoundError("session not found")
            return {"schema": 1, "session": {"id": None, "rows": 0, "status": "empty",
                    "newest_utc": None, "elapsed_s": None, "age_s": None, "period_s": None},
                    "signals": {}, "derived": {}, "report": {}, "history": [],
                    "warnings": ["No recorded sessions yet."], "insights": [],
                    "energy_budget": energy_budget([])}
        stat = path.stat()
        if stat.st_size > MAX_SESSION_BYTES:
            raise ValueError("session exceeds the 16 MiB dashboard limit; use offline analysis")
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        if key != self._cache_key:
            # Bound memory before the shared CSV reader materializes rows.
            with path.open("rb") as handle:
                for index, _ in enumerate(handle):
                    if index > MAX_SESSION_ROWS:
                        raise ValueError("session exceeds dashboard row limit; use offline analysis")
            rows, warnings, _ = analyze.read_session(path)
            # Drop fields outside the contract before any report or derived
            # computation, including completeness keys from future CSV columns.
            rows = [{c: r.get(c) for c in self.columns if c in r} for r in rows]
            # A complete row with no usable time cannot support a live view.
            if any(not _finite(r.get("elapsed_s")) for r in rows):
                warnings.append("Rows with invalid elapsed time were withheld from measurements.")
                rows = [r if _finite(r.get("elapsed_s")) else
                        {"utc": r.get("utc"), "elapsed_s": None} for r in rows]
            # Preserve time slots even for transition/invalid rows, so offline
            # integration cannot join across samples the dashboard rejected.
            report_rows = [
                {c: v if c in live.BOOKKEEPING or (analyze.sane(r) and _valid(c, v)) else None
                 for c, v in r.items()} for r in rows
            ]
            report = analyze.analyze(report_rows, path=path.name, extra_warnings=warnings)
            derived = _clean(live.derive(rows))
            snap = live.snapshot(rows)
            self._cached = (rows, warnings, report, derived, snap,
                            _history(rows, location=self.expose_location),
                            energy_budget(rows))
            self._cache_key = key
        rows, read_warnings, report, base_derived, snap, history, budget = self._cached
        warnings = list(read_warnings)
        newest = snap.get("newest_utc")
        stamp = _timestamp(newest)
        age = now - stamp if stamp is not None else None
        clock_bad = age is None or age < -5
        if clock_bad and rows:
            warnings.append("The session timestamp cannot establish freshness; check the node clock.")
        age = max(0.0, age) if age is not None else None
        historical = session != "latest"
        status = ("empty" if not rows else "historical" if historical else
                  "stale" if clock_bad or age > self.stale_after else "live")
        sources = live.column_sources()
        signals = {}
        for column in self.columns:
            item = snap.get("columns", {}).get(column, {})
            value = item.get("value")
            missing = value is None or value == ""
            valid = not missing and _valid(column, value)
            relative_age = item.get("age_s")
            signal_age = (relative_age + age if _finite(relative_age) and relative_age >= 0
                          and age is not None else None)
            if historical:
                signal_age = relative_age if _finite(relative_age) and relative_age >= 0 else None
            state = ("missing" if missing else "invalid" if not valid else
                     "stale" if (clock_bad and not historical) or signal_age is None
                     or signal_age > self.stale_after else "fresh")
            source, identifier = sources.get(column, ("unknown", ""))
            evidence = CONFIDENCE.get(identifier.removeprefix("0x"))
            label, unit = live.LABELS.get(column, (column.replace("_", " "), ""))
            raw = column.endswith("_raw") or column.startswith("array_")
            signals[column] = {**item, "value": value if valid else None,
                               "age_s": signal_age, "status": state,
                               "label": label, "unit": unit,
                               "source": source, "identifier": identifier,
                               "confidence": evidence.level if evidence and not raw else None,
                               "confidence_label": "raw / unscaled" if raw else
                               LEVEL_NAMES[evidence.level] if evidence else "recorded"}
        derived = dict(base_derived)
        # Current-value tiles may not silently carry old/invalid data forward.
        # Historical sessions retain their final readings, explicitly labelled
        # historical by the session envelope and signal ages.
        for field, dependencies in CURRENT_FIELDS.items():
            allowed = ("fresh", "stale") if historical else ("fresh",)
            if any(signals.get(c, {}).get("status") not in allowed for c in dependencies):
                derived[field] = None
        # Fresh individual sensors do not prove a fresh *pair*: alternating
        # missing replies can keep both columns current while their last shared
        # row is old. Apply freshness to the actual pair used by derive().
        for field in ("pack_kw", "series_cells", "implied_kwh"):
            if historical or derived.get(field) is None:
                continue
            dependencies = CURRENT_FIELDS[field]
            pair_row = next((r for r in reversed(rows) if analyze.sane(r)
                             and all(_finite(r.get(c)) and _valid(c, r[c]) for c in dependencies)), None)
            elapsed = snap.get("elapsed_s")
            paired_age = (elapsed - pair_row["elapsed_s"] + age
                          if pair_row is not None and _finite(elapsed) and age is not None else None)
            if paired_age is None or not 0 <= paired_age <= self.stale_after:
                derived[field] = None
        if derived.get("resistance"):
            resistance, steps, correlation = derived["resistance"]
            if resistance is None or resistance <= 0 or correlation is None or correlation > -0.5:
                derived["resistance"] = None
                warnings.append("The current-step fit does not support a reliable resistance estimate.")
        return _clean({"schema": 1,
                       "session": {"id": path.name, "rows": len(rows), "newest_utc": newest,
                                   "elapsed_s": snap.get("elapsed_s"), "age_s": age,
                                   "period_s": snap.get("period_s"), "status": status},
                       "signals": signals, "derived": derived, "report": report,
                       "history": history, "energy_budget": budget,
                       "warnings": list(dict.fromkeys(warnings + report.get("warnings", []))),
                       "insights": _insights(report, derived, signals)})


def make_server(store: SessionStore, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, value, content_type="application/json; charset=utf-8"):
            body = value if isinstance(value, bytes) else json.dumps(_clean(value), allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                             "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                             "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            # Refuse DNS-rebinding hostnames. A deliberate interface bind may
            # be reached using that IP; loopback also permits localhost.
            accepted = {self.server.server_address[0], "127.0.0.1", "localhost"}
            hostname = self.headers.get("Host", "").split(":", 1)[0]
            if hostname not in accepted:
                self._reply(403, {"error": "host not allowed; use the listener IP or localhost"})
                return
            url = urlsplit(self.path)
            try:
                if url.path == "/":
                    self._reply(200, files("hummer_obd").joinpath("dashboard.html").read_bytes(),
                                "text/html; charset=utf-8")
                elif url.path == "/api/r8":
                    # The detector is a separate project on the same Pi. Its
                    # state is read here rather than fetched by the browser
                    # because this page's CSP allows connections to its own
                    # origin only -- which is the right default, and means
                    # cross-project data has to come through this server.
                    self._reply(200, r8.snapshot(location=store.expose_location))
                elif url.path == "/api/sessions":
                    self._reply(200, store.sessions())
                elif url.path == "/api/snapshot":
                    query = parse_qs(url.query, max_num_fields=4)
                    if set(query) - {"session"} or len(query.get("session", [])) > 1:
                        raise ValueError("expected one session parameter")
                    self._reply(200, store.snapshot(query.get("session", ["latest"])[0]))
                else:
                    self._reply(404, {"error": "not found"})
            except FileNotFoundError:
                self._reply(404, {"error": "session not found"})
            except (ValueError, OverflowError):
                self._reply(400, {"error": "invalid or oversized session; use offline analysis"})
            except OSError:
                self._reply(503, {"error": "session temporarily unavailable"})

        def log_message(self, fmt, *args):
            # A local telemetry reader does not need a log of trip selections.
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default="evidence/sessions")
    parser.add_argument("--host", default="127.0.0.1", help="listener IP (default: loopback)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--stale-after", type=float, default=45.0)
    parser.add_argument("--json", action="store_true", help="print a snapshot and exit without a listener")
    parser.add_argument("--session", default="latest", help="session for --json, default latest")
    parser.add_argument(
        "--expose-location", action="store_true",
        help="serve GPS coordinates, altitude and heading to the browser. Off "
             "by default: fix quality alone says whether the receiver works "
             "without saying where the vehicle is. Only turn this on for a "
             "listener you control -- check --host.")
    args = parser.parse_args(argv)
    try:
        store = SessionStore(args.dir, args.stale_after,
                             expose_location=args.expose_location)
        if args.json:
            print(json.dumps(store.snapshot(args.session), indent=2, allow_nan=False))
            return 0
        with make_server(store, args.host, args.port) as server:
            print(f"Hummer telemetry: http://{args.host}:{server.server_port} (read-only)", flush=True)
            server.serve_forever()
    except (OSError, ValueError) as exc:
        parser.exit(2, f"dashboard: {exc}\n")
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
