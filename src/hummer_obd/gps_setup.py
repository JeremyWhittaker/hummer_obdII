"""Prepare a stable alias and Debian gpsd hotplug rule for the fitted USB GPS.

Preparation never opens a serial port or changes system state. The guarded
root installer applies the files; both telemetry consumers keep using gpsd.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import socket
import subprocess
import time

ALIAS = Path("/dev/hummer_gps")
HOTPLUG_UNIT = "gpsdctl@hummer_gps.service"
# This receiver has no unique USB serial. Match its measured adapter profile;
# never claim arbitrary USB serial adapters as GPS devices.
IDENTITY = {
    "ID_BUS": "usb",
    "ID_VENDOR_ID": "067b",
    "ID_MODEL_ID": "2303",
    "ID_SERIAL": "Prolific_Technology_Inc._USB-Serial_Controller_D",
    "ID_REVISION": "0400",
    "ID_USB_INTERFACE_NUM": "00",
    "ID_USB_DRIVER": "pl2303",
}


@dataclass(frozen=True)
class Receiver:
    node: Path
    sysfs: Path


def properties(path: Path) -> dict[str, str]:
    result = subprocess.run(
        ["udevadm", "info", "--query=property", f"--path={path}"],
        check=True, capture_output=True, text=True, timeout=10,
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def find_receiver(sys_tty: Path = Path("/sys/class/tty"), read_properties=properties) -> Receiver:
    matches = []
    for path in sorted(sys_tty.glob("ttyUSB*")):
        if not re.fullmatch(r"ttyUSB\d+", path.name):
            continue
        props = read_properties(path)
        if all(props.get(key) == value for key, value in IDENTITY.items()):
            node = Path(props.get("DEVNAME", ""))
            if node != Path("/dev") / path.name:
                raise ValueError("udev reported an unexpected device path")
            matches.append(Receiver(node, path.resolve()))
    if len(matches) != 1:
        raise ValueError(f"expected exactly one matching USB GPS, found {len(matches)}")
    return matches[0]


def udev_rule() -> str:
    match = ', '.join(f'ENV{{{key}}}=="{value}"' for key, value in IDENTITY.items())
    return (
        "# Hummer USB GPS: stable name and Debian gpsd add/remove lifecycle.\n"
        "# Run after 60-serial.rules has imported USB identity properties.\n"
        f'ACTION=="add|change", SUBSYSTEM=="tty", KERNEL=="ttyUSB[0-9]*", {match}, '
        'SYMLINK+="hummer_gps", TAG+="systemd", '
        f'ENV{{SYSTEMD_ALIAS}}+="{ALIAS}", ENV{{SYSTEMD_WANTS}}+="{HOTPLUG_UNIT}"\n'
    )


def update_defaults(text: str) -> str:
    replacements = {"DEVICES": f'"{ALIAS}"', "USBAUTO": '"true"'}
    seen = set()
    lines = []
    has_options = False
    for line in text.splitlines():
        assignment = re.match(r"^\s*([A-Z_]+)\s*=", line)
        key = assignment.group(1) if assignment else None
        if key == "GPSD_OPTIONS":
            has_options = True
        if key in replacements:
            if key not in seen:
                lines.append(f"{key}={replacements[key]}")
                seen.add(key)
        else:
            lines.append(line)
    for key, value in replacements.items():
        if key not in seen:
            lines.append(f"{key}={value}")
    if not has_options:
        lines.append('GPSD_OPTIONS="-n"')
    return "\n".join(lines) + "\n"


def stage(directory: Path, receiver: Receiver, defaults: str) -> None:
    if not receiver.node.is_char_device():
        raise ValueError("selected GPS is not a present character device")
    # The caller creates the private staging directory before invoking us.
    (directory / "gpsd.default").write_text(update_defaults(defaults), encoding="utf-8")
    (directory / "79-hummer-gps.rules").write_text(udev_rule(), encoding="utf-8")
    (directory / "node").write_text(str(receiver.node), encoding="ascii")
    (directory / "sysfs").write_text(str(receiver.sysfs), encoding="ascii")


def gpsd_status(wait_s: float = 10.0) -> dict:
    """Read device registration/fix mode without exposing coordinates."""
    deadline = time.monotonic() + wait_s
    with socket.create_connection(("127.0.0.1", 2947), timeout=3) as conn:
        conn.sendall(b'?WATCH={"enable":true,"json":true};\n')
        buffer = b""
        registered = False
        mode = None
        driver = None
        while time.monotonic() < deadline:
            conn.settimeout(min(1.0, max(0.01, deadline - time.monotonic())))
            try:
                chunk = conn.recv(8192)
            except TimeoutError:
                if registered:
                    break
                continue
            if not chunk:
                break
            buffer += chunk
            if len(buffer) > 65536:
                raise ValueError("gpsd report exceeds the size limit")
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    doc = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(doc, dict):
                    continue
                kind = doc.get("class")
                devices = doc.get("devices", []) if kind == "DEVICES" else [doc] if kind == "DEVICE" else []
                if isinstance(devices, list):
                    for device in devices:
                        if not isinstance(device, dict) or not isinstance(device.get("path"), str):
                            continue
                        path = Path(device["path"])
                        if ALIAS.is_char_device() and path.resolve() == ALIAS.resolve():
                            registered = True
                            driver = device.get("driver") or driver
                if kind == "TPV" and isinstance(doc.get("device"), str):
                    if ALIAS.is_char_device() and Path(doc["device"]).resolve() == ALIAS.resolve():
                        mode = doc.get("mode")
            if registered and mode is not None:
                break
    return {"registered": registered, "device": str(ALIAS), "driver": driver,
            "fix_mode": mode, "has_fix": isinstance(mode, int) and mode >= 2}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.check:
            result = gpsd_status()
            print(json.dumps(result))
            return 0 if result["registered"] else 2
        receiver = find_receiver()
        print(f"USB GPS: {receiver.node}; stable alias: {ALIAS}")
        if args.stage:
            stage(args.stage, receiver, Path("/etc/default/gpsd").read_text(encoding="utf-8"))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"GPS setup: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
