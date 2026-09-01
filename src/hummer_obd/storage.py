"""SQLite storage for decoded samples, DTC reads and vehicle information.

The raw JSONL transcript is the source of truth; this database is the queryable
mirror the collector fills.  It is deliberately small and boring:

* WAL journal so a power cut cannot corrupt the file mid-write,
* one row per decoded sample, with the raw hex kept alongside the value,
* an ``uploaded_at`` column that acts as the local buffer/queue marker — rows
  accumulate on disk and are only marked when an (opt-in) uploader confirms
  them.  With upload disabled, this degrades to "everything stays local".
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

__all__ = ["Storage", "SCHEMA_VERSION"]

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_uid    TEXT NOT NULL UNIQUE,
    started_at     TEXT NOT NULL,
    ended_at       TEXT,
    adapter_id     TEXT,
    protocol       TEXT,
    raw_log_path   TEXT,
    notes          TEXT
);

CREATE TABLE IF NOT EXISTS samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    ts          TEXT NOT NULL,
    pid         TEXT NOT NULL,
    name        TEXT NOT NULL,
    value       REAL,
    unit        TEXT,
    status      TEXT NOT NULL,
    raw_hex     TEXT NOT NULL,
    uploaded_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_session_ts ON samples(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_samples_pending ON samples(uploaded_at) WHERE uploaded_at IS NULL;

CREATE TABLE IF NOT EXISTS dtc_reads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    ts          TEXT NOT NULL,
    mode        TEXT NOT NULL,
    codes       TEXT NOT NULL,
    raw_hex     TEXT NOT NULL,
    uploaded_at TEXT
);

CREATE TABLE IF NOT EXISTS vehicle_info (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL REFERENCES sessions(id),
    ts            TEXT NOT NULL,
    item          TEXT NOT NULL,
    value_masked  TEXT NOT NULL,
    raw_hex       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    session_id INTEGER REFERENCES sessions(id),
    kind       TEXT NOT NULL,
    detail     TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    """Thin, explicit wrapper around the collector's SQLite database."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        with closing(self.conn.execute("SELECT version FROM schema_version")) as cur:
            row = cur.fetchone()
        if row is None:
            self.conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
        elif row["version"] != SCHEMA_VERSION:
            raise RuntimeError(
                f"database {self.path} has schema version {row['version']}, expected {SCHEMA_VERSION}"
            )

    # -- sessions --------------------------------------------------------
    def start_session(
        self,
        session_uid: str,
        *,
        adapter_id: str = "",
        protocol: str = "",
        raw_log_path: str = "",
        notes: str = "",
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO sessions(session_uid, started_at, adapter_id, protocol, raw_log_path, notes)"
            " VALUES (?,?,?,?,?,?)",
            (session_uid, _now(), adapter_id, protocol, raw_log_path, notes),
        )
        return int(cur.lastrowid)

    def end_session(self, session_id: int) -> None:
        self.conn.execute("UPDATE sessions SET ended_at=? WHERE id=?", (_now(), session_id))

    def update_session(self, session_id: int, **fields) -> None:
        allowed = {"adapter_id", "protocol", "raw_log_path", "notes"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"cannot update session fields {sorted(unknown)}")
        for key, value in fields.items():
            self.conn.execute(f"UPDATE sessions SET {key}=? WHERE id=?", (value, session_id))

    # -- writes ----------------------------------------------------------
    def add_sample(self, session_id: int, sample) -> int:
        cur = self.conn.execute(
            "INSERT INTO samples(session_id, ts, pid, name, value, unit, status, raw_hex)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                session_id,
                _now(),
                sample.pid,
                sample.name,
                sample.value,
                sample.unit,
                sample.status,
                sample.raw_hex,
            ),
        )
        return int(cur.lastrowid)

    def add_dtc_read(self, session_id: int, mode: str, codes: Iterable[str], raw_hex: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO dtc_reads(session_id, ts, mode, codes, raw_hex) VALUES (?,?,?,?,?)",
            (session_id, _now(), mode, ",".join(codes), raw_hex),
        )
        return int(cur.lastrowid)

    def add_vehicle_info(self, session_id: int, item: str, value_masked: str, raw_hex: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO vehicle_info(session_id, ts, item, value_masked, raw_hex) VALUES (?,?,?,?,?)",
            (session_id, _now(), item, value_masked, raw_hex),
        )
        return int(cur.lastrowid)

    def add_event(self, kind: str, detail: str = "", session_id: Optional[int] = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO events(ts, session_id, kind, detail) VALUES (?,?,?,?)",
            (_now(), session_id, kind, detail),
        )
        return int(cur.lastrowid)

    # -- local buffer / upload queue -------------------------------------
    def pending_samples(self, limit: int = 200) -> list[sqlite3.Row]:
        with closing(
            self.conn.execute(
                "SELECT * FROM samples WHERE uploaded_at IS NULL ORDER BY id LIMIT ?", (limit,)
            )
        ) as cur:
            return cur.fetchall()

    def mark_uploaded(self, sample_ids: Iterable[int]) -> int:
        ids = list(sample_ids)
        if not ids:
            return 0
        ts = _now()
        self.conn.executemany(
            "UPDATE samples SET uploaded_at=? WHERE id=?", [(ts, i) for i in ids]
        )
        return len(ids)

    def pending_count(self) -> int:
        with closing(self.conn.execute("SELECT COUNT(*) AS n FROM samples WHERE uploaded_at IS NULL")) as cur:
            return int(cur.fetchone()["n"])

    def latest_samples(self, limit: int = 10) -> list[sqlite3.Row]:
        with closing(
            self.conn.execute("SELECT * FROM samples ORDER BY id DESC LIMIT ?", (limit,))
        ) as cur:
            return cur.fetchall()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
