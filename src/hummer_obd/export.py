"""Local export of the collected telemetry, in a form something else can read.

The SQLite buffer is the collector's working file, not a format anyone else
should have to learn.  This module writes it out as a self-describing local
file that a notebook, a spreadsheet or a language model can ingest without this
repository, without the schema in front of it, and without having to guess what
``0142`` means.

It is a file writer and nothing more:

* it never opens the serial device.  An export does not involve the vehicle, so
  neither ``transport`` nor ``pyserial`` is imported here,
* it never uploads.  Output goes to a local path or to stdout; where the file
  travels afterwards is a decision the operator makes in the open,
* it opens the database read-only (``mode=ro``), so exporting can neither
  damage the buffer the collector is filling nor advance its upload queue,
* every emitted string goes through the project's masking policy: a VIN-shaped
  token is masked with :func:`decode.mask_vin`, MAC and IP addresses are
  redacted, and the raw-log path is reduced to a bare file name,
* two exports of the same database differ only in the timestamp they were
  taken at, which ``--export-time`` pins, so a diff means the data changed.

One thing it deliberately does not sanitise is ``raw_hex``.  Those are the
bytes the vehicle actually sent and fidelity is the whole point of an export —
but response bytes spell out VINs, calibration IDs and ECU names in ASCII, so
an export file is private data even though its VIN fields are masked.  Treat it
the way the raw log is treated: evidence, not a publication.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence, TextIO

from .config import load_config
from .decode import PID_DECODERS, mask_vin

__all__ = [
    "SCHEMA_ID",
    "KINDS",
    "ExportOptions",
    "Exporter",
    "open_readonly",
    "redact",
    "main",
]

#: Identifies the shape of the output.  It is written into every export so a
#: consumer can tell one generation of this file from the next.
SCHEMA_ID = "hummer-obd/export/1"

#: Record kinds, in the order the meta block describes them.
KINDS = ("session", "sample", "dtc_read", "event")

#: ``--include`` names map onto record kinds; "all" expands to every kind.
_INCLUDE_KINDS = {
    "sessions": ("session",),
    "samples": ("sample",),
    "dtcs": ("dtc_read",),
    "events": ("event",),
    "all": KINDS,
}

#: Key each kind lands under in the single-object ``json`` format.
_JSON_KEY = {"session": "sessions", "sample": "samples", "dtc_read": "dtc_reads", "event": "events"}

_DTC_MODE_MEANING = {
    "03": "confirmed diagnostic trouble codes stored by the vehicle",
    "07": "pending codes from the current or last completed drive cycle",
    "0A": "permanent codes, which only the vehicle itself can clear",
}

_FIELD_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "session": {
        "kind": 'always "session": one run of the collector or probe',
        "id": "row id in the source database, unique within this kind",
        "ts": "UTC ISO-8601 time the session started (same as started_at)",
        "session_uid": "stable session name other records refer to",
        "started_at": "UTC ISO-8601 time the session opened",
        "ended_at": "UTC ISO-8601 time the session closed, null if it did not",
        "duration_s": "seconds between started_at and ended_at, null if unknown",
        "adapter_id": "adapter self-identification string (ELM/STN ATI reply)",
        "protocol": "OBD protocol the adapter negotiated with the vehicle",
        "raw_log_file": "file name of the private raw transcript, directory removed",
        "notes": "free-text note recorded when the session opened",
    },
    "sample": {
        "kind": 'always "sample": one decoded service 01 reading',
        "id": "row id in the source database, unique within this kind",
        "ts": "UTC ISO-8601 time the reading was stored",
        "session_uid": "session this reading was taken in",
        "pid": "service 01 parameter id, two hex digits",
        "ecu": "address of the module that gave this answer; several modules "
               "answer the same request with their own values, and an empty "
               "string means the reading predates per-module attribution",
        "request": "the exact read-only request that produced it",
        "name": "human name of the parameter",
        "value": "decoded numeric value, null when the vehicle did not answer",
        "unit": "unit that value is expressed in",
        "status": '"ok" when decoded; otherwise why not ("no_data", "unmatched", '
                  '"short_frame", "undecoded", "error", "incomplete", "empty")',
        "meaning": "one line saying what this parameter measures",
        "raw_hex": "response bytes as hex, exactly as the vehicle sent them",
        "uploaded_at": "UTC time an opt-in uploader confirmed the row, else null",
    },
    "dtc_read": {
        "kind": 'always "dtc_read": one read-only trouble-code query',
        "id": "row id in the source database, unique within this kind",
        "ts": "UTC ISO-8601 time of the read",
        "session_uid": "session the read was taken in",
        "mode": "OBD service used: 03 stored, 07 pending, 0A permanent",
        "mode_meaning": "what that service returns",
        "codes": "comma-separated trouble codes, empty when the vehicle reported none",
        "code_count": "number of codes in the codes field",
        "raw_hex": "response bytes as hex, exactly as the vehicle sent them",
        "uploaded_at": "UTC time an opt-in uploader confirmed the row, else null",
    },
    "event": {
        "kind": 'always "event": something the node noticed, not something the vehicle said',
        "id": "row id in the source database, unique within this kind",
        "ts": "UTC ISO-8601 time of the event",
        "session_uid": "session it happened in, null for node-wide events",
        "event": 'event name, for example "connected", "idle_backoff", "transport_error"',
        "detail": "free-text detail",
    },
}

# A VIN is 17 characters from a restricted alphabet (no I, O or Q).  The
# lookarounds keep the match to a whole token, so a longer hex or base64 run is
# left alone rather than being mangled into a fake VIN.  Matching is
# case-insensitive for the same reason ``uploader._refuse_if_vin_shaped``
# upper-cases before it scans: a VIN typed into a free-text note in lower case
# is still a VIN, and over-masking an odd-length hex run is the safe failure.
# (I, O and Q stay excluded in both cases, so the alphabet is unchanged.)
_VIN_RE = re.compile(r"(?<![0-9A-Za-z])[A-HJ-NPR-Z0-9]{17}(?![0-9A-Za-z])", re.IGNORECASE)
# Both written forms of a MAC, colon and hyphen.  The run is matched whole
# (five *or more* groups) so a longer EUI-64 is redacted in one piece instead
# of leaving its head behind, and the lookbehind excludes only a hex digit:
# excluding the separator too would let the common "mac:00:04:3E:AA:BB:CC"
# spelling slip through unredacted.
_MAC_RE = re.compile(r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}[:-]){5,}[0-9A-Fa-f]{2}(?![0-9A-Fa-f])")
_IPV4_RE = re.compile(r"(?<![0-9.])(?:\d{1,3}\.){3}\d{1,3}(?![0-9.])")
# Only the unambiguous IPv6 forms: a full eight-group address, or one using the
# "::" contraction.  A looser pattern would eat the colons in a timestamp.
_IPV6_RE = re.compile(
    r"(?<![0-9A-Za-z:.])"
    r"(?:(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
    r"|(?:[0-9A-Fa-f]{1,4}:){1,7}:(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6})?)"
    r"(?![0-9A-Za-z:.])"
)


def redact(text: str) -> str:
    """Return *text* with vehicle and network identifiers removed.

    The database should not contain a MAC or an address in the first place, but
    a free-text ``notes`` or ``detail`` field is exactly where one ends up by
    accident, and an export is the moment such a string would leave the Pi.
    """
    if not text:
        return text
    out = _VIN_RE.sub(lambda m: mask_vin(m.group(0)), text)
    out = _MAC_RE.sub("(mac redacted)", out)
    out = _IPV6_RE.sub("(ip redacted)", out)
    return _IPV4_RE.sub("(ip redacted)", out)


def _scrub(value: Any) -> Any:
    return redact(value) if isinstance(value, str) else value


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp as UTC, returning None if it is unusable."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if moment.tzinfo is None:
        # Stored timestamps are always UTC; a naive one on the command line is
        # read the same way rather than silently taken as local time.
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _utc_text(value: Optional[str]) -> Optional[str]:
    """Normalise a stored timestamp to UTC ISO-8601, or pass it through."""
    if not value:
        return None
    moment = _parse_ts(value)
    return _iso(moment) if moment else redact(value)


def open_readonly(path: str | Path) -> sqlite3.Connection:
    """Open the collector database read-only.

    Read-only is the connection mode, not a convention: an export that could
    write is an export that could corrupt the evidence it is copying.

    SQLite still needs the ``-shm`` shared-memory index to read a WAL database,
    and creates it (plus an empty ``-wal``) beside the file if the directory
    allows it.  That is how a reader sees commits a running collector has not
    checkpointed yet; the database file itself is never modified.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass
