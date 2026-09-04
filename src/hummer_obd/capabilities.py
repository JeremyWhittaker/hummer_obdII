"""Offline capabilities report.

Before anyone — an operator, or an agent acting for one — decides to touch the
vehicle, they need an honest answer to two questions: what is this node *able*
to do, and what has it actually *proven*?  This module answers both from
evidence that is already on disk, and it answers them without going anywhere
near the car.

That last part is the whole point.  The report is meant to be safe to run while
the truck is asleep and a sleep test is being observed, so a run of it must be
indistinguishable, from the vehicle's side, from not running it at all:

* it never imports the serial layer and never touches ``adapter.device``.  The
  device path is inspected with ``stat`` only, so the report can say whether the
  character device exists without ever opening it,
* it opens the collector's SQLite buffer through a ``file:...?mode=ro`` URI, so
  a report is incapable of writing to the collector's own database,
* it reads raw transcripts for *metadata* only — size, hash, record counts —
  and never emits a byte of transcript payload,
* it shells out to nothing except ``systemctl is-enabled``/``is-active``, and
  never with ``sudo``,
* every string that reaches the output goes through :func:`_sanitize`, which
  redacts MAC addresses down to their OUI, IP addresses, tailnet hostnames,
  VINs and long device serial numbers.

Every input is optional.  A missing database, an empty evidence directory, an
absent adapter and a machine with no ``systemctl`` are all ordinary answers
rather than errors, and the report is still produced with exit status 0.  The
one thing this refuses to do is describe a node that does not exist: an
unreadable configuration exits 2 instead of quietly reporting on defaults.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import stat as stat_module
import subprocess
import sys
import textwrap
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from . import __version__, safety
from .confidence import CONFIDENCE, PRODUCTION_MINIMUM
from .config import Config, load_config
from .decode import mask_vin
from .rawlog import iter_records

__all__ = [
    "SCHEMA",
    "SERVICE_UNITS",
    "GATE_ACCEPT_SAMPLES",
    "GATE_REFUSE_SAMPLES",
    "RawLogSummary",
    "GateCheck",
    "DeferredItem",
    "open_database_readonly",
    "build_report",
    "render_text",
    "main",
]

#: Bump the trailing integer when a consumer would have to change to read this.
SCHEMA = "hummer-obd/capabilities/1"

DEFAULT_JSON_NAME = "capabilities-latest.json"

#: systemd units this project installs.  Their state is read, never changed.
SERVICE_UNITS = ("hummer-display", "hummer-rfcomm", "hummer-btdiscover", "hummer-collector")

#: Representative read-only requests.  The gate decides; this list only asks.
GATE_ACCEPT_SAMPLES = ("0100", "010D", "011F", "0142", "03", "07", "0A", "0902", "ATI", "ATRV", "STI")

#: Representative commands that must never reach the bus.  Nothing here is
#: hardcoded as "refused" in the output — each one is put to the live gate, so
#: a weakened allowlist would show up in the report as an accepted command.
GATE_REFUSE_SAMPLES = (
    "04",        # clear DTCs
    "0400",      # clear DTCs, with a parameter
    "08",        # on-board component control
    "22F190",    # enhanced read-by-identifier (GM/Ultium territory)
    "2E1234",    # WriteDataByIdentifier
    "2701",      # SecurityAccess
    "3101FF00",  # RoutineControl
    "1101",      # ECUReset
    "3E00",      # TesterPresent
    "010D;04",   # a read request with a clear-DTC smuggled behind it
    # The monitor commands.  These are *passive* -- they receive rather than
    # request -- and they are still refused here, because this is the gate the
    # unattended collector runs behind.  ``hummer-obd-passive`` reaches them
    # through two separate gates of its own (safety.py), so if a future edit
    # ever widens the collector's allowlist to include one, a published
    # capability report flips it from refused to accepted in plain sight.
    "ATMA",      # monitor all
    "STM",       # STN monitor
    "STMA",      # STN monitor all
    "STCMM0",    # CAN monitoring mode: receive only, no acknowledgements
    "STCMM1",    # CAN monitoring mode: normal node (acknowledges -- transmits)
    "STCMM2",    # CAN monitoring mode: receive all, no acknowledgements
)


# -- sanitization --------------------------------------------------------
#
# The report is written to be pasted into a commit message, an issue or a
# handover note, so redaction has to happen on the way out rather than being
# left to whoever is doing the pasting.  Order matters below: MAC addresses are
# taken before IPv6, because both are colon-separated hex.

#: Case-insensitive: DNS is, and a hostname copied out of a UI may not be
#: lower-cased.  The literal ``ts.net`` suffix is still required, so this
#: widens what is caught without widening what is matched.
_TAILNET_RE = re.compile(r"(?<![\w.-])[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*\.ts\.net\b", re.I)
_MAC_RE = re.compile(r"(?<![0-9A-Fa-f:])([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})(?![0-9A-Fa-f:])")
#: The same address written with hyphens, which is how an adapter's own
#: device-description string (``AT@2``, ``STDI``) often reports it.  It needs
#: its own boundary class: a colon-separated MAC may legitimately follow a
#: hyphen ("bt-00:04:..."), so the two forms cannot share one pattern.
_MAC_DASH_RE = re.compile(r"(?<![0-9A-Fa-f-])([0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){5})(?![0-9A-Fa-f-])")
_IPV6_RE = re.compile(
    r"(?<![0-9A-Za-z:.])("
    r"(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
    r"|(?:[0-9A-Fa-f]{1,4}:){1,7}:(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6})?"
    r"|::(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,7})?"
    r")(?![0-9A-Za-z:.])"
)
_IPV4_RE = re.compile(r"(?<![0-9A-Za-z.])((?:\d{1,3}\.){3}\d{1,3})(?![0-9A-Za-z.])")
#: A VIN is 17 characters with I, O and Q excluded to avoid digit confusion.
# Uppercase only, deliberately.  Every VIN that can reach this report comes
# from decode.decode_vin, which builds it from response bytes and emits
# uppercase.  Matching case-insensitively would additionally catch any 17-char
# lowercase token -- session ids, file names, digests -- and mangling real
# evidence is a worse outcome than a lowercase VIN that no code path produces.
_VIN_RE = re.compile(r"(?<![A-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Z0-9])")
#: A standalone run of ten or more digits: an adapter serial, an IMEI, an epoch.
#: The lookarounds keep it from biting a hex digest that happens to contain a
#: long digit run, which would silently corrupt a raw-log hash.
_SERIAL_RE = re.compile(r"(?<![0-9A-Za-z])(\d{10,})(?![0-9A-Za-z])")


def _mac_replacement(match: re.Match[str]) -> str:
    """Keep the OUI, drop the device half.

    The vendor prefix is the useful part in a report ("this really is an
    OBDLink"); the last three octets identify one specific adapter and are the
    part that must not be published.
    """
    raw = match.group(1)
    separator = ":" if ":" in raw else "-"
    octets = raw.upper().split(separator)
    return separator.join(octets[:3] + ["XX", "XX", "XX"])


def _ipv4_replacement(match: re.Match[str]) -> str:
    octets = match.group(1).split(".")
    if any(not 0 <= int(o) <= 255 for o in octets):
        return match.group(0)  # a version number, not an address
    return "[redacted-ipv4]"


def _serial_replacement(match: re.Match[str]) -> str:
    digits = match.group(1)
    return "*" * (len(digits) - 4) + digits[-4:]


def _sanitize(text: str) -> str:
    """Redact everything that must not leave the Pi in a shareable report."""
    if not isinstance(text, str):
        return text
    text = _TAILNET_RE.sub("[redacted-tailnet-host]", text)
    text = _MAC_RE.sub(_mac_replacement, text)
    text = _MAC_DASH_RE.sub(_mac_replacement, text)
    text = _IPV6_RE.sub("[redacted-ipv6]", text)
    text = _IPV4_RE.sub(_ipv4_replacement, text)
    text = _VIN_RE.sub(lambda m: mask_vin(m.group(0)), text)
    text = _SERIAL_RE.sub(_serial_replacement, text)
    return text


def _sanitize_tree(value: Any) -> Any:
    """Apply :func:`_sanitize` to every string in a nested structure.

    Running this over the finished report is what makes redaction a property of
    the module rather than a rule each section has to remember.
    """
    if isinstance(value, str):
        return _sanitize(value)
    if isinstance(value, dict):
        return {_sanitize_tree(k): _sanitize_tree(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_tree(v) for v in value]
    return value


# -- small records -------------------------------------------------------


@dataclass
class GateCheck:
    """One command put to the live safety gate, with the gate's answer."""

    command: str
    accepted: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RawLogSummary:
    """Metadata for one raw transcript.  Never its contents."""

    name: str
    size_bytes: int = 0
    modified: str = ""
    sha256: str = ""
    records: int = 0
    tx: int = 0
    rx: int = 0
    events: int = 0
    corrupt: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DeferredItem:
    """Something this node cannot do, and the reason it cannot do it."""

    capability: str
    title: str
    status: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# -- read-only database access -------------------------------------------


def open_database_readonly(path: str | Path) -> sqlite3.Connection:
    """Open the collector's database in a mode that cannot write to it.

    The collector owns this file; a report has no business changing it, and
    "we simply never call INSERT" is a promise rather than a mechanism.  SQLite
    enforces ``mode=ro`` itself, so a stray write raises instead of touching the
    buffer.  The path is passed as a percent-encoded file URI so a directory
    name containing ``?`` cannot smuggle in another URI parameter.
    """
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Any:
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


# -- sections ------------------------------------------------------------


def _device_section(device: str) -> dict[str, Any]:
    """Describe the adapter device from its metadata alone.

    ``stat`` answers "is the tty bound?" without opening it.  Opening
    ``/dev/rfcomm0`` would raise the RFCOMM link and could wake a sleeping
    vehicle, which is exactly what this report exists to avoid.
    """
    info: dict[str, Any] = {"path": device, "exists": False, "kind": "absent", "opened": False}
    try:
        st = os.stat(device)
    except OSError as exc:
        info["detail"] = exc.strerror or str(exc)
        return info
    info["exists"] = True
    if stat_module.S_ISCHR(st.st_mode):
        info["kind"] = "character device"
    elif stat_module.S_ISBLK(st.st_mode):
        info["kind"] = "block device"
    elif stat_module.S_ISDIR(st.st_mode):
        info["kind"] = "directory"
    else:
        info["kind"] = "regular file"
    info["mode"] = stat_module.filemode(st.st_mode)
    info["modified"] = _iso(st.st_mtime)
    return info


def _node_section(cfg: Config, config_source: str) -> dict[str, Any]:
    return {
        "package_version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(terse=True),
        "root": _where(cfg.root),
        "config_source": config_source,
        "adapter_device": _device_section(cfg.adapter.device),
    }


def _gate_check(command: str) -> GateCheck:
    """Ask the live gate about *command* and record what it said."""
    accepted = safety.is_safe(command)
    if accepted:
        return GateCheck(command=safety.normalize(command), accepted=True,
                         detail=safety.describe_command(command))
    try:
        safety.validate_command(command)
        detail = ""  # unreachable while is_safe and validate_command agree
    except safety.UnsafeCommandError as exc:
        detail = str(exc)
    return GateCheck(command=command, accepted=False, detail=detail)


def _enhanced_proven() -> int:
    """How many enumerated identifiers this vehicle has actually answered."""
    return sum(1 for e in CONFIDENCE.values() if e.level >= 1)


def _enhanced_production() -> int:
    """How many are cross-validated -- the bar for a telemetry reading."""
    return sum(1 for e in CONFIDENCE.values() if e.level >= PRODUCTION_MINIMUM)


def _safety_section() -> dict[str, Any]:
    """Report the gate by interrogating it, not by describing it."""
    at_exact = getattr(safety, "_ALLOWED_AT_EXACT", frozenset())
    at_patterns = getattr(safety, "_ALLOWED_AT_PATTERNS", ())
    accepted = [_gate_check(c) for c in GATE_ACCEPT_SAMPLES]
    refused = [_gate_check(c) for c in GATE_REFUSE_SAMPLES]
    return {
        "allowed_obd_modes": sorted(safety.ALLOWED_OBD_MODES),
        "allowed_obd_mode_count": len(safety.ALLOWED_OBD_MODES),
        "forbidden_services": sorted(safety.FORBIDDEN_SERVICES),
        "forbidden_service_count": len(safety.FORBIDDEN_SERVICES),
        "adapter_commands_exact": len(at_exact),
        "adapter_command_patterns": len(at_patterns),
        "checked_accepted": [c.as_dict() for c in accepted],
        "checked_refused": [c.as_dict() for c in refused],
        # If either of these is ever false, the gate has been weakened and the
        # rest of this report is not worth reading.
        "all_samples_accepted": all(c.accepted for c in accepted),
        "all_samples_refused": not any(c.accepted for c in refused),
    }


def _safe_endpoint(endpoint: str) -> str:
    """Say *where* telemetry would go without republishing a credential.

    ``upload.token_file`` is the supported home for a bearer token, but a URL
    can carry one anyway — in ``user:pass@`` userinfo, or in a ``?token=``
    query — and nothing in :func:`_sanitize` recognises a secret by shape.
    This report is written to be pasted into a handover note and is
    deliberately un-ignored by ``.gitignore``, so the endpoint is reduced to
    scheme, host and path.  That still answers the only question the report is
    asking ("does anything leave the Pi, and to where?") and leaves no room for
    a secret to ride along.
    """
    if not endpoint:
        return endpoint
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        # An endpoint we cannot parse is an endpoint we cannot vouch for.
        return "[unparsable-endpoint]"
    if not parts.scheme or not parts.netloc:
        return endpoint  # not a URL at all; the generic sanitizer still runs
    # Split userinfo off the authority by hand rather than rebuilding from
    # ``parts.hostname``/``parts.port``, which lower-cases the host and drops
    # the brackets around an IPv6 literal.
    host = parts.netloc.rsplit("@", 1)[-1]
    if host != parts.netloc:
        host = f"[redacted-credentials]@{host}"
    trimmed = f"{parts.scheme}://{host}{parts.path}"
    if parts.query or parts.fragment:
        trimmed += "?[redacted-query]"
    return trimmed


def _configuration_section(cfg: Config) -> dict[str, Any]:
    token_file = cfg.upload.token_file
    return {
        "collector": {
            "enabled": cfg.collector.enabled,
            "pids": list(cfg.collector.pids),
            "poll_interval_s": cfg.collector.poll_interval_s,
            "dtc_interval_s": cfg.collector.dtc_interval_s,
            "idle_backoff_s": cfg.collector.idle_backoff_s,
            "max_consecutive_errors": cfg.collector.max_consecutive_errors,
            "max_cycles": cfg.collector.max_cycles,
            "duration_s": cfg.collector.duration_s,
            "database": _where(cfg.path(cfg.collector.database)),
            "raw_log_dir": _where(cfg.path(cfg.collector.raw_log_dir)),
        },
        "upload": {
            "enabled": cfg.upload.enabled,
            "endpoint": _safe_endpoint(cfg.upload.endpoint),
            "require_https": cfg.upload.require_https,
            "batch_size": cfg.upload.batch_size,
            "interval_s": cfg.upload.interval_s,
            "timeout_s": cfg.upload.timeout_s,
            "allow_raw_logs": cfg.upload.allow_raw_logs,
            # The path to a bearer token is itself a hint worth not publishing,
            # so only its existence is reported, never the path or the token.
            "token_file_configured": bool(token_file),
            "token_file_present": bool(token_file) and Path(cfg.path(token_file)).exists(),
        },
        "display": {
            "enabled": cfg.display.enabled,
            "refresh_interval_s": cfg.display.refresh_interval_s,
            "sleep_between_updates": cfg.display.sleep_between_updates,
            "simulated": bool(cfg.display.simulate_path),
        },
        "adapter": {
            "device": cfg.adapter.device,
            "baudrate": cfg.adapter.baudrate,
            "read_timeout_s": cfg.adapter.read_timeout_s,
            "command_timeout_s": cfg.adapter.command_timeout_s,
            "bluetooth_address_configured": bool(cfg.adapter.bluetooth_address),
        },
    }


#: Keys worth merging out of probe/command summaries, in report order.
_SUMMARY_KEYS = (
    "adapter",
    "supported_pids",
    "probed_pids",
    "supported_service09",
    "samples",
    "dtcs",
    "service09",
    "commands",
    "vin_masked",
    "vin_status",
)


def _scan_order(path: Path) -> tuple[float, str]:
    """Oldest evidence first, so a later probe wins a merge on equal footing."""
    try:
        return (path.stat().st_mtime, path.name)
    except OSError:
        return (0.0, path.name)


def _looks_like_summary(data: Any) -> bool:
    """True for a probe or command-set summary, false for any other JSON."""
    return (
        isinstance(data, dict)
        and "session" in data
        and any(key in data for key in ("adapter", "commands", "supported_pids"))
    )


def _informative(value: Any) -> int:
    """Rank a candidate value so the richest evidence wins a merge."""
    if value is None or value == "" or value == [] or value == {}:
        return 0
    if isinstance(value, (list, dict)):
        return len(value)
    return 1


def _trim_samples(samples: Any) -> dict[str, Any]:
    """Keep the decoded reading, drop the raw hex.

    The raw bytes are already in the transcript, which is the authoritative
    copy; repeating them here would put payload into a shareable document for
    no gain.
    """
    if not isinstance(samples, dict):
        return {}
    trimmed = {}
    for pid, item in samples.items():
        if not isinstance(item, dict):
            continue
        trimmed[str(pid)] = {
            "name": item.get("name", ""),
            "value": item.get("value"),
            "unit": item.get("unit", ""),
            "status": item.get("status", ""),
        }
    return trimmed


def _trim_dtcs(dtcs: Any) -> dict[str, Any]:
    if not isinstance(dtcs, dict):
        return {}
    return {
        str(mode): {
            "codes": list(item.get("codes", [])) if isinstance(item, dict) else [],
            "status": item.get("status", "") if isinstance(item, dict) else "",
        }
        for mode, item in dtcs.items()
    }


def _trim_commands(commands: Any) -> list[dict[str, Any]]:
    if not isinstance(commands, list):
        return []
    return [
        {"command": item.get("command", ""), "status": item.get("status", "")}
        for item in commands
        if isinstance(item, dict)
    ]


def _evidence_section(cfg: Config, exclude: Optional[Path] = None) -> dict[str, Any]:
    """Merge whatever probe evidence the node has accumulated.

    Several probes usually exist: an early one that only fingerprinted the
    adapter, a later one that reached the vehicle.  Rather than picking a file,
    each key takes the most informative answer anyone ever got, and records
    which session produced it, so the report can say "this was proven" and name
    the proof.
    """
    directory = Path(cfg.path("evidence"))
    section: dict[str, Any] = {
        "directory": _where(directory),
        "files_scanned": [],
        "summaries": 0,
        "sessions": [],
        "merged": {},
        "sources": {},
    }
    if not directory.is_dir():
        section["note"] = "no evidence directory; nothing has been probed from this checkout"
        return section

    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}
    candidates = sorted(directory.glob("*.json"), key=_scan_order)
    for path in candidates:
        if exclude is not None and path.resolve() == exclude:
            continue
        section["files_scanned"].append(path.name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not _looks_like_summary(data):
            continue
        section["summaries"] += 1
        session_uid = str(data.get("session", path.name))
        section["sessions"].append(session_uid)
        for key in _SUMMARY_KEYS:
            if key not in data:
                continue
            if _informative(data[key]) > _informative(merged.get(key)):
                merged[key] = data[key]
                sources[key] = session_uid

    if "samples" in merged:
        merged["samples"] = _trim_samples(merged["samples"])
    if "dtcs" in merged:
        merged["dtcs"] = _trim_dtcs(merged["dtcs"])
    if "commands" in merged:
        merged["commands"] = _trim_commands(merged["commands"])
    adapter = merged.get("adapter") if isinstance(merged.get("adapter"), dict) else {}
    merged["protocol"] = adapter.get("protocol", "")
    merged["adapter_id"] = adapter.get("ATI", "")
    section["merged"] = merged
    section["sources"] = sources
    if not section["summaries"]:
        section["note"] = "no probe summary found; nothing has been proven on the vehicle yet"
    return section


def _raw_log_summary(path: Path) -> RawLogSummary:
    """Hash and count one transcript without emitting any of its payload."""
    summary = RawLogSummary(name=path.name)
    try:
        st = path.stat()
        summary.size_bytes = st.st_size
        summary.modified = _iso(st.st_mtime)
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        summary.sha256 = digest.hexdigest()
        for record in iter_records(path):
            summary.records += 1
            kind = record.get("kind")
            if kind == "corrupt":
                summary.corrupt += 1
            elif kind == "event":
                summary.events += 1
            elif kind == "io":
                direction = record.get("dir")
                if direction == "tx":
                    summary.tx += 1
                elif direction == "rx":
                    summary.rx += 1
    except (OSError, UnicodeDecodeError) as exc:
        summary.error = str(exc)
    return summary


def _raw_logs_section(cfg: Config) -> dict[str, Any]:
    directory = Path(cfg.path(cfg.collector.raw_log_dir))
    section: dict[str, Any] = {"directory": _where(directory), "files": [], "totals": {}}
    if not directory.is_dir():
        section["note"] = "no raw log directory; nothing has been recorded from the wire"
        return section
    files = [_raw_log_summary(p) for p in sorted(directory.glob("*.jsonl"))]
    section["files"] = [f.as_dict() for f in files]
    section["totals"] = {
        "files": len(files),
        "size_bytes": sum(f.size_bytes for f in files),
        "records": sum(f.records for f in files),
        "tx": sum(f.tx for f in files),
        "rx": sum(f.rx for f in files),
        "events": sum(f.events for f in files),
        "corrupt": sum(f.corrupt for f in files),
    }
    if not files:
        section["note"] = "raw log directory is empty"
    return section


def _count_responders(raw_hex: str) -> int:
    """Count distinct reply frames in a stored DTC response.

    ``dtc_reads.raw_hex`` holds one whitespace-separated frame per responding
    line, with the CAN identifier already stripped by the parser, so distinct
    frames are the closest thing to an ECU count this table can support.  The
    raw transcript keeps the actual identifiers and stays authoritative.
    """
    return len({token for token in str(raw_hex).split() if token})


def _database_section(cfg: Config) -> dict[str, Any]:
    path = Path(cfg.path(cfg.collector.database))
    section: dict[str, Any] = {"path": _where(path), "present": path.exists(), "read_only": True}
    if not path.exists():
        section["note"] = "no collector database; nothing has been recorded on this node"
        return section
    try:
        conn = open_database_readonly(path)
    except sqlite3.Error as exc:
        section["error"] = str(exc)
        return section
    try:
        section["schema_version"] = _scalar(conn, "SELECT version FROM schema_version")
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                " ORDER BY name"
            )
        ]
        section["tables"] = {name: _scalar(conn, f"SELECT COUNT(*) FROM '{name}'") for name in tables}
        section["sessions"] = _sessions(conn)
        section["latest_values"] = _latest_values(conn, cfg)
        section["dtc_summary"] = _dtc_summary(conn)
        section["vehicle_info"] = _vehicle_info(conn)
        section["upload_queue_depth"] = _scalar(
            conn, "SELECT COUNT(*) FROM samples WHERE uploaded_at IS NULL"
        )
    except sqlite3.Error as exc:
        section["error"] = str(exc)
    finally:
        conn.close()
    return section


