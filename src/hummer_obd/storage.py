"""SQLite storage for decoded samples, monitor tests, DTCs and vehicle information.

The raw JSONL transcript is the source of truth; this database is the queryable
mirror the collector fills.  It is deliberately small and boring:

* WAL journal so a power cut cannot corrupt the file mid-write,
* one row per decoded sample *per responding module*, with the raw hex kept
  alongside the value.  A single request on this vehicle is answered by up to
  eight ECUs at once, and folding those answers into one row would silently
  pick a winner: "the battery pack said 51 degC and the drive unit said 38" is
  the observation, not one of the two numbers,
* an ``uploaded_at`` column that acts as the local buffer/queue marker — rows
  accumulate on disk and are only marked when an (opt-in) uploader confirms
  them.  With upload disabled, this degrades to "everything stays local".

Schema changes are migrations, never rebuilds.  A node that has spent a week in
a vehicle holds readings nobody can take again, so :func:`migrate` only ever
adds — ``ALTER TABLE ... ADD COLUMN`` and ``CREATE TABLE IF NOT EXISTS``.
Nothing in this module drops, renames or recreates a table, and no row is ever
deleted.  A version this build does not understand still raises instead of
being opened hopefully: writing into a layout you have guessed at is how
evidence gets destroyed quietly.

``ADD COLUMN`` appends, so a migrated database and a freshly created one differ
in column *order*.  Every statement here names the columns it touches, and
readers elsewhere in the project do the same; nothing may depend on position.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

__all__ = ["Storage", "SCHEMA_VERSION", "migrate"]

SCHEMA_VERSION = 2

_BASE_SCHEMA = """
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
    ecu         TEXT NOT NULL DEFAULT '',
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

#: Service 06 results, kept as separate statements rather than one script so the
#: migration can run them inside its own transaction — ``executescript`` commits
#: whatever is pending before it starts.  One definition serves both a fresh
#: database and an upgraded one, so the two cannot drift apart.
#:
#: The limits and the value are stored exactly as the module reported them
#: (raw counts), and the ``scaled_*`` columns hold the same numbers converted
#: through the test's unit-and-scaling identifier.  Those are nullable on
#: purpose: an unfamiliar UASID means the scaling is unknown, and a NULL says
#: so.  A guessed conversion would be indistinguishable from a measurement.
_MONITOR_TESTS_DDL = (
    """
    CREATE TABLE IF NOT EXISTS monitor_tests (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id   INTEGER NOT NULL REFERENCES sessions(id),
        ts           TEXT NOT NULL,
        ecu          TEXT NOT NULL DEFAULT '',
        mid          INTEGER,
        tid          INTEGER,
        uasid        INTEGER,
        value        INTEGER,
        min_limit    INTEGER,
        max_limit    INTEGER,
        unit         TEXT,
        scaled_value REAL,
        scaled_min   REAL,
        scaled_max   REAL,
        raw_hex      TEXT NOT NULL DEFAULT '',
        uploaded_at  TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_monitor_tests_session_ts ON monitor_tests(session_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_monitor_tests_pending ON monitor_tests(uploaded_at)"
    " WHERE uploaded_at IS NULL",
)

_SCHEMA = _BASE_SCHEMA + "".join(f"{statement};\n" for statement in _MONITOR_TESTS_DDL)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    """Coerce a label to the string its NOT NULL column expects.

    A missing module identity is the empty string, never NULL: "no ECU was
    recorded" is a fact about the row, and a null would make every reader test
    for it separately.
    """
    return "" if value is None else str(value)


def _as_int(value: Any) -> Optional[int]:
    """Coerce a monitor-test field to the integer its column stores.

    MIDs, TIDs, UASIDs and the raw test values are bytes off the wire, and they
    are written as hex ("A1") as often as they are held as numbers.  Storing
    both forms would put ``mid = 161`` and ``mid = 'A1'`` in different rows for
    the same monitor, so a string is read as hex rather than accepted as-is.
    ``None`` and the empty string mean "the module did not report this" and are
    kept as NULL.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return int(value, 16)
    return int(value)


def _as_float(value: Any) -> Optional[float]:
    """Coerce a scaled reading to REAL, keeping "unknown" as NULL.

    ``None`` arrives here whenever the decoder could not name the scaling for a
    UASID.  It is written straight through: an un-scaled count dressed up as a
    volt reading would be worse than an admitted gap.
    """
    return None if value is None else float(value)


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Group statements that must land together, or not at all.

    The connection runs in autocommit mode so that ordinary single-row writes
    reach the disk immediately; anything that belongs together therefore has to
    say so explicitly.  ``BEGIN IMMEDIATE`` takes the write lock up front
    rather than discovering a busy database half-way through a batch.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    with closing(
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
    ) as cur:
        return cur.fetchone() is not None


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    # PRAGMA rows are read by position because this runs against connections
    # this module did not open, which may not have a row factory set.
    with closing(conn.execute(f"PRAGMA table_info({table})")) as cur:
        return {row[1] for row in cur.fetchall()}


def _current_version(conn: sqlite3.Connection) -> Optional[int]:
    with closing(conn.execute("SELECT version FROM schema_version")) as cur:
        row = cur.fetchone()
    return None if row is None else int(row[0])


def migrate(conn: sqlite3.Connection, *, path: str | Path = "") -> int:
    """Bring an existing database up to :data:`SCHEMA_VERSION` and return it.

    The reference node has been recording a real vehicle, so this upgrades in
    place: it adds the missing column, creates the missing table, and only then
    records the new version.  Every step is guarded by its own "is it already
    there?" test, which makes the whole function safe to run twice, safe to run
    against a database that is already current, and safe to interrupt — a crash
    before the final bump leaves a version-1 database that the next open
    upgrades again.

    Version 1 (and a database whose version row went missing, which the same
    additive steps repair) is understood.  Anything else — a 0, or a version 3
    written by a newer build — raises, because the alternative is a program
    that writes into a layout it does not know.
    """
    version = _current_version(conn)
    if version == SCHEMA_VERSION:
        return version
    if version is not None and version != 1:
        where = f" {path}" if path else ""
        raise RuntimeError(
            f"database{where} has schema version {version}, expected {SCHEMA_VERSION};"
            " this build does not know how to upgrade it"
        )
    with _transaction(conn):
        if "ecu" not in _column_names(conn, "samples"):
            # ADD COLUMN rewrites no rows: existing samples keep their values
            # and gain the default, which is exactly "we did not record which
            # module answered", the truth about a reading taken before this.
            conn.execute("ALTER TABLE samples ADD COLUMN ecu TEXT NOT NULL DEFAULT ''")
        for statement in _MONITOR_TESTS_DDL:
            conn.execute(statement)
        if version is None:
            conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
        else:
            conn.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))
    return SCHEMA_VERSION


