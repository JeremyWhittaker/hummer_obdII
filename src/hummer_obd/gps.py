"""Position, speed and satellite time from ``gpsd``, for the session recorder.

Reads gpsd's JSON protocol on a background thread and hands the newest fix to
whoever asks.  It is built to be ignorable: the recorder's job is the vehicle,
and a GPS that is unplugged, unfixed or broken must cost it nothing.  Every
public call here is non-blocking and swallows its own errors.

The parsing is ported from the sibling project ``unidenr8``
(``src/uniden_r8/gnss.py``), which solved the same problem against the same
receiver -- a BU-353S4, SiRF Star IV over a PL2303 bridge at 4800 baud.  Two of
its lessons are carried over verbatim because they are not obvious:

* **gpsd renamed its altitude field.**  ``altMSL`` is current, ``altHAE`` is
  height above ellipsoid, and ``alt`` is the old spelling.  A reader that knows
  only one of them silently records no altitude against some gpsd versions.
* **Staleness is measured on the monotonic clock, never the wall clock.**  This
  Pi has no RTC, so wall time can step by days when NTP or a GPS fix lands.  A
  staleness check against wall time would call a fresh fix ancient, or an
  ancient one fresh, precisely during the boot window that matters most.

What is deliberately different: unidenr8 is asyncio and this recorder is not,
so the transport is a blocking socket on its own thread rather than a
coroutine.  And where unidenr8 reports only *that* GPS is unavailable, this
reports *why* -- gpsd unreachable, gpsd up but serving no device, or a device
with no fix are three different faults with three different fixes, and its
runbook documents having to tell them apart by hand.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any, Callable, Optional

#: gpsd's standard port.  The sibling project documents a stopgap instance on
#: 2948 for when ``/etc/default/gpsd`` cannot be edited; that is a temporary
#: arrangement which does not survive a reboot, and this defaults to the real
#: one rather than encoding somebody's workaround.
DEFAULT_HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 2947

#: Sent once per connection to make gpsd stream JSON.
WATCH: bytes = b'?WATCH={"enable":true,"json":true}\n'

#: A fix older than this is not reported.  gpsd emits about 1 Hz, so five
#: seconds is several missed reports rather than a tight margin.
STALE_AFTER_S: float = 5.0

#: Reconnect backoff, doubling to a cap, then jittered.  A receiver that is
#: flapping should not settle into a fixed retry rhythm.
BACKOFF_BASE_S: float = 2.0
BACKOFF_CAP_S: float = 60.0

#: A single JSON line longer than this means the stream is corrupt, not that
#: gpsd had a lot to say.
MAX_LINE_BYTES: int = 64 * 1024

#: The columns this module contributes to a session row.
COLUMNS: tuple[str, ...] = (
    "gps_mode", "gps_lat", "gps_lon", "gps_alt_m",
    "gps_speed_mps", "gps_track_deg", "gps_sats", "gps_epx_m", "gps_time",
)

#: What each mode value means, for humans reading a report.
MODE_NAMES: dict[int, str] = {0: "unknown", 1: "no fix", 2: "2D fix", 3: "3D fix"}


def _number(value: Any) -> Optional[float]:
    """A float from a gpsd field, or ``None``.

    Rejects ``bool`` explicitly.  In Python ``bool`` is a subclass of ``int``,
    so ``isinstance(True, int)`` is true and a boolean field would otherwise be
    recorded as 1.0 -- a real value where gpsd meant a flag.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _first(report: dict, *keys: str) -> Optional[float]:
    """The first key present with a numeric value.

    gpsd has renamed fields across versions; asking for one spelling only is
    how a reader silently records nothing against half the installed base.
    """
    for key in keys:
        value = _number(report.get(key))
        if value is not None:
            return value
    return None


def parse_tpv(report: dict) -> dict:
    """The useful parts of a gpsd ``TPV`` (time-position-velocity) report."""
    mode = report.get("mode")
    return {
        "mode": int(mode) if isinstance(mode, int) and not isinstance(mode, bool) else 0,
        "lat": _number(report.get("lat")),
        "lon": _number(report.get("lon")),
        "alt_m": _first(report, "altMSL", "altHAE", "alt"),
        "speed_mps": _number(report.get("speed")),
        "track_deg": _first(report, "track", "magtrack"),
        "epx_m": _number(report.get("epx")),
        # Kept as the string gpsd sent.  It comes from the satellites, which
        # makes it the one clock on this node that does not depend on either
        # the network or a battery-backed chip the Pi does not have.
        "time": report.get("time") if isinstance(report.get("time"), str) else None,
    }


def parse_sky(report: dict) -> Optional[int]:
    """Satellites *used* in the solution, not merely visible.

    Visible climbs first and means little; used is what a fix rests on.  The
    sibling project measured this receiver using exactly four in every tested
    condition -- the minimum for 3D -- so losing one satellite loses the fix.
    """
    used = _number(report.get("uSat"))
    if used is not None:
        return int(used)
    sats = report.get("satellites")
    if isinstance(sats, list):
        return sum(1 for s in sats if isinstance(s, dict) and s.get("used") is True)
    return None


