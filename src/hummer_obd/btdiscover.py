"""Fail-closed Bluetooth discovery and pairing for the OBDLink adapter.

This module recovers the vehicle's adapter once a human has paired it: it finds
the single already-paired OBDLink, discovers its Serial Port Profile channel by
SDP and binds ``/dev/rfcomm0``.  It then stops.

**It does not pair.**  The OBDLink MX+ was proven on 2026-09-01 to refuse
unattended pairing: with a ``NoInputNoOutput`` agent BlueZ returns
``org.bluez.Error.AuthenticationFailed``, and the association that actually
works is Secure Simple Pairing with a ``KeyboardDisplay`` agent answering the
six-digit confirmation — which needs a person at the adapter.  A watcher that
kept retrying an association this adapter rejects would be theatre, so the
first pairing is documented as a human step and this module only takes over
afterwards.  A bonded adapter also stops being discoverable, so recovery works
from BlueZ's known-device list rather than from an inquiry.

It is deliberately incapable of talking to the vehicle:

* it never opens a serial device and never imports the transport layer,
* every external command goes through :func:`_run`, which refuses any binary
  outside :data:`ALLOWED_BINARIES` (``hcitool``, ``bluetoothctl``, ``sdptool``,
  ``rfcomm``, ``systemctl``),
* it does not start the collector and does not run the probe.

Fail-closed rules, in order:

1. A device is a *candidate* only if its name resolves and matches
   :data:`NAME_PATTERN`.  An unnamed device, or one whose name request fails,
   is never a candidate — it is logged and ignored.
2. A known device is *recoverable* only if BlueZ reports it ``Paired: yes``,
   ``Bonded: yes`` and ``Trusted: yes``.  A half-pairing is not a bond.
3. Zero recoverable OBDLinks: keep watching.
4. More than one: **refuse**, log every one, keep watching.  Ambiguity is never
   resolved by guessing.
5. Exactly one: require SDP to yield exactly one Serial Port RFCOMM channel.
   Zero or several: refuse to bind, keep watching.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

__all__ = [
    "Device",
    "Selection",
    "ALLOWED_BINARIES",
    "NAME_PATTERN",
    "parse_inquiry",
    "is_named",
    "select_candidate",
    "parse_sdp_channels",
    "select_spp_channel",
    "parse_devices_list",
    "is_bonded_ready",
    "select_known_candidate",
    "INTERACTIVE_PAIRING_COMMAND",
]

#: The only external programs this module may execute.  Nothing here can send
#: a command to the vehicle; a serial tool would have to be added on purpose.
ALLOWED_BINARIES: frozenset[str] = frozenset(
    {"hcitool", "bluetoothctl", "sdptool", "rfcomm", "systemctl"}
)

#: The pairing procedure that actually works on this adapter.  Kept here so the
#: log, the runbook and the code cannot drift apart.
INTERACTIVE_PAIRING_COMMAND = (
    "bluetoothctl --agent KeyboardDisplay   # then: scan on / pair <MAC> / "
    "answer 'yes' to the six-digit confirmation / trust <MAC>"
)

#: A candidate must say it is an OBDLink.  "OBD", "ELM327" or a bare "MX+" are
#: not enough: this vehicle's adapter is an OBDLink MX+, and a loose pattern
#: would let some other dongle be paired instead.
NAME_PATTERN = re.compile(r"obdlink", re.IGNORECASE)

#: Names an inquiry returns when it could not resolve one.
_UNRESOLVED_NAMES = {"", "n/a", "unknown", "unresolved", "(unknown)"}

_MAC_RE = re.compile(r"^\s*((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\s*(.*?)\s*$")


@dataclass(frozen=True)
class Device:
    mac: str
    name: str = ""

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.mac} {self.name or '(no name)'}"


@dataclass
class Selection:
    #: "none", "unique" or "ambiguous"
    status: str
    device: Optional[Device] = None
    candidates: list[Device] = field(default_factory=list)
    ignored: list[Device] = field(default_factory=list)
    reason: str = ""

    @property
    def may_pair(self) -> bool:
        return self.status == "unique" and self.device is not None


def parse_inquiry(text: str) -> list[Device]:
    """Parse ``hcitool scan`` output into devices, preserving what it said."""
    devices: list[Device] = []
    for line in (text or "").splitlines():
        if not line.strip() or line.strip().lower().startswith("scanning"):
            continue
        match = _MAC_RE.match(line)
        if not match:
            continue
        mac = match.group(1).upper()
        name = match.group(2).strip()
        devices.append(Device(mac=mac, name=name))
    return devices


def is_named(device: Device) -> bool:
    """True when the device reported a name we can actually judge."""
    return device.name.strip().lower() not in _UNRESOLVED_NAMES


def select_candidate(devices: Iterable[Device]) -> Selection:
    """Choose at most one unambiguous OBDLink.  Never guesses."""
    devices = list(devices)
    named = [d for d in devices if is_named(d)]
    unnamed = [d for d in devices if not is_named(d)]
    candidates = [d for d in named if NAME_PATTERN.search(d.name)]

    # De-duplicate by address: one adapter answering two inquiries is not two
    # adapters.
    unique: dict[str, Device] = {}
    for device in candidates:
        unique.setdefault(device.mac, device)
    candidates = list(unique.values())

    if not candidates:
        return Selection(
            status="none",
            ignored=unnamed,
            reason=(
                f"no device identifies as an OBDLink "
                f"({len(named)} named, {len(unnamed)} unnamed)"
            ),
        )
    if len(candidates) > 1:
        return Selection(
            status="ambiguous",
            candidates=candidates,
            ignored=unnamed,
            reason=(
                "refusing to pair: "
                + str(len(candidates))
                + " devices identify as an OBDLink ("
                + ", ".join(str(c) for c in candidates)
                + "); a human must confirm which one is the vehicle's adapter"
            ),
        )
    return Selection(
        status="unique",
        device=candidates[0],
        candidates=candidates,
        ignored=unnamed,
        reason=f"exactly one OBDLink: {candidates[0]}",
    )


_DEVICE_LINE_RE = re.compile(r"^\s*Device\s+((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\s*(.*?)\s*$")


def parse_devices_list(text: str) -> list[Device]:
    """Parse ``bluetoothctl devices ...`` output ("Device <MAC> <name>")."""
    devices: list[Device] = []
    for line in (text or "").splitlines():
        match = _DEVICE_LINE_RE.match(line)
        if match:
            devices.append(Device(mac=match.group(1).upper(), name=match.group(2).strip()))
    return devices


def is_bonded_ready(info_text: str) -> bool:
    """True only when BlueZ reports a real, trusted bond.

    ``Paired: yes`` on its own is not enough: an association that was accepted
    but never produced a link key leaves the adapter paired-but-unbonded, and
    binding that produces a device node nothing can open.
    """
    text = info_text or ""
    return all(flag in text for flag in ("Paired: yes", "Bonded: yes", "Trusted: yes"))


def select_known_candidate(devices: Iterable[Device]) -> Selection:
    """Choose at most one already-bonded OBDLink from BlueZ's known devices."""
    selection = select_candidate(devices)
    if selection.status == "ambiguous":
        selection.reason = (
            "refusing to bind: several known devices identify as an OBDLink ("
            + ", ".join(str(c) for c in selection.candidates)
            + "); a human must confirm which one is the vehicle's adapter"
        )
    elif selection.status == "none":
        selection.reason = "no paired, bonded and trusted OBDLink is known to BlueZ"
    return selection