class ExportOptions:
    database: Path
    fmt: str = "jsonl"
    since: Optional[datetime] = None
    until: Optional[datetime] = None
    sessions: tuple[str, ...] = ()
    limit: int = 0
    include: tuple[str, ...] = KINDS
    #: Stamp written into the meta block.  Fixing it makes an export
    #: byte-reproducible; left empty it is the current time.
    exported_at: str = ""


class Exporter:
    """Reads the buffer and writes it out.  Owns no connection between calls."""

    def __init__(self, options: ExportOptions) -> None:
        self.options = options

    # -- filtering -------------------------------------------------------
    def _in_window(self, moment: Optional[datetime]) -> bool:
        if moment is None:
            # An unparseable timestamp cannot be shown to be inside the window,
            # so it only survives when no window was asked for.
            return self.options.since is None and self.options.until is None
        if self.options.since is not None and moment < self.options.since:
            return False
        if self.options.until is not None and moment > self.options.until:
            return False
        return True

    def _session_overlaps(self, started: Optional[datetime], ended: Optional[datetime]) -> bool:
        """A session is context for the rows in the window, not a row in it.

        Filtering sessions by their start alone would strip the adapter and
        protocol rows that explain the samples an operator asked for, so a
        session is kept whenever its interval overlaps the requested window.
        """
        if self.options.since is not None and ended is not None and ended < self.options.since:
            return False
        if self.options.until is not None and started is not None and started > self.options.until:
            return False
        return True

    def _wanted_session(self, session_uid: Optional[str]) -> bool:
        if not self.options.sessions:
            return True
        return session_uid in self.options.sessions

    def _finish(self, records: list[dict]) -> list[dict]:
        """Sort, apply ``--limit`` and scrub, in that order."""
        records.sort(key=lambda r: (r["ts"] or "", r["id"]))
        if self.options.limit:
            records = records[-self.options.limit:]
        return [{k: _scrub(v) for k, v in record.items()} for record in records]

    # -- readers ---------------------------------------------------------
    def _sessions(self, conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            "SELECT id, session_uid, started_at, ended_at, adapter_id, protocol,"
            " raw_log_path, notes FROM sessions"
        ).fetchall()
        out: list[dict] = []
        for row in rows:
            if not self._wanted_session(row["session_uid"]):
                continue
            started = _parse_ts(row["started_at"])
            ended = _parse_ts(row["ended_at"])
            if not self._session_overlaps(started, ended):
                continue
            duration = round((ended - started).total_seconds(), 3) if started and ended else None
            out.append({
                "kind": "session",
                "id": int(row["id"]),
                "ts": _utc_text(row["started_at"]),
                "session_uid": row["session_uid"],
                "started_at": _utc_text(row["started_at"]),
                "ended_at": _utc_text(row["ended_at"]),
                "duration_s": duration,
                "adapter_id": row["adapter_id"] or "",
                "protocol": row["protocol"] or "",
                # Only the file name: the directory layout of the node is not
                # part of the telemetry, and the transcript itself never leaves.
                "raw_log_file": Path(row["raw_log_path"]).name if row["raw_log_path"] else "",
                "notes": row["notes"] or "",
            })
        return self._finish(out)

    def _samples(self, conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            "SELECT s.id, s.ts, s.pid, s.name, s.value, s.unit, s.status, s.raw_hex, s.ecu,"
            " s.uploaded_at, sess.session_uid AS session_uid"
            " FROM samples s LEFT JOIN sessions sess ON sess.id = s.session_id"
        ).fetchall()
        out: list[dict] = []
        for row in rows:
            if not self._wanted_session(row["session_uid"]):
                continue
            if not self._in_window(_parse_ts(row["ts"])):
                continue
            pid = (row["pid"] or "").upper()
            # The decoder table is the authority on what a PID means; the stored
            # name is only a fallback for a PID this project does not decode.
            meta = PID_DECODERS.get(pid)
            if meta:
                unit = meta["unit"]
                meaning = f"service 01 PID {pid}: {meta['name']}"
                meaning += f", reported in {unit}" if unit else ""
                name = meta["name"]
            else:
                name = row["name"] or f"PID {pid}"
                unit = row["unit"] or ""
                meaning = (f"service 01 PID {pid}: this project has no decoder for it; "
                           "raw_hex holds what the vehicle sent")
            out.append({
                "kind": "sample",
                "id": int(row["id"]),
                "ts": _utc_text(row["ts"]),
                "session_uid": row["session_uid"],
                "pid": pid,
                # Which module answered.  Several modules answer the same
                # request with different values, so an export that dropped
                # this would turn a distribution into an unattributed list.
                "ecu": row["ecu"] or "",
                "request": f"01{pid}",
                "name": name,
                "value": float(row["value"]) if row["value"] is not None else None,
                "unit": unit,
                "status": row["status"],
                "meaning": meaning,
                "raw_hex": row["raw_hex"] or "",
                "uploaded_at": _utc_text(row["uploaded_at"]),
            })
        return self._finish(out)

    def _dtc_reads(self, conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            "SELECT d.id, d.ts, d.mode, d.codes, d.raw_hex, d.uploaded_at,"
            " sess.session_uid AS session_uid"
            " FROM dtc_reads d LEFT JOIN sessions sess ON sess.id = d.session_id"
        ).fetchall()
        out: list[dict] = []
        for row in rows:
            if not self._wanted_session(row["session_uid"]):
                continue
            if not self._in_window(_parse_ts(row["ts"])):
                continue
            mode = (row["mode"] or "").upper()
            codes = row["codes"] or ""
            out.append({
                "kind": "dtc_read",
                "id": int(row["id"]),
                "ts": _utc_text(row["ts"]),
                "session_uid": row["session_uid"],
                "mode": mode,
                "mode_meaning": _DTC_MODE_MEANING.get(mode, f"OBD service {mode}"),
                "codes": codes,
                "code_count": len([c for c in codes.split(",") if c]),
                "raw_hex": row["raw_hex"] or "",
                "uploaded_at": _utc_text(row["uploaded_at"]),
            })
        return self._finish(out)

    def _events(self, conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            "SELECT e.id, e.ts, e.kind AS event, e.detail, sess.session_uid AS session_uid"
            " FROM events e LEFT JOIN sessions sess ON sess.id = e.session_id"
        ).fetchall()
        out: list[dict] = []
        for row in rows:
            if not self._wanted_session(row["session_uid"]):
                continue
            if not self._in_window(_parse_ts(row["ts"])):
                continue
            out.append({
                "kind": "event",
                "id": int(row["id"]),
                "ts": _utc_text(row["ts"]),
                "session_uid": row["session_uid"],
                "event": row["event"],
                "detail": row["detail"] or "",
            })
        return self._finish(out)

    # -- assembly --------------------------------------------------------
    def collect(self) -> dict[str, list[dict]]:
        readers = {
            "session": self._sessions,
            "sample": self._samples,
            "dtc_read": self._dtc_reads,
            "event": self._events,
        }
        with closing(open_readonly(self.options.database)) as conn:
            return {kind: readers[kind](conn) for kind in KINDS if kind in self.options.include}

    def meta(self, groups: dict[str, list[dict]]) -> dict:
        """The block that makes the file readable without this codebase."""
        exported_at = self.options.exported_at or _iso(datetime.now(timezone.utc))
        return {
            "kind": "meta",
            "schema": SCHEMA_ID,
            "exported_at": exported_at,
            # Basename only: the path to the node's data directory is not
            # telemetry and does not belong in a file meant to be shared.
            "source_database": redact(Path(self.options.database).name),
            "counts": {kind: len(rows) for kind, rows in groups.items()},
            "records": sum(len(rows) for rows in groups.values()),
            "filters": {
                "since": _iso(self.options.since) if self.options.since else None,
                "until": _iso(self.options.until) if self.options.until else None,
                # Echoed from the command line, so it gets the same masking as
                # the session_uid on every record below; otherwise
                # "--session <VIN>" would write the VIN back out in clear.
                "sessions": [redact(uid) for uid in self.options.sessions],
                "limit": self.options.limit,
                "include": [kind for kind in KINDS if kind in self.options.include],
            },
            "description": (
                "Read-only OBD-II telemetry from a GMC Hummer EV, exported from the "
                "collector's local SQLite buffer. Every record carries a kind and a UTC "
                "ts; the fields block below describes each column. Values are as the "
                "vehicle reported them: a null value means the vehicle did not answer, "
                "not zero. Records are sorted by (kind, ts, id) and --limit keeps the "
                "most recent rows of each kind."
            ),
            "fields": {kind: _FIELD_DESCRIPTIONS[kind] for kind in groups},
            "privacy": (
                "VIN-shaped strings are masked and MAC/IP addresses are redacted, but "
                "raw_hex is unmodified response data and can encode vehicle identifiers. "
                "Treat this file as private."
            ),
        }

    # -- writers ---------------------------------------------------------
    def write(self, stream: TextIO) -> dict[str, list[dict]]:
        groups = self.collect()
        meta = self.meta(groups)
        if self.options.fmt == "json":
            document: dict[str, Any] = {"meta": meta}
            for kind, rows in groups.items():
                document[_JSON_KEY[kind]] = rows
            stream.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
            return groups
        flat = sorted(
            (record for rows in groups.values() for record in rows),
            key=lambda r: (r["kind"], r["ts"] or "", r["id"]),
        )
        if self.options.fmt == "csv":
            _write_csv(stream, flat)
            return groups
        # jsonl: the meta record first, then one record per line.  Key order is
        # the insertion order above, so "kind" leads every line and two exports
        # of the same rows are byte-identical.
        stream.write(json.dumps(meta) + "\n")
        for record in flat:
            stream.write(json.dumps(record) + "\n")
        return groups