def _sessions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT session_uid, started_at, ended_at, adapter_id, protocol FROM sessions"
        " ORDER BY id DESC LIMIT 50"
    ).fetchall()
    return [
        {
            "session_uid": row["session_uid"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"] or "",
            "adapter_id": row["adapter_id"] or "",
            "protocol": row["protocol"] or "",
        }
        for row in reversed(rows)
    ]


#: Sessions shown in the coverage section's per-session list; older sessions
#: still count towards every total, they just are not enumerated by name.
_COVERAGE_RECENT_LIMIT = 10


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse a stored ISO-8601 timestamp, or say nothing rather than crash.

    ``samples.ts`` is written by this project's own code and should always be
    parseable, but the coverage section exists precisely to survive whatever
    the database on a real Pi actually contains -- including a row a future
    schema change, a hand edit, or a partial write left malformed.

    A timestamp with no UTC offset parses cleanly but is not "unparsable" in
    the way ``except ValueError`` catches -- it is a landmine instead: mixing
    one naive value into comparisons against the timezone-aware values every
    other row has raises ``TypeError`` the moment they are compared, not when
    they are parsed. Every timestamp this project writes carries an offset
    (:func:`hummer_obd.storage._now`), so one that does not is treated the
    same as one that cannot be parsed at all: skip the row, do not crash.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _duration(seconds: Optional[float]) -> str:
    """Render a span for a human, not for a spreadsheet.

    A coverage report is read by someone deciding whether a gap matters, and
    "5112.597165s" makes them do arithmetic before they can judge it.  Full
    precision stays in the JSON; only the text rendering is rounded.
    """
    if seconds is None:
        return "(none)"
    seconds = float(seconds)
    if seconds < 90:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 90:
        return f"{seconds:.0f}s ({int(minutes)}m {int(rest):02d}s)"
    hours, minutes = divmod(int(minutes), 60)
    return f"{seconds:.0f}s ({hours}h {minutes:02d}m)"


def _coverage_gaps(windows: list[dict[str, Any]], gap_threshold_s: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Intervals between temporally consecutive sessions.

    *windows* must already be sorted by ``first_ts``.  A session whose window
    starts before the previous session's window has ended is an overlap, not
    a gap -- reporting a negative "gap" would be nonsense, so those pairs are
    left out of both the full and the threshold-filtered list entirely.
    """
    intervals: list[dict[str, Any]] = []
    for prev, cur in zip(windows, windows[1:]):
        if cur["first_ts"] <= prev["last_ts"]:
            continue  # overlap, not a gap
        seconds = (cur["first_ts"] - prev["last_ts"]).total_seconds()
        intervals.append({
            "after_session": prev["uid"],
            "before_session": cur["uid"],
            "start": prev["last_ts"].isoformat(),
            "end": cur["first_ts"].isoformat(),
            "seconds": seconds,
        })
    reported = sorted(
        (gap for gap in intervals if gap["seconds"] > gap_threshold_s),
        key=lambda gap: gap["seconds"],
        reverse=True,
    )
    return intervals, reported


def _compute_coverage(conn: sqlite3.Connection, gap_threshold_s: float) -> dict[str, Any]:
    """Everything the coverage section reports, read from one connection.

    Sample timestamps -- not ``sessions.started_at``/``ended_at`` -- are the
    evidence a gap is built from: a session row can be started and never
    properly closed, but a run of samples is proof the collector was actually
    talking to the adapter during that window.
    """
    session_rows = conn.execute(
        "SELECT id, session_uid, started_at, ended_at FROM sessions ORDER BY id"
    ).fetchall()
    counts = {
        row["session_id"]: row["n"]
        for row in conn.execute("SELECT session_id, COUNT(*) AS n FROM samples GROUP BY session_id")
    }

    windows: dict[int, dict[str, datetime]] = {}
    first_sample: Optional[datetime] = None
    last_sample: Optional[datetime] = None
    for row in conn.execute("SELECT session_id, ts FROM samples"):
        ts = _parse_ts(row["ts"])
        if ts is None:
            continue  # unparsable timestamp: skip the row, not the report
        window = windows.setdefault(row["session_id"], {"first_ts": ts, "last_ts": ts})
        if ts < window["first_ts"]:
            window["first_ts"] = ts
        if ts > window["last_ts"]:
            window["last_ts"] = ts
        if first_sample is None or ts < first_sample:
            first_sample = ts
        if last_sample is None or ts > last_sample:
            last_sample = ts

    detail: list[dict[str, Any]] = []
    ordered_windows: list[dict[str, Any]] = []
    observed_seconds = 0.0
    for row in session_rows:
        sid = row["id"]
        window = windows.get(sid)
        if window is not None:
            observed_seconds += (window["last_ts"] - window["first_ts"]).total_seconds()
            ordered_windows.append({
                "uid": row["session_uid"],
                "first_ts": window["first_ts"],
                "last_ts": window["last_ts"],
            })
        detail.append({
            "session_uid": row["session_uid"],
            "started_at": row["started_at"] or "",
            "ended_at": row["ended_at"] or "open",
            "sample_count": counts.get(sid, 0),
        })
    ordered_windows.sort(key=lambda w: w["first_ts"])

    all_intervals, reported_gaps = _coverage_gaps(ordered_windows, gap_threshold_s)

    total_span_seconds = None
    if first_sample is not None and last_sample is not None:
        total_span_seconds = (last_sample - first_sample).total_seconds()

    longest_gap_s = max((gap["seconds"] for gap in all_intervals), default=None)

    coverage_ratio = None
    if total_span_seconds:  # guards both None and exactly-zero span
        # Overlapping sessions could in principle push observed_seconds past
        # total_span_seconds; capped at 1.0 so the ratio stays a fraction.
        coverage_ratio = max(0.0, min(1.0, observed_seconds / total_span_seconds))

    session_count = len(detail)
    recent = detail[-_COVERAGE_RECENT_LIMIT:]
    note = ""
    if session_count == 0:
        note = "no sessions recorded; nothing to evaluate for coverage"
    elif session_count == 1:
        note = "only one session recorded; no gap is measurable without a second session"
    elif not reported_gaps:
        note = f"no gaps longer than {gap_threshold_s}s were found between {session_count} sessions"

    result: dict[str, Any] = {
        "sessions": {
            "count": session_count,
            "recent": recent,
            "omitted": max(0, session_count - len(recent)),
        },
        "first_sample": first_sample.isoformat() if first_sample else None,
        "last_sample": last_sample.isoformat() if last_sample else None,
        "total_span_seconds": total_span_seconds,
        "observed_seconds": observed_seconds,
        "gaps": reported_gaps,
        "longest_gap_s": longest_gap_s,
        "coverage_ratio": coverage_ratio,
    }
    if note:
        result["note"] = note
    return result


def _coverage_section(cfg: Config, gap_threshold_s: float = 60.0) -> dict[str, Any]:
    """Make gaps in local collection visible instead of discovered by accident.

    Derived entirely from ``sessions``/``samples`` in the collector's own
    database, through the same read-only connection every other database
    section uses.
    """
    path = Path(cfg.path(cfg.collector.database))
    section: dict[str, Any] = {
        "gap_threshold_s": gap_threshold_s,
        "sessions": {"count": 0, "recent": [], "omitted": 0},
        "first_sample": None,
        "last_sample": None,
        "total_span_seconds": None,
        "observed_seconds": 0.0,
        "gaps": [],
        "longest_gap_s": None,
        "coverage_ratio": None,
    }
    if not path.exists():
        section["note"] = "no collector database; nothing has been recorded on this node"
        return section
    try:
        conn = open_database_readonly(path)
    except sqlite3.Error as exc:
        section["error"] = str(exc)
        return section
    try:
        section.update(_compute_coverage(conn, gap_threshold_s))
    except sqlite3.Error as exc:
        section["error"] = str(exc)
    finally:
        conn.close()
    return section


def _latest_values(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    """Newest recorded reading for each PID the collector is configured to poll.

    Configured but never-recorded PIDs are reported too: "we ask for this and
    have never got an answer" is the single most useful line in this report.
    """
    latest: dict[str, Any] = {}
    for entry in cfg.collector.pids:
        request = safety.normalize(str(entry))
        pid = request[2:4]
        row = conn.execute(
            "SELECT ts, name, value, unit, status FROM samples WHERE pid = ?"
            " ORDER BY ts DESC, id DESC LIMIT 1",
            (pid,),
        ).fetchone()
        if row is None:
            latest[pid] = {"request": request, "recorded": False}
            continue
        latest[pid] = {
            "request": request,
            "recorded": True,
            "ts": row["ts"],
            "name": row["name"],
            "value": row["value"],
            "unit": row["unit"] or "",
            "status": row["status"],
        }
    return latest


def _dtc_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    rows = conn.execute(
        "SELECT mode, COUNT(*) AS reads, MAX(ts) AS latest,"
        " SUM(CASE WHEN codes <> '' THEN 1 ELSE 0 END) AS with_codes"
        " FROM dtc_reads GROUP BY mode ORDER BY mode"
    ).fetchall()
    for row in rows:
        mode = row["mode"]
        responders = 0
        for raw in conn.execute(
            "SELECT raw_hex FROM dtc_reads WHERE mode = ? ORDER BY id DESC LIMIT 500", (mode,)
        ):
            responders = max(responders, _count_responders(raw["raw_hex"]))
        summary[mode] = {
            "reads": int(row["reads"] or 0),
            "latest": row["latest"] or "",
            "ever_reported_codes": bool(row["with_codes"]),
            "distinct_reply_frames": responders,
        }
    return summary


def _vehicle_info(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Vehicle information rows, re-masked on the way out.

    The collector already masks these on the way in; running ``mask_vin`` over
    anything VIN-shaped again costs nothing and means a row written by an older
    or hand-edited build still cannot publish a VIN.
    """
    rows = conn.execute(
        "SELECT ts, item, value_masked FROM vehicle_info ORDER BY id DESC LIMIT 20"
    ).fetchall()
    return [
        {
            "ts": row["ts"],
            "item": row["item"],
            "value": _VIN_RE.sub(lambda m: mask_vin(m.group(0)), str(row["value_masked"])),
        }
        for row in reversed(rows)
    ]


def _services_section() -> dict[str, Any]:
    """Read systemd state for this project's units, or admit it cannot."""
    systemctl = shutil.which("systemctl")
    section: dict[str, Any] = {"systemctl": bool(systemctl), "units": {}}
    if not systemctl:
        section["note"] = "systemctl is not present; service state is unknown"
        section["units"] = {unit: {"enabled": "unknown", "active": "unknown"} for unit in SERVICE_UNITS}
        return section
    for unit in SERVICE_UNITS:
        section["units"][unit] = {
            "enabled": _systemctl(systemctl, "is-enabled", unit),
            "active": _systemctl(systemctl, "is-active", unit),
        }
    return section


def _systemctl(systemctl: str, query: str, unit: str) -> str:
    """One ``systemctl`` query.  Never ``sudo``, never a state change."""
    try:
        completed = subprocess.run(
            [systemctl, query, f"{unit}.service"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    # A disabled or inactive unit exits non-zero but still names its state on
    # stdout, so the text is the answer and the exit status is not.
    answer = (completed.stdout or "").strip().splitlines()
    return answer[0] if answer else "unknown"


def _deferred_section(cfg: Config, services: dict[str, Any], gate: dict[str, Any]) -> list[dict[str, Any]]:
    """What this node cannot do, and why — derived, not asserted."""
    mode22 = next(
        (c for c in gate["checked_refused"] if c["command"].startswith("22")),
        {"accepted": False, "detail": "service 22 is not on the read-only allowlist"},
    )
    control_services = sorted(s for s in safety.FORBIDDEN_SERVICES if s in {"04", "08", "2F", "31"})
    collector_unit = services["units"].get("hummer-collector", {})
    unit_enabled = collector_unit.get("enabled", "unknown")
    unit_active = collector_unit.get("active", "unknown")
    autostart = cfg.collector.enabled and unit_enabled == "enabled"

    items = [
        DeferredItem(
            capability="gps_location",
            title="GPS and location",
            status="not available",
            reason="this node has no GNSS receiver and no location source in its configuration, "
                   "so nothing it records is geotagged.",
        ),
        DeferredItem(
            capability="onstar_cloud",
            title="OnStar / GM cloud data",
            status="not available",
            reason="there is no GM account, credential or cloud client anywhere in this project; "
                   "the only interface it has to the vehicle is the OBD-II connector.",
        ),
        DeferredItem(
            capability="mode22_enhanced_pids",
            title="Mode 22 GM/Ultium enhanced PIDs",
            status="deferred",
            # This said "GM/Ultium identifiers are unproven on this VIN" until
            # 2026-09-04, by which point 31 of 35 had answered and nine were
            # cross-validated.  It was a fixed string in a *generated* report,
            # which is the worst place for one: every capability report
            # published it as though it were a measurement.  Counted from the
            # confidence table instead, so it cannot drift again.
            reason=f"the live safety gate {'accepts' if mode22['accepted'] else 'refuses'} service 22 "
                   "for the unattended collector, which has not changed. "
                   f"{_enhanced_proven()} of {len(CONFIDENCE)} enumerated "
                   f"identifiers answer on this VIN and {_enhanced_production()} "
                   "are cross-validated; reading them is a separate, supervised "
                   "path (hummer-obd-enhanced) that no unattended process calls.",
        ),
        DeferredItem(
            capability="remote_commands",
            title="Remote commands (lock, unlock, precondition, start)",
            status="not available",
            reason=f"every write, control and reset service is permanently forbidden "
                   f"(including {', '.join(control_services)}), so the node is incapable of "
                   "transmitting a command, remote or otherwise.",
        ),
        DeferredItem(
            capability="collector_autostart",
            title="Continuous collector autostart",
            status="available" if autostart else "deferred",
            reason=f"collector.enabled is {str(cfg.collector.enabled).lower()} and "
                   f"hummer-collector.service is {unit_enabled}/{unit_active}; "
                   "continuous collection stays a deliberate act until a probe has been reviewed.",
        ),
        DeferredItem(
            capability="raw_log_upload",
            title="Raw transcript upload",
            status="refused",
            reason="upload.allow_raw_logs is rejected at configuration load time: a raw transcript "
                   "can contain an unmasked VIN, so transcripts never leave the Pi.",
        ),
    ]
    return [item.as_dict() for item in items]


# -- assembly ------------------------------------------------------------


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


def _where(path: str | Path) -> str:
    """Absolute form of a path, so a reader can go and look at it."""
    return str(Path(path).resolve())


def build_report(cfg: Config, *, config_source: str = "built-in defaults",
                 json_path: Optional[Path] = None,
                 gap_threshold_s: float = 60.0) -> dict[str, Any]:
    """Assemble the whole report from on-disk evidence.

    *json_path*, when given, is excluded from the evidence scan so a previous
    run's own output is never mistaken for probe evidence.
    """
    exclude = json_path.resolve() if json_path is not None else None
    services = _services_section()
    gate = _safety_section()
    sections = {
        "node": _node_section(cfg, config_source),
        "safety_gate": gate,
        "configuration": _configuration_section(cfg),
        "evidence": _evidence_section(cfg, exclude=exclude),
        "raw_logs": _raw_logs_section(cfg),
        "database": _database_section(cfg),
        "coverage": _coverage_section(cfg, gap_threshold_s=gap_threshold_s),
        "services": services,
        "deferred": _deferred_section(cfg, services, gate),
    }
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sections": sections,
    }
    return _sanitize_tree(report)


# -- rendering -----------------------------------------------------------


def _fmt(value: Any) -> str:
    if value is None:
        return "(none)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value) if value else "(none)"
    text = str(value)
    return text if text else "(none)"


def render_text(report: dict[str, Any]) -> str:
    """Render the report as plain aligned text, in the style of the smoke script."""
    sections = report.get("sections", {})
    out: list[str] = []

    def block(title: str, rows: list[tuple[str, Any]]) -> None:
        out.append(f"### {title}")
        if not rows:
            out.append("  (nothing recorded)")
            out.append("")
            return
        width = max(len(label) for label, _ in rows)
        for label, value in rows:
            out.append(f"  {label.ljust(width)}  {_fmt(value)}")
        out.append("")

    out.append(f"### {SCHEMA}")
    out.append(f"  generated {report.get('generated_at', '')}")
    out.append("")

    node = sections.get("node", {})
    device = node.get("adapter_device", {})
    block("node", [
        ("package", node.get("package_version")),
        ("python", node.get("python")),
        ("platform", node.get("platform")),
        ("project root", node.get("root")),
        ("config source", node.get("config_source")),
        ("adapter device", f"{device.get('path', '')} ({device.get('kind', 'absent')})"),
        ("device opened", device.get("opened")),
    ])

    gate = sections.get("safety_gate", {})
    block("safety gate", [
        ("allowed services", gate.get("allowed_obd_modes")),
        ("forbidden services", f"{gate.get('forbidden_service_count', 0)} permanently refused: "
                               f"{' '.join(gate.get('forbidden_services', []))}"),
        ("adapter commands", f"{gate.get('adapter_commands_exact', 0)} exact, "
                             f"{gate.get('adapter_command_patterns', 0)} patterns"),
        ("samples accepted", f"{sum(1 for c in gate.get('checked_accepted', []) if c['accepted'])}"
                             f"/{len(gate.get('checked_accepted', []))}"),
        ("samples refused", f"{sum(1 for c in gate.get('checked_refused', []) if not c['accepted'])}"
                            f"/{len(gate.get('checked_refused', []))}"),
        ("refused sample set", [c["command"] for c in gate.get("checked_refused", [])]),
    ])

    config = sections.get("configuration", {})
    collector = config.get("collector", {})
    upload = config.get("upload", {})
    block("configuration", [
        ("collector enabled", collector.get("enabled")),
        ("collector pids", collector.get("pids")),
        ("poll interval", f"{collector.get('poll_interval_s')}s"),
        ("dtc interval", f"{collector.get('dtc_interval_s')}s"),
        ("run limits", f"max_cycles={collector.get('max_cycles')} "
                       f"duration_s={collector.get('duration_s')}"),
        ("database", collector.get("database")),
        ("raw log dir", collector.get("raw_log_dir")),
        ("upload enabled", upload.get("enabled")),
        ("upload endpoint", upload.get("endpoint")),
        ("upload raw logs", upload.get("allow_raw_logs")),
        ("display enabled", config.get("display", {}).get("enabled")),
    ])

    evidence = sections.get("evidence", {})
    merged = evidence.get("merged", {})
    samples = merged.get("samples", {})
    answered = [pid for pid, item in samples.items() if item.get("status") == "ok"]
    rows = [
        ("evidence dir", evidence.get("directory")),
        ("summaries merged", evidence.get("summaries")),
        ("sessions", evidence.get("sessions")),
        ("adapter", merged.get("adapter_id")),
        ("protocol", merged.get("protocol")),
        ("supported pids", merged.get("supported_pids")),
        ("pids answered", answered),
        ("vin", merged.get("vin_masked")),
        ("vin status", merged.get("vin_status")),
    ]
    for mode, item in sorted(merged.get("dtcs", {}).items()):
        rows.append((f"dtc mode {mode}", f"{item.get('codes') or '(none)'} [{item.get('status')}]"))
    if evidence.get("note"):
        rows.append(("note", evidence["note"]))
    block("evidence (probe summaries)", rows)

    logs = sections.get("raw_logs", {})
    totals = logs.get("totals", {})
    rows = [
        ("raw log dir", logs.get("directory")),
        ("files", totals.get("files", 0)),
        ("size", f"{totals.get('size_bytes', 0)} bytes"),
        ("records", f"{totals.get('records', 0)} "
                    f"({totals.get('tx', 0)} tx, {totals.get('rx', 0)} rx, "
                    f"{totals.get('events', 0)} event, {totals.get('corrupt', 0)} corrupt)"),
    ]
    for item in logs.get("files", [])[-5:]:
        rows.append((item["name"], f"{item['size_bytes']}B {item['records']} records "
                                   f"sha256:{item['sha256'][:12]}"))
    if logs.get("note"):
        rows.append(("note", logs["note"]))
    block("raw transcripts (metadata only)", rows)

    db = sections.get("database", {})
    rows = [
        ("database", db.get("path")),
        ("opened", "read-only" if db.get("present") else "absent"),
        ("schema version", db.get("schema_version")),
    ]
    for name, count in sorted(db.get("tables", {}).items()):
        rows.append((f"table {name}", count))
    for pid, item in sorted(db.get("latest_values", {}).items()):
        if item.get("recorded"):
            rows.append((f"latest {pid}", f"{item.get('value')} {item.get('unit')} "
                                          f"[{item.get('status')}] at {item.get('ts')}"))
        else:
            rows.append((f"latest {pid}", f"{item.get('request')} configured, never recorded"))
    for mode, item in sorted(db.get("dtc_summary", {}).items()):
        rows.append((f"dtc mode {mode}", f"{item.get('reads')} reads, latest {item.get('latest')}, "
                                         f"codes seen {_fmt(item.get('ever_reported_codes'))}, "
                                         f"{item.get('distinct_reply_frames')} reply frames"))
    for item in db.get("vehicle_info", []):
        rows.append((f"info {item.get('item')}", item.get("value")))
    sessions = db.get("sessions", [])
    if sessions:
        rows.append(("sessions", len(sessions)))
        last = sessions[-1]
        rows.append(("latest session", f"{last.get('session_uid')} started {last.get('started_at')} "
                                       f"{last.get('protocol') or 'no protocol'}"))
    if db.get("upload_queue_depth") is not None:
        rows.append(("upload queue", f"{db['upload_queue_depth']} rows unsent (upload off = all of them)"))
    for key in ("note", "error"):
        if db.get(key):
            rows.append((key, db[key]))
    block("database (read-only)", rows)

    coverage = sections.get("coverage", {})
    cov_sessions = coverage.get("sessions", {})
    rows = [
        ("gap threshold", f"{coverage.get('gap_threshold_s')}s"),
        ("sessions", cov_sessions.get("count", 0)),
        ("first sample", coverage.get("first_sample")),
        ("last sample", coverage.get("last_sample")),
        ("total span", _duration(coverage.get("total_span_seconds"))),
        ("observed", _duration(coverage.get("observed_seconds", 0.0))),
        ("coverage ratio", "(none)" if coverage.get("coverage_ratio") is None
                           else f"{float(coverage['coverage_ratio']):.3f}"),
        ("longest gap", _duration(coverage.get("longest_gap_s"))),
    ]
    for item in cov_sessions.get("recent", []):
        rows.append((f"session {item['session_uid']}",
                     f"{item['started_at']} -> {item['ended_at']} ({item['sample_count']} samples)"))
    if cov_sessions.get("omitted"):
        rows.append(("older sessions omitted", cov_sessions["omitted"]))
    for gap in coverage.get("gaps", []):
        rows.append((f"gap after {gap['after_session']}",
                     f"{_duration(gap['seconds'])} until {gap['before_session']} "
                     f"({gap['start']} -> {gap['end']})"))
    for key in ("note", "error"):
        if coverage.get(key):
            rows.append((key, coverage[key]))
    block("collection coverage", rows)

    services = sections.get("services", {})
    rows = [(unit, f"{state.get('enabled')}/{state.get('active')}")
            for unit, state in sorted(services.get("units", {}).items())]
    if services.get("note"):
        rows.append(("note", services["note"]))
    block("services (enabled/active)", rows)

    out.append("### not available / deferred")
    for item in sections.get("deferred", []):
        out.append(f"  {item['title']} [{item['status']}]")
        out.append(textwrap.fill(item["reason"], width=88,
                                 initial_indent="      ", subsequent_indent="      "))
    out.append("")
    return "\n".join(out).rstrip() + "\n"


# -- entry point ---------------------------------------------------------


def _load(config_arg: Optional[str], root: Optional[str]) -> tuple[Config, str]:
    """Load the configuration this node actually runs on.

    With no ``--config``, the on-disk config is still preferred over defaults:
    a report that silently described built-in defaults while a real
    ``config/hummer.toml`` sat next to it would be worse than no report, so the
    source is discovered and then named in the output.

    With a ``--config`` but no ``--root``, the root is taken from the config's
    own location the way :func:`load_config` does.  Defaulting to the working
    directory instead made ``--config /opt/hummer/config/hummer.toml`` report
    "nothing has been recorded on this node" from any other directory, which is
    the most misleading answer this tool can give.
    """
    if config_arg:
        if root is None:
            # load_config resolves the root as the config's grandparent
            # (config/hummer.toml -> the project directory).
            cfg = load_config(config_arg)
            return cfg, str(config_arg)
        return load_config(config_arg, root=Path(root)), str(config_arg)
    root_path = Path(root if root is not None else ".")
    discovered = root_path / "config" / "hummer.toml"
    if discovered.is_file():
        return load_config(discovered, root=root_path), str(discovered)
    return load_config(root=root_path), "built-in defaults"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline capabilities report; never opens the adapter or the vehicle"
    )
    parser.add_argument("--config", help="path to hummer.toml (default: <root>/config/hummer.toml)")
    parser.add_argument(
        "--root",
        default=None,
        help="project root for relative paths (default: the parent of the "
             "config file's directory, or the working directory)",
    )
    parser.add_argument("--json", dest="json_path",
                        help=f"write the JSON report here (default: <root>/evidence/{DEFAULT_JSON_NAME})")
    parser.add_argument("--no-json", action="store_true", help="do not write a JSON report")
    parser.add_argument("--quiet", action="store_true", help="suppress the console report")
    parser.add_argument(
        "--gap-threshold-s",
        dest="gap_threshold_s",
        type=float,
        default=60.0,
        help="minimum length, in seconds, of a gap between sessions worth reporting (default: 60.0)",
    )
    args = parser.parse_args(argv)

    try:
        cfg, config_source = _load(args.config, args.root)
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read configuration: {exc}", file=sys.stderr)
        return 2

    destination: Optional[Path] = None
    if not args.no_json:
        destination = Path(args.json_path) if args.json_path else Path(cfg.root) / "evidence" / DEFAULT_JSON_NAME

    report = build_report(
        cfg, config_source=config_source, json_path=destination, gap_threshold_s=args.gap_threshold_s
    )

    written = ""
    if destination is not None:
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            written = str(destination)
        except OSError as exc:
            # A report that was produced is still a report; failing to file it
            # is worth saying out loud but is not worth failing the run over.
            print(f"WARNING: could not write {destination}: {exc}", file=sys.stderr)

    if not args.quiet:
        print(render_text(report), end="")
        if written:
            print(f"# report written to {written}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