_CHANNEL_RE = re.compile(r"Channel:\s*(\d+)")


def parse_sdp_channels(text: str) -> list[int]:
    """Return the RFCOMM channels of Serial Port (0x1101) records.

    ``sdptool browse --tree`` prints one block per service.  Only blocks whose
    service class list contains Serial Port are considered: an adapter also
    advertises other profiles, and binding the wrong channel produces a device
    node that never answers.
    """
    channels: list[int] = []
    blocks = re.split(r"\n\s*\n", text or "")
    for block in blocks:
        lowered = block.lower()
        if "serial port" not in lowered and "0x1101" not in lowered:
            continue
        if "rfcomm" not in lowered:
            continue
        for match in _CHANNEL_RE.finditer(block):
            channels.append(int(match.group(1)))
    return channels


def select_spp_channel(text: str) -> tuple[Optional[int], str]:
    """Fail-closed channel choice: exactly one distinct SPP channel, or none."""
    channels = parse_sdp_channels(text)
    distinct = sorted(set(channels))
    if not distinct:
        return None, "SDP returned no Serial Port RFCOMM channel"
    if len(distinct) > 1:
        return None, f"SDP returned several Serial Port channels {distinct}; refusing to guess"
    return distinct[0], f"Serial Port Profile channel {distinct[0]}"


