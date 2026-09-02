"""Status screen for the Waveshare 2.13-inch V4 (250x122, monochrome) panel.

Design constraints, in order of importance:

* **Panel wear.**  E-paper degrades with refreshes.  The renderer produces one
  image; the driver is only asked to update when the rendered content actually
  changed, at a floor of ``display.refresh_interval_s`` seconds, and the panel
  is put to sleep between updates.
* **Ghosting.**  Only full refreshes are used (no partial-update mode), which
  is the conservative choice for a screen that changes a few times an hour.
* **Legibility.**  250x122 pixels is small: six short lines, left-aligned,
  no chrome, no graphics.

The renderer is hardware-free and testable: :func:`render_status_image`
returns a PIL image, and ``--simulate`` writes it to a PNG.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

WIDTH = 250
HEIGHT = 122
R8_STATE_PATH = Path("/home/jeremy/unidenr8/.state/state.json")
R8_STATE_SCHEMA = 1
R8_STATE_MAX_BYTES = 64 * 1024
R8_STATE_MAX_AGE_SECONDS = 90.0

_R8_STATUSES = frozenset({
    "starting", "obd-blocked", "connecting", "incompatible", "degraded",
    "streaming", "reconnecting", "stopped",
})
_R8_BANDS = frozenset({
    "K", "KA", "X", "LASER", "MRCD", "MRCT", "RT3", "RT4", "K POP", "KA POP",
})
_R8_DIRECTIONS = frozenset({"front", "side", "rear", "unknown"})

__all__ = [
    "R8_STATE_PATH",
    "StatusData",
    "gather_status",
    "read_r8_display_line",
    "render_status_image",
    "main",
]


def _run(cmd: list[str], timeout: float = 4.0) -> str:
    """Run *cmd* and return stdout, or "" if it is unavailable or fails."""
    if not shutil.which(cmd[0]):
        return ""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


@dataclass
class StatusData:
    hostname: str = ""
    ssid: str = ""
    signal: str = ""
    lan_ip: str = ""
    tailscale_ip: str = ""
    uptime: str = ""
    temperature: str = ""
    r8_state: str = ""
    obd_state: str = "unknown"
    updated: str = ""

    def as_lines(self) -> list[str]:
        wifi = self.ssid or "no wifi"
        if self.signal:
            wifi = f"{wifi} {self.signal}"
        return [
            self.hostname or "(unknown host)",
            f"wifi {wifi}",
            f"lan  {self.lan_ip or '-'}",
            self.r8_state or f"ts   {self.tailscale_ip or '-'}",
            f"up {self.uptime or '-'}   {self.temperature or '-'}",
            f"obd  {self.obd_state}",
        ]


def _read_uptime() -> str:
    try:
        seconds = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return ""
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def _read_temperature() -> str:
    for path in ("/sys/class/thermal/thermal_zone0/temp",):
        try:
            milli = int(Path(path).read_text().strip())
        except (OSError, ValueError):
            continue
        return f"{milli / 1000:.0f}C"
    out = _run(["vcgencmd", "measure_temp"])
    match = re.search(r"([\d.]+)", out)
    return f"{float(match.group(1)):.0f}C" if match else ""


def _read_wifi() -> tuple[str, str]:
    """Return (ssid, signal) using NetworkManager, falling back to iw."""
    out = _run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "device", "wifi"])
    for line in out.splitlines():
        parts = line.split(":")
        if parts and parts[0] == "yes" and len(parts) >= 3:
            return parts[1], f"{parts[2]}%"
    out = _run(["iw", "dev", "wlan0", "link"])
    ssid = re.search(r"SSID:\s*(.+)", out)
    signal = re.search(r"signal:\s*(-?\d+)", out)
    return (ssid.group(1).strip() if ssid else "",
            f"{signal.group(1)}dBm" if signal else "")


def _read_lan_ip() -> str:
    out = _run(["ip", "-4", "-o", "addr", "show", "scope", "global"])
    for line in out.splitlines():
        if " tailscale" in line:
            continue
        match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", line)
        if match:
            return match.group(1)
    return ""


def _read_tailscale_ip() -> str:
    out = _run(["tailscale", "ip", "-4"])
    return out.splitlines()[0].strip() if out else ""


def _read_obd_state(cfg=None) -> str:
    """Describe the OBD link without touching the bus.

    The display never transmits.  It reports what is observable: whether the
    RFCOMM device exists, and whether the collector service is running.
    """
    device = "/dev/rfcomm0"
    if cfg is not None:
        device = cfg.adapter.device
    bound = Path(device).exists()
    active = _run(["systemctl", "is-active", "hummer-collector.service"]) == "active"
    if active and bound:
        return "collecting"
    if bound:
        return f"{Path(device).name} bound"
    return "not bound"


def _read_small_json(path: Path) -> Any:
    """Read one bounded JSON document, or raise a normal parse/I/O error."""
    with path.open("r", encoding="utf-8") as handle:
        payload = handle.read(R8_STATE_MAX_BYTES + 1)
    if len(payload) > R8_STATE_MAX_BYTES:
        raise ValueError("R8 state exceeds the display input limit")
    return json.loads(payload)


def _r8_document_age(document: dict[str, Any], now: datetime) -> float | None:
    stamp = document.get("updated_at")
    if not isinstance(stamp, str):
        return None
    try:
        updated = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now.astimezone(timezone.utc) - updated).total_seconds()


def _r8_voltage(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "--V"
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 30.0:
        return "--V"
    return f"{number:.1f}V"


def _r8_alert_line(alerts: Any, voltage: str) -> str | None:
    if not isinstance(alerts, list) or not alerts:
        return None
    alert = alerts[0]
    if not isinstance(alert, dict):
        return f"r8 {voltage} alert"

    band = alert.get("band")
    strength = alert.get("strength")
    direction = alert.get("direction")
    if (
        not isinstance(band, str)
        or band not in _R8_BANDS
        or isinstance(strength, bool)
        or not isinstance(strength, int)
        or not 0 <= strength <= 8
        or not isinstance(direction, str)
        or direction not in _R8_DIRECTIONS
    ):
        return f"r8 {voltage} alert"
    suffix = "" if direction == "unknown" else f" {direction}"
    return f"r8 {voltage} {band} {strength}/8{suffix}"


def read_r8_display_line(
    path: str | os.PathLike[str] = R8_STATE_PATH,
    *,
    now: datetime | None = None,
    max_age_s: float = R8_STATE_MAX_AGE_SECONDS,
) -> str:
    """Build a safe display line from the collector's untrusted state file.

    No free-form value from the file is rendered. Missing, malformed, future,
    or unknown-schema input falls back to the existing Tailscale line. A valid
    but old document gets a fixed stale marker instead of stale telemetry.
    """
    try:
        document = _read_small_json(Path(path))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return ""
    if not isinstance(document, dict) or document.get("schema") != R8_STATE_SCHEMA:
        return ""

    age = _r8_document_age(document, now or datetime.now(timezone.utc))
    if age is None or age < -30.0:
        return ""
    if age > max_age_s:
        return "r8 stale"

    collector = document.get("collector")
    obd = document.get("obd")
    link = document.get("link")
    telemetry = document.get("telemetry")
    if not all(isinstance(value, dict) for value in (collector, obd, link, telemetry)):
        return ""

    status = collector.get("status")
    if not isinstance(status, str) or status not in _R8_STATUSES:
        return ""
    if obd.get("healthy") is False:
        return "r8 paused: obd"
    if obd.get("healthy") is not True:
        return ""
    if status == "stopped":
        return "r8 stopped"
    if status in {"starting", "connecting", "reconnecting"}:
        return "r8 connecting"
    if status == "obd-blocked":
        return "r8 paused: obd"
    if status == "incompatible":
        return "r8 incompatible"
    if status == "degraded":
        return "r8 degraded"
    if link.get("connected") is not True:
        return "r8 disconnected"
    if link.get("compatible") is not True:
        return "r8 incompatible"
    if telemetry.get("stale") is not False:
        return "r8 data stale"

    voltage = _r8_voltage(telemetry.get("voltage"))
    alerts = document.get("alerts")
    if not isinstance(alerts, list):
        return ""
    alert_line = _r8_alert_line(alerts, voltage)
    if alert_line:
        return alert_line
    gps_locked = telemetry.get("gps_locked")
    if not isinstance(gps_locked, bool):
        return "r8 data invalid"
    gps = "GPS" if gps_locked is True else "no-fix"
    return f"r8 {voltage} {gps} clear"


def gather_status(cfg=None) -> StatusData:
    ssid, signal = _read_wifi()
    return StatusData(
        hostname=socket.gethostname(),
        ssid=ssid,
        signal=signal,
        lan_ip=_read_lan_ip(),
        tailscale_ip=_read_tailscale_ip(),
        uptime=_read_uptime(),
        temperature=_read_temperature(),
        r8_state=read_r8_display_line(),
        obd_state=_read_obd_state(cfg),
        updated=datetime.now(timezone.utc).strftime("%H:%MZ"),
    )


def _load_font(size: int):
    from PIL import ImageFont

    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def render_status_image(status: StatusData, *, rotate: int = 0):
    """Render *status* to a 1-bit PIL image sized for the panel."""
    from PIL import Image, ImageDraw

    image = Image.new("1", (WIDTH, HEIGHT), 255)  # 255 = white
    draw = ImageDraw.Draw(image)
    title_font = _load_font(16)
    body_font = _load_font(13)

    lines = status.as_lines()
    draw.text((2, 0), lines[0], font=title_font, fill=0)
    draw.line((0, 19, WIDTH - 1, 19), fill=0)
    y = 23
    for line in lines[1:]:
        draw.text((2, y), line, font=body_font, fill=0)
        y += 16
    footer = status.updated
    if footer:
        draw.text((2, HEIGHT - 14), footer, font=body_font, fill=0)
    if rotate:
        image = image.rotate(rotate, expand=False)
    return image


class PanelWriter:
    """Thin wrapper over the official Waveshare ``epd2in13_V4`` driver."""

    def __init__(self, *, sleep_between_updates: bool = True, logger=print) -> None:
        self.sleep_between_updates = sleep_between_updates
        self.log = logger
        self._epd = None

    def _driver(self):
        if self._epd is None:
            from waveshare_epd import epd2in13_V4  # type: ignore

            self._epd = epd2in13_V4.EPD()
        return self._epd

    def show(self, image) -> None:
        epd = self._driver()
        # init() after sleep() is a full reset; the V4 panel needs it before
        # every update once it has been put to sleep.
        epd.init()
        # The panel is 122x250 natively and this layout is 250x122; the
        # official driver's getbuffer() rotates a landscape image itself, so
        # it is handed over unrotated.
        epd.display(epd.getbuffer(image))
        if self.sleep_between_updates:
            epd.sleep()

    def clear(self) -> None:
        epd = self._driver()
        epd.init()
        epd.Clear(0xFF)
        if self.sleep_between_updates:
            epd.sleep()


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Hummer Pi e-paper status display")
    parser.add_argument("--config", help="path to hummer.toml")
    parser.add_argument("--root", default=".")
    parser.add_argument("--simulate", metavar="PNG",
                        help="render to this PNG instead of the panel")
    parser.add_argument("--once", action="store_true", help="render a single frame and exit")
    parser.add_argument("--interval", type=float, help="seconds between updates")
    parser.add_argument("--clear", action="store_true", help="clear the panel and exit")
    args = parser.parse_args(argv)

    cfg = None
    try:
        from ..config import load_config

        cfg = load_config(args.config, root=args.root) if args.config else load_config(root=args.root)
    except Exception as exc:  # configuration problems must not blank the screen
        print(f"warning: using defaults ({exc})")

    interval = args.interval or (cfg.display.refresh_interval_s if cfg else 300.0)
    simulate = args.simulate or (cfg.display.simulate_path if cfg else "")
    rotate = cfg.display.rotate if cfg else 0
    writer = None if simulate else PanelWriter(
        sleep_between_updates=cfg.display.sleep_between_updates if cfg else True
    )

    if args.clear:
        if writer is None:
            print("--clear needs the real panel (no --simulate)")
            return 1
        writer.clear()
        return 0

    last_lines: Optional[list[str]] = None
    while True:
        status = gather_status(cfg)
        lines = status.as_lines()
        image = render_status_image(status, rotate=rotate)
        if simulate:
            Path(simulate).parent.mkdir(parents=True, exist_ok=True)
            image.save(simulate)
            print(f"rendered {simulate}: {' | '.join(lines)}")
        elif lines != last_lines:
            writer.show(image)
            print(f"panel updated: {' | '.join(lines)}")
        else:
            print("unchanged; panel not refreshed")
        last_lines = lines
        if args.once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