class Storage:
    """Thin, explicit wrapper around the collector's SQLite database."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        # Opening the file is where an upgrade happens: there is no separate
        # migration command an operator could forget to run before the
        # collector starts writing into a database from the previous build.
        # A file that already carries a version is checked *before* anything is
        # written to it, because a version this build cannot read has to be
        # left exactly as it was found, and CREATE TABLE IF NOT EXISTS is
        # still a write.
        known = _has_table(self.conn, "schema_version")
        if known:
            migrate(self.conn, path=self.path)
        self.conn.executescript(_SCHEMA)
        if not known:
            # A file this module has not seen before.  The script above created
            # whatever was missing; migrate now stamps the version and, for a
            # stray database that had tables but no version row, adds the
            # column and table that go with it.
            migrate(self.conn, path=self.path)

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
        """Record one decoded reading, attributed to the module that sent it."""
        return self._insert_sample(session_id, sample, _now())

    def add_samples(self, session_id: int, samples: Iterable) -> list[int]:
        """Record the answers of every module to one request, as one unit.

        Eight modules replying to the same PID are a single observation of the
        bus, so the batch shares one timestamp — that is what lets a reader
        group the rows back into the cycle they came from — and one
        transaction, so a crash leaves the whole cycle or none of it rather
        than a partial picture of the vehicle.
        """
        batch = list(samples)
        if not batch:
            return []
        ts = _now()
        with _transaction(self.conn):
            ids = [self._insert_sample(session_id, sample, ts) for sample in batch]
        return ids

    def _insert_sample(self, session_id: int, sample, ts: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO samples(session_id, ts, pid, name, value, unit, status, raw_hex, ecu)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                ts,
                sample.pid,
                sample.name,
                sample.value,
                sample.unit,
                sample.status,
                sample.raw_hex,
                # Read defensively: a decoded value from before per-ECU
                # attribution has no such attribute, and dropping a real
                # reading over a missing label would be the wrong trade.
                _text(getattr(sample, "ecu", "")),
            ),
        )
        return int(cur.lastrowid)

    def add_monitor_test(self, session_id: int, test, raw_hex: str = "") -> int:
        """Record one service 06 on-board monitoring result."""
        return self._insert_monitor_test(session_id, test, raw_hex, _now())

    def add_monitor_tests(self, session_id: int, tests: Iterable, raw_hex: str = "") -> list[int]:
        """Record a module's monitor results together, under one timestamp.

        A service 06 reply carries many tests in one message; splitting them
        across timestamps and transactions would invent an ordering the vehicle
        never reported.
        """
        batch = list(tests)
        if not batch:
            return []
        ts = _now()
        with _transaction(self.conn):
            ids = [self._insert_monitor_test(session_id, test, raw_hex, ts) for test in batch]
        return ids

    def _insert_monitor_test(self, session_id: int, test, raw_hex: str, ts: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO monitor_tests(session_id, ts, ecu, mid, tid, uasid, value, min_limit,"
            " max_limit, unit, scaled_value, scaled_min, scaled_max, raw_hex)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                ts,
                _text(getattr(test, "ecu", "")),
                _as_int(getattr(test, "mid", None)),
                _as_int(getattr(test, "tid", None)),
                _as_int(getattr(test, "uasid", None)),
                _as_int(getattr(test, "value", None)),
                _as_int(getattr(test, "min_limit", None)),
                _as_int(getattr(test, "max_limit", None)),
                _text(getattr(test, "unit", "")),
                _as_float(getattr(test, "scaled_value", None)),
                _as_float(getattr(test, "scaled_min", None)),
                _as_float(getattr(test, "scaled_max", None)),
                # The caller passes the frame these tests were decoded from,
                # because one reply yields many rows; a decoded object that
                # carries its own bytes is trusted only when nothing was given.
                _text(raw_hex or getattr(test, "raw_hex", "")),
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

    def latest_monitor_tests(self, limit: int = 10) -> list[sqlite3.Row]:
        """Most recent service 06 rows, newest first.

        ``monitor_tests`` carries an ``uploaded_at`` marker for symmetry with
        ``samples``, but nothing stamps it: the uploader reads ``samples`` and
        nothing else, and widening what may leave the node is a decision to
        take deliberately, not a side effect of adding a table.
        """
        with closing(
            self.conn.execute("SELECT * FROM monitor_tests ORDER BY id DESC LIMIT ?", (limit,))
        ) as cur:
            return cur.fetchall()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