# --------------------------------------------------------------------------
# Everything below this line touches the system.  Nothing below opens a serial
# port, writes to one, or sends any vehicle command.
# --------------------------------------------------------------------------
class DisallowedCommand(RuntimeError):
    """Raised when something tries to run a binary outside the allowlist."""


def _run(argv: Sequence[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Run an allowlisted external command."""
    if not argv:
        raise DisallowedCommand("empty command")
    binary = Path(argv[0]).name
    if binary not in ALLOWED_BINARIES:
        raise DisallowedCommand(
            f"{binary!r} is not in the Bluetooth discovery allowlist {sorted(ALLOWED_BINARIES)}"
        )
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(argv), returncode=124, stdout="", stderr="timeout")


def _stamp() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Recorder:
    """Append-only evidence file for scan/pair/SDP/bind steps.

    Bluetooth link keys, Wi-Fi keys and vehicle data never pass through here:
    only inquiry output, device names, SDP records and command exit statuses.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, heading: str, body: str = "") -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(f"\n### {_stamp()} {heading}\n")
            if body:
                fh.write("\n".join("  " + line for line in body.splitlines()) + "\n")
            fh.flush()


def inquiry(timeout: float = 25.0) -> list[Device]:
    """Run one classic inquiry and resolve any missing names."""
    result = _run(["hcitool", "scan", "--flush"], timeout=timeout)
    devices = parse_inquiry(result.stdout)
    resolved: list[Device] = []
    for device in devices:
        if is_named(device):
            resolved.append(device)
            continue
        name = _run(["hcitool", "name", device.mac], timeout=15.0).stdout.strip()
        resolved.append(Device(mac=device.mac, name=name))
    return resolved


def known_ready_obdlinks() -> tuple[list[Device], list[Device]]:
    """Return (recoverable OBDLinks, OBDLinks that are known but not bonded)."""
    listing = _run(["bluetoothctl", "--timeout", "8", "devices"], timeout=20)
    known = [d for d in parse_devices_list(listing.stdout) if NAME_PATTERN.search(d.name)]
    ready: list[Device] = []
    not_ready: list[Device] = []
    for device in known:
        info = _run(["bluetoothctl", "--timeout", "8", "info", device.mac], timeout=20)
        (ready if is_bonded_ready(info.stdout) else not_ready).append(device)
    return ready, not_ready


def bind_known_adapter(device: Device, recorder: Recorder, *, root: Path) -> bool:
    """Discover the SPP channel of an already-bonded adapter and bind it.

    This never pairs, never trusts and never opens the serial port.  It fails
    closed: no Serial Port channel, or more than one, means no bind.
    """
    recorder.write(f"recovering already-paired adapter {device}")

    sdp = _run(["sdptool", "browse", "--tree", device.mac], timeout=45)
    if not sdp.stdout.strip():
        sdp = _run(["sdptool", "records", device.mac], timeout=45)
    recorder.write("sdptool browse", sdp.stdout + sdp.stderr)
    channel, reason = select_spp_channel(sdp.stdout)
    recorder.write(f"SPP channel selection: {reason}")
    if channel is None:
        recorder.write("REFUSED: not binding without exactly one Serial Port channel")
        return False

    default_file = Path("/etc/default/hummer-rfcomm")
    try:
        default_file.write_text(f"ADAPTER_MAC={device.mac}\nSPP_CHANNEL={channel}\n")
        recorder.write(f"wrote {default_file} (MAC and channel only)")
    except OSError as exc:
        recorder.write(
            f"REFUSED: cannot write {default_file}: {exc}",
            "This needs privilege the watcher does not have; a human can run:\n"
            f"  sudo sh -c 'printf \"ADAPTER_MAC={device.mac}\\nSPP_CHANNEL={channel}\\n\" "
            "> /etc/default/hummer-rfcomm && systemctl enable --now hummer-rfcomm.service'",
        )
        return False

    enable = _run(["systemctl", "enable", "--now", "hummer-rfcomm.service"], timeout=60)
    recorder.write(f"systemctl enable --now hummer-rfcomm rc={enable.returncode}",
                   enable.stdout + enable.stderr)
    if not Path("/dev/rfcomm0").exists():
        bind = _run(["rfcomm", "bind", "/dev/rfcomm0", device.mac, str(channel)], timeout=30)
        recorder.write(f"rfcomm bind rc={bind.returncode}", bind.stdout + bind.stderr)
    bound = Path("/dev/rfcomm0").exists()
    recorder.write("RESULT: " + ("/dev/rfcomm0 is bound" if bound else "REFUSED: /dev/rfcomm0 absent"))
    if bound:
        recorder.write(
            "next step is a human decision",
            "The raw read-only probe is NOT run automatically.  Run it, then\n"
            "review the transcript before the collector is even considered:\n"
            "  PYTHONPATH=src python3 -m hummer_obd.probe --device /dev/rfcomm0 \\\n"
            "      --config config/hummer.toml --root . --summary evidence/probe-summary.json",
        )
    return bound


def watch(root: Path, *, interval: float, once: bool, dry_run: bool, log=print) -> int:
    """Recover and bind the paired adapter; report anything that needs a human."""
    recorder = Recorder(root / "evidence" / "obdlink-pairing.txt")
    recorder.write("watch started", f"interval={interval}s dry_run={dry_run}")
    announced_unpaired: set[str] = set()
    while True:
        ready, not_bonded = known_ready_obdlinks()
        selection = select_known_candidate(ready)
        log(f"known bonded OBDLinks: {len(ready)}; {selection.reason}")

        if selection.status == "ambiguous":
            recorder.write("AMBIGUOUS - refusing to bind", selection.reason)
        elif selection.may_pair:
            log(f"recoverable adapter: {selection.device}")
            if dry_run:
                recorder.write("dry run: stopping before touching SDP or rfcomm")
                return 0
            if bind_known_adapter(selection.device, recorder, root=root):
                log("bound /dev/rfcomm0; stopping the watcher")
                return 0
            log("SPP discovery or bind did not succeed; continuing to watch")
        else:
            # Nothing bonded yet.  An adapter that is merely visible, or known
            # but unbonded, needs a person: this one refuses unattended
            # pairing, so say so once instead of retrying an association it
            # rejects.
            visible = [d for d in inquiry() if NAME_PATTERN.search(d.name)]
            for device in list(not_bonded) + visible:
                if device.mac in announced_unpaired:
                    continue
                announced_unpaired.add(device.mac)
                log(f"{device} is present but not bonded; interactive pairing is required")
                recorder.write(
                    f"NEEDS A HUMAN: {device} is not bonded",
                    "This adapter refuses unattended pairing (NoInputNoOutput gives\n"
                    "org.bluez.Error.AuthenticationFailed).  Pair it once, at the vehicle:\n"
                    f"  {INTERACTIVE_PAIRING_COMMAND}\n"
                    "Then this watcher recovers and binds it without further help.",
                )
        if once:
            return 0
        time.sleep(interval)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed recovery and RFCOMM binding for an already paired OBDLink"
    )
    parser.add_argument("--root", default="/home/jeremy/hummer-obd")
    parser.add_argument("--interval", type=float, default=45.0)
    parser.add_argument("--once", action="store_true", help="one inquiry, then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="scan and select, but never pair")
    args = parser.parse_args(argv)
    try:
        return watch(Path(args.root), interval=args.interval, once=args.once,
                     dry_run=args.dry_run)
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