def _write_csv(stream: TextIO, records: Sequence[dict]) -> None:
    """One table for every kind: a leading kind column, then the union."""
    columns: set[str] = set()
    for record in records:
        columns.update(record)
    columns.discard("kind")
    header = ["kind"] + sorted(columns)
    writer = csv.DictWriter(stream, fieldnames=header, restval="", lineterminator="\n")
    writer.writeheader()
    for record in records:
        writer.writerow(record)


def _resolve_include(values: Optional[list[str]]) -> tuple[str, ...]:
    if not values:
        return KINDS
    wanted: set[str] = set()
    for value in values:
        wanted.update(_INCLUDE_KINDS[value])
    return tuple(kind for kind in KINDS if kind in wanted)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Export collected telemetry to a local file (read-only, never uploads)"
    )
    parser.add_argument("--config", help="path to hummer.toml")
    parser.add_argument("--root", default=".", help="project root for relative paths")
    parser.add_argument("--format", choices=("jsonl", "csv", "json"), default="jsonl",
                        help="output format (default: jsonl)")
    parser.add_argument("--output", default="-", help='write here ("-" means stdout)')
    parser.add_argument("--since", help="keep records at or after this UTC ISO-8601 time")
    parser.add_argument("--until", help="keep records at or before this UTC ISO-8601 time")
    parser.add_argument("--session", action="append", default=[], metavar="UID",
                        help="restrict to this session uid (repeatable)")
    parser.add_argument("--limit", type=int, default=0,
                        help="keep at most N of the most recent records of each kind (0: all)")
    parser.add_argument("--include", action="append",
                        choices=("samples", "dtcs", "sessions", "events", "all"),
                        help="record kinds to export (repeatable, default: all)")
    parser.add_argument("--export-time",
                        help="stamp the export with this UTC time instead of now, "
                             "for a byte-reproducible file")
    args = parser.parse_args(argv)

    if args.limit < 0:
        print("--limit must not be negative", file=sys.stderr)
        return 2
    times: dict[str, Optional[datetime]] = {}
    for name in ("since", "until", "export_time"):
        text = getattr(args, name)
        if text is None:
            times[name] = None
            continue
        moment = _parse_ts(text)
        if moment is None:
            print(f"--{name.replace('_', '-')}: {text!r} is not an ISO-8601 timestamp",
                  file=sys.stderr)
            return 2
        times[name] = moment
    if times["since"] and times["until"] and times["since"] > times["until"]:
        print("--since is after --until; that window selects nothing", file=sys.stderr)
        return 2

    cfg = load_config(args.config, root=args.root) if args.config else load_config(root=args.root)
    database = Path(cfg.path(cfg.collector.database))
    options = ExportOptions(
        database=database,
        fmt=args.format,
        since=times["since"],
        until=times["until"],
        sessions=tuple(args.session),
        limit=args.limit,
        include=_resolve_include(args.include),
        exported_at=_iso(times["export_time"]) if times["export_time"] else "",
    )
    exporter = Exporter(options)

    to_stdout = args.output in ("-", "")
    # Render first, write second.  Writing straight into the destination opens
    # it for truncation before the database has been read, so a missing or
    # unreadable buffer would destroy the previous export at that path.  The
    # rendered text costs less memory than the record dicts it is built from,
    # which are all resident already.
    buffer = io.StringIO()
    try:
        groups = exporter.write(buffer)
    except FileNotFoundError:
        print(f"no collector database at {database}; nothing has been recorded yet "
              f"(check collector.database in the configuration)", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"cannot read {database} read-only: {exc}; if the collector is running, "
              f"export from a copy of the database", file=sys.stderr)
        return 1

    try:
        if to_stdout:
            sys.stdout.write(buffer.getvalue())
        else:
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8", newline="") as fh:
                fh.write(buffer.getvalue())
    except OSError as exc:
        print(f"cannot write {args.output}: {exc}", file=sys.stderr)
        return 1

    if not to_stdout:
        total = sum(len(rows) for rows in groups.values())
        print(f"wrote {total} records to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