class GpsReader:
    """Keeps the newest gpsd fix available without ever blocking the caller.

    Start it once, call :meth:`columns` per session row.  It owns a thread, a
    socket and its own failures; nothing it does can raise into the recorder.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        stale_after_s: float = STALE_AFTER_S,
        clock: Callable[[], float] = time.monotonic,
        connect: Optional[Callable[[str, int, float], Any]] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.stale_after_s = stale_after_s
        self._clock = clock
        self._connect = connect or (
            lambda h, p, t: socket.create_connection((h, p), timeout=t))
        self._lock = threading.Lock()
        self._tpv: dict = {}
        self._sats: Optional[int] = None
        self._at: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        #: Transport state: whether we can talk to gpsd at all.
        self.status: str = "not started"
        #: Why there is no FIX, which is a different question from whether the
        #: socket is up.  Kept apart because the transport message is the one
        #: that gets overwritten most often -- every reconnect rewrites it --
        #: and it would otherwise erase the specific diagnosis ("no device",
        #: "cold start") that the caller actually needs.
        self.reason: str = ""
        self.has_device: bool = False
        self.malformed: int = 0

    # -- lifecycle -------------------------------------------------------
    def start(self) -> "GpsReader":
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="gpsd-reader", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    # -- what the recorder asks for --------------------------------------
    def fix(self) -> Optional[dict]:
        """The newest fix, or ``None`` if there is not a usable recent one."""
        with self._lock:
            at, tpv, sats = self._at, dict(self._tpv), self._sats
        if at is None or not tpv:
            return None
        if self._clock() - at > self.stale_after_s:
            return None
        if tpv.get("mode", 0) < 2:
            return None
        tpv["sats"] = sats
        return tpv

    def columns(self) -> dict:
        """The session-row fields for this cycle.

        Always returns every column.  A row that omits them when the GPS is
        quiet is indistinguishable from a row recorded before GPS existed, and
        the difference matters when reading a session back.
        """
        fix = self.fix()
        if fix is None:
            return {name: None for name in COLUMNS}
        return {
            "gps_mode": fix.get("mode"),
            "gps_lat": fix.get("lat"),
            "gps_lon": fix.get("lon"),
            "gps_alt_m": fix.get("alt_m"),
            "gps_speed_mps": fix.get("speed_mps"),
            "gps_track_deg": fix.get("track_deg"),
            "gps_sats": fix.get("sats"),
            "gps_epx_m": fix.get("epx_m"),
            "gps_time": fix.get("time"),
        }

    def describe(self) -> str:
        """One line saying what the GPS is doing, including why it is not.

        Prefers the most specific thing known: a fix, else the reason there is
        no fix, else the transport state.  A caller reading "gpsd closed the
        connection" when the real answer is "no receiver attached" has been
        told the truth and learned nothing.
        """
        fix = self.fix()
        if fix is not None:
            mode = MODE_NAMES.get(fix.get("mode", 0), "?")
            sats = fix.get("sats")
            return f"{mode}, {sats if sats is not None else '?'} satellites used"
        return self.reason or self.status

    # -- the thread ------------------------------------------------------
    def _run(self) -> None:  # pragma: no cover - exercised via _session
        attempt = 0
        while not self._stop.is_set():
            try:
                self.status = "connecting to gpsd"
                self._session()
                attempt = 0
            except Exception as exc:
                self.status = f"gpsd unreachable ({type(exc).__name__})"
            if self._stop.is_set():
                return
            delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** min(attempt, 5)))
            attempt += 1
            self._stop.wait(delay)

    def _session(self) -> None:
        sock = self._connect(self.host, self.port, 10.0)
        try:
            sock.sendall(WATCH)
            buf = b""
            while not self._stop.is_set():
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    self.status = "gpsd closed the connection"
                    return
                buf += chunk
                if len(buf) > MAX_LINE_BYTES:
                    self.malformed += 1
                    self.status = "gpsd stream corrupt (oversized line)"
                    return
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._consume(line)
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _consume(self, line: bytes) -> None:
        if not line.strip():
            return
        try:
            report = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            self.malformed += 1
            return
        if not isinstance(report, dict):
            self.malformed += 1
            return
        kind = report.get("class")
        if kind == "TPV":
            tpv = parse_tpv(report)
            with self._lock:
                self._tpv = tpv
                self._at = self._clock()
            mode = tpv.get("mode", 0)
            self.reason = ("" if mode >= 2 else
                           "device present, no fix yet "
                           "(cold start is not a fault)")
        elif kind == "SKY":
            used = parse_sky(report)
            if used is not None:
                with self._lock:
                    self._sats = used
        elif kind == "DEVICES":
            # The fault the sibling project could only diagnose by hand: gpsd
            # answers on its port whether or not it has a receiver, so "no
            # device" and "no sky view" look identical from the client unless
            # this is checked.
            devices = report.get("devices")
            self.has_device = bool(isinstance(devices, list) and devices)
            self.reason = ("" if self.has_device
                           else "gpsd running but serving no device")
