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
from typing import Any, Final, Iterable, Iterator, Optional

__all__ = ["Storage", "SCHEMA_VERSION", "migrate"]

#: Versions this build knows how to upgrade *from*.  Anything else raises:
#: opening a layout you have guessed at is how evidence gets destroyed.
_UPGRADABLE_FROM: Final[frozenset[int]] = frozenset({1, 2})

SCHEMA_VERSION = 3

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
    cycle_id    INTEGER REFERENCES cycles(id),
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
    cycle_id    INTEGER REFERENCES cycles(id),
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
        cycle_id     INTEGER REFERENCES cycles(id),
        uploaded_at  TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_monitor_tests_session_ts ON monitor_tests(session_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_monitor_tests_pending ON monitor_tests(uploaded_at)"
    " WHERE uploaded_at IS NULL",
)


#: Schema version 3.  Held as statements rather than one script for the same
#: reason as the monitor tests above: ``executescript`` commits whatever is
#: pending before it runs, so :func:`migrate` cannot use it inside a
#: transaction.  One definition serves both a fresh database and an upgraded
#: one, which is what keeps the two shapes from drifting apart.
#:
#: A *cycle* is one pass over the configured requests.  It is a table and not
#: merely a column because a pass that produced **no rows at all** still has to
#: be recordable -- and a sleeping vehicle produces exactly that.  A column
#: alone could not represent it, and an unrecorded gap is the failure this
#: project has already been bitten by once.
_CYCLES_DDL: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS cycles (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  INTEGER NOT NULL REFERENCES sessions(id),
        seq         INTEGER NOT NULL,
        kind        TEXT    NOT NULL DEFAULT 'poll',
        started_at  TEXT    NOT NULL,
        ended_at    TEXT,
        completed   INTEGER NOT NULL DEFAULT 0,
        had_data    INTEGER NOT NULL DEFAULT 0,
        policy      TEXT    NOT NULL DEFAULT '',
        interval_s  REAL,
        detail      TEXT    NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cycles_session_seq ON cycles(session_id, seq)",
)

#: Which module is at which address.  ``address`` holds exactly what
#: :func:`decode.ecu_from_header` returns -- two hex digits for a 29-bit source
#: ("45"), the whole three-character identifier for an 11-bit one ("7E8") -- so
#: it joins to ``samples.ecu`` with no translation.  That asymmetry is
#: deliberate and documented there; do not "fix" it here.
#:
#: ``name_source`` is what keeps a backfilled row distinguishable from a
#: measured one.  A name copied out of ``vehicle_info`` is an earlier
#: inference; a name read back behind an ``ATCRA`` receive filter is a
#: measurement.  Collapsing the two would launder the first into the second.
_ECU_MODULES_DDL: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS ecu_modules (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        address        TEXT NOT NULL UNIQUE,
        name           TEXT NOT NULL DEFAULT '',
        first_seen_at  TEXT NOT NULL,
        last_seen_at   TEXT NOT NULL,
        source_session INTEGER REFERENCES sessions(id),
        name_source    TEXT NOT NULL DEFAULT '',
        raw_hex        TEXT NOT NULL DEFAULT ''
    )
    """,
)

#: Per-module DTC answers, as a child of ``dtc_reads`` rather than a widening
#: of it.  A ``dtc_reads`` row has always meant "one request, one aggregate
#: answer"; adding an ``ecu`` column would silently change that and break every
#: existing count.  ``read_id`` makes the join available in both directions.
_DTC_ECU_DDL: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS dtc_ecu_reads (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  INTEGER NOT NULL REFERENCES sessions(id),
        cycle_id    INTEGER REFERENCES cycles(id),
        read_id     INTEGER REFERENCES dtc_reads(id),
        ts          TEXT    NOT NULL,
        mode        TEXT    NOT NULL,
        ecu         TEXT    NOT NULL DEFAULT '',
        codes       TEXT    NOT NULL DEFAULT '',
        code_count  INTEGER NOT NULL DEFAULT 0,
        status      TEXT    NOT NULL DEFAULT '',
        detail      TEXT    NOT NULL DEFAULT '',
        raw_hex     TEXT    NOT NULL DEFAULT '',
        uploaded_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dtc_ecu_reads_session_ts"
    " ON dtc_ecu_reads(session_id, ts)",
)

#: Service 01 PID 01 as structure rather than a number.  ``samples`` still gets
#: its ``undecoded`` row for this PID; this table is added *alongside*, never
#: instead, so the existing rows keep meaning what they meant.
_MONITOR_STATUS_DDL: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS monitor_status (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id    INTEGER NOT NULL REFERENCES sessions(id),
        cycle_id      INTEGER REFERENCES cycles(id),
        ts            TEXT    NOT NULL,
        ecu           TEXT    NOT NULL DEFAULT '',
        mil_on        INTEGER,
        dtc_count     INTEGER,
        ignition_type TEXT    NOT NULL DEFAULT '',
        status        TEXT    NOT NULL DEFAULT '',
        raw_hex       TEXT    NOT NULL DEFAULT '',
        uploaded_at   TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_monitor_status_session_ts"
    " ON monitor_status(session_id, ts)",
    # A monitor name is a row, not a column.  Byte B bit 3 chooses the spark or
    # compression name table at decode time, so column headers would bake that
    # choice into the schema.  As rows, a bit this build cannot name is stored
    # with its source byte and bit intact and no invented meaning -- recoverable
    # later, exactly like an unknown unit-and-scaling identifier.
    """
    CREATE TABLE IF NOT EXISTS monitor_readiness (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        status_id  INTEGER NOT NULL REFERENCES monitor_status(id),
        session_id INTEGER NOT NULL REFERENCES sessions(id),
        ts         TEXT    NOT NULL,
        monitor    TEXT    NOT NULL,
        kind       TEXT    NOT NULL,
        supported  INTEGER NOT NULL,
        complete   INTEGER,
        src_byte   TEXT    NOT NULL,
        src_bit    INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_monitor_readiness_status"
    " ON monitor_readiness(status_id)",
)

#: Per-module service 09.  ``seq`` is load-bearing: one live reply carried 42
#: calibration verification numbers from six modules, so a module's values are
#: an ordered list, not a single field.
_ECU_INFO_DDL: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS ecu_info (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  INTEGER NOT NULL REFERENCES sessions(id),
        cycle_id    INTEGER REFERENCES cycles(id),
        ts          TEXT    NOT NULL,
        ecu         TEXT    NOT NULL DEFAULT '',
        item        TEXT    NOT NULL,
        item_name   TEXT    NOT NULL DEFAULT '',
        seq         INTEGER NOT NULL DEFAULT 0,
        value       TEXT    NOT NULL DEFAULT '',
        status      TEXT    NOT NULL DEFAULT '',
        raw_hex     TEXT    NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ecu_info_session_ts ON ecu_info(session_id, ts)",
)

#: Tables carrying a cycle reference, and the column added to each.  Nullable
#: on purpose: ``NULL`` means "recorded before cycles existed", which is the
#: truth about the rows already on the node.  A ``0`` would be a fabricated
#: group id that reads exactly like a real one.
_CYCLE_REFERENCED_TABLES: Final[tuple[str, ...]] = ("samples", "monitor_tests", "dtc_reads")

_V3_DDL: Final[tuple[str, ...]] = (
    _ECU_MODULES_DDL + _DTC_ECU_DDL + _MONITOR_STATUS_DDL + _ECU_INFO_DDL
    + ("CREATE INDEX IF NOT EXISTS idx_samples_cycle ON samples(cycle_id)"
       " WHERE cycle_id IS NOT NULL",)
)


def _as_script(statements: tuple[str, ...]) -> str:
    return "".join(f"{statement};\n" for statement in statements)


# ``cycles`` is created before the tables that reference it.  Foreign keys are
# not enforced here (nothing sets ``PRAGMA foreign_keys=ON``), but relying on
# that to get the order wrong would be a trap for whoever turns them on.
_SCHEMA = (
    _as_script(_CYCLES_DDL)
    + _BASE_SCHEMA
    + _as_script(_MONITOR_TESTS_DDL)
    + _as_script(_V3_DDL)
)


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



def _backfill_ecu_modules(conn: sqlite3.Connection) -> int:
    """Seed ``ecu_modules`` from the ``vehicle_info`` ``ecu:<addr>`` rows.

    Those rows were the workaround before this table existed.  **Nothing in
    ``vehicle_info`` is read destructively, moved or deleted**: it stays the
    append-only log of what was recorded when, and ``ecu_modules`` becomes the
    current-state index beside it.

    Two real malformed rows exist on the reference node — ``ecu:addresses`` and
    ``ecu:names``, which hold the Python ``repr`` of a list and a dict from a
    transient bug, and are disclosed in the validation record rather than
    deleted.  The address filter below exists to exclude exactly those, not a
    hypothetical: an address is two or three characters and hexadecimal.

    ``ON CONFLICT DO NOTHING`` makes a re-run a no-op and means a backfill can
    never overwrite an observation recorded after the upgrade.
    """
    cur = conn.execute(
        """
        INSERT INTO ecu_modules(address, name, first_seen_at, last_seen_at,
                                source_session, name_source, raw_hex)
        SELECT
            substr(v.item, 5),
            COALESCE((SELECT v2.value_masked FROM vehicle_info v2
                      WHERE v2.item = v.item AND v2.value_masked <> ''
                      ORDER BY v2.id DESC LIMIT 1), ''),
            MIN(v.ts), MAX(v.ts), MIN(v.session_id), 'backfill:vehicle_info', ''
        FROM vehicle_info v
        WHERE v.item LIKE 'ecu:%'
          AND length(substr(v.item, 5)) IN (2, 3)
          AND upper(substr(v.item, 5)) NOT GLOB '*[^0-9A-F]*'
        GROUP BY substr(v.item, 5)
        ON CONFLICT(address) DO NOTHING
        """
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def migrate(conn: sqlite3.Connection, *, path: str | Path = "") -> int:
    """Bring an existing database up to :data:`SCHEMA_VERSION` and return it.

    The reference node has been recording a real vehicle, so this upgrades in
    place: it adds the missing column, creates the missing table, and only then
    records the new version.  Every step is guarded by its own "is it already
    there?" test, which makes the whole function safe to run twice, safe to run
    against a database that is already current, and safe to interrupt — a crash
    before the final bump leaves a version-1 database that the next open
    upgrades again.

    Versions 1 and 2 (and a database whose version row went missing, which the
    same additive steps repair) are understood.  Anything else — a 0, or a
    version 4 written by a newer build — raises, because the alternative is a
    program that writes into a layout it does not know.

    Every step is guarded by its own "is it already there?" test rather than
    keyed on the starting version, so one code path upgrades a v1 and a v2
    database alike and neither can be half-applied.
    """
    version = _current_version(conn)
    if version == SCHEMA_VERSION:
        return version
    if version is not None and version not in _UPGRADABLE_FROM:
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

        # --- version 3 -------------------------------------------------
        for statement in _CYCLES_DDL:
            conn.execute(statement)
        for table in _CYCLE_REFERENCED_TABLES:
            if "cycle_id" not in _column_names(conn, table):
                # Nullable, and that is the point: NULL says "recorded before
                # cycles existed", which is true of every row already on the
                # node.  A zero would be a group id that reads like a real one.
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN cycle_id INTEGER REFERENCES cycles(id)")
        for statement in _V3_DDL:
            conn.execute(statement)
        _backfill_ecu_modules(conn)

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
    def add_sample(self, session_id: int, sample, *, cycle_id: Optional[int] = None) -> int:
        """Record one decoded reading, attributed to the module that sent it."""
        return self._insert_sample(session_id, sample, _now(), cycle_id)

    def add_samples(self, session_id: int, samples: Iterable,
                    *, cycle_id: Optional[int] = None) -> list[int]:
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
            ids = [self._insert_sample(session_id, sample, ts, cycle_id) for sample in batch]
        return ids

    def _insert_sample(self, session_id: int, sample, ts: str,
                       cycle_id: Optional[int] = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO samples(session_id, ts, pid, name, value, unit, status, raw_hex, ecu,"
            " cycle_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
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
                cycle_id,
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


    # -- cycles ----------------------------------------------------------
    def begin_cycle(self, session_id: int, *, seq: int, kind: str = "poll",
                    policy: str = "") -> int:
        """Open a cycle row *before* the pass runs.

        Written up front on purpose: a crash or a power cut then leaves
        ``ended_at IS NULL`` and ``completed = 0``, which is a visible partial
        cycle.  Writing it afterwards would make an interrupted pass invisible,
        and an invisible gap is the failure this project has already been
        bitten by once.
        """
        cur = self.conn.execute(
            "INSERT INTO cycles(session_id, seq, kind, started_at, policy)"
            " VALUES (?,?,?,?,?)",
            (session_id, int(seq), _text(kind), _now(), _text(policy)),
        )
        return int(cur.lastrowid)

    def end_cycle(self, cycle_id: int, *, completed: bool, had_data: bool,
                  interval_s: Optional[float] = None, detail: str = "") -> None:
        """Close a cycle, recording whether it finished and whether it saw data."""
        self.conn.execute(
            "UPDATE cycles SET ended_at=?, completed=?, had_data=?, interval_s=?, detail=?"
            " WHERE id=?",
            (_now(), int(bool(completed)), int(bool(had_data)),
             None if interval_s is None else float(interval_s), _text(detail), cycle_id),
        )

    def latest_cycles(self, limit: int = 10) -> list[sqlite3.Row]:
        with closing(self.conn.execute(
                "SELECT * FROM cycles ORDER BY id DESC LIMIT ?", (limit,))) as cur:
            return cur.fetchall()

    # -- module identity -------------------------------------------------
    def note_ecu(self, address: str, *, name: str = "", session_id: Optional[int] = None,
                 raw_hex: str = "", name_source: str = "") -> int:
        """Record that *address* answered, and its name when one is known.

        Two rules carry the weight here.

        An empty *name* never overwrites a known one.  ``ecu_name_map`` returns
        an empty string when its receive filter did not take, because it will
        not guess — and that refusal must not erase a name proven on an earlier
        run.

        A *different* non-empty name for a known address updates the row and
        records an ``ecu_name_changed`` event.  Modules do get replaced, and
        silently overwriting would lose the fact that it happened.
        """
        address = _text(address).upper()
        if not address:
            raise ValueError("note_ecu needs a module address")
        now = _now()
        with closing(self.conn.execute(
                "SELECT id, name FROM ecu_modules WHERE address=?", (address,))) as cur:
            row = cur.fetchone()
        if row is not None and name and row["name"] and name != row["name"]:
            self.add_event(
                "ecu_name_changed",
                f"{address}: {row['name']!r} -> {name!r}", session_id=session_id)
        self.conn.execute(
            "INSERT INTO ecu_modules(address, name, first_seen_at, last_seen_at,"
            " source_session, name_source, raw_hex) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(address) DO UPDATE SET"
            "   last_seen_at = excluded.last_seen_at,"
            "   name = CASE WHEN excluded.name <> '' THEN excluded.name"
            "               ELSE ecu_modules.name END,"
            "   name_source = CASE WHEN excluded.name <> '' THEN excluded.name_source"
            "                      ELSE ecu_modules.name_source END",
            (address, _text(name), now, now, session_id, _text(name_source), _text(raw_hex)),
        )
        with closing(self.conn.execute(
                "SELECT id FROM ecu_modules WHERE address=?", (address,))) as cur:
            return int(cur.fetchone()["id"])

    def ecu_modules(self) -> list[sqlite3.Row]:
        with closing(self.conn.execute(
                "SELECT * FROM ecu_modules ORDER BY address")) as cur:
            return cur.fetchall()

    # -- per-module diagnostic answers -----------------------------------
    def add_dtc_ecu_reads(self, session_id: int, results: Iterable, *,
                          read_id: Optional[int] = None,
                          cycle_id: Optional[int] = None) -> list[int]:
        """Record each module's answer to one DTC request.

        One request, one instant: the batch shares a timestamp and a
        transaction, for the same reason :meth:`add_samples` does.
        """
        batch = list(results)
        if not batch:
            return []
        ts = _now()
        ids: list[int] = []
        with _transaction(self.conn):
            for item in batch:
                codes = list(getattr(item, "codes", []) or [])
                cur = self.conn.execute(
                    "INSERT INTO dtc_ecu_reads(session_id, cycle_id, read_id, ts, mode, ecu,"
                    " codes, code_count, status, detail, raw_hex)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (session_id, cycle_id, read_id, ts, _text(item.mode),
                     _text(getattr(item, "ecu", "")), ",".join(codes), len(codes),
                     _text(getattr(item, "status", "")), _text(getattr(item, "detail", "")),
                     _text(getattr(item, "raw_hex", ""))),
                )
                ids.append(int(cur.lastrowid))
        return ids

    def add_monitor_status(self, session_id: int, statuses: Iterable, *,
                           cycle_id: Optional[int] = None) -> list[int]:
        """Record service 01 PID 01 as structure, with its readiness bits.

        ``complete`` is stored as NULL when a monitor is not supported.  "The
        vehicle does not run this monitor" and "it runs it and is not ready"
        are different facts, and storing 0 for both would be the same class of
        error as a guessed scaling.
        """
        batch = list(statuses)
        if not batch:
            return []
        ts = _now()
        ids: list[int] = []
        with _transaction(self.conn):
            for item in batch:
                cur = self.conn.execute(
                    "INSERT INTO monitor_status(session_id, cycle_id, ts, ecu, mil_on,"
                    " dtc_count, ignition_type, status, raw_hex) VALUES (?,?,?,?,?,?,?,?,?)",
                    (session_id, cycle_id, ts, _text(getattr(item, "ecu", "")),
                     None if item.mil_on is None else int(bool(item.mil_on)),
                     item.dtc_count, _text(getattr(item, "ignition_type", "")),
                     _text(getattr(item, "status", "")),
                     _text(getattr(item, "raw_hex", ""))),
                )
                status_id = int(cur.lastrowid)
                ids.append(status_id)
                for bit in getattr(item, "readiness", []) or []:
                    self.conn.execute(
                        "INSERT INTO monitor_readiness(status_id, session_id, ts, monitor,"
                        " kind, supported, complete, src_byte, src_bit)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (status_id, session_id, ts, _text(bit.monitor), _text(bit.kind),
                         int(bool(bit.supported)),
                         None if bit.complete is None else int(bool(bit.complete)),
                         _text(bit.src_byte), int(bit.src_bit)),
                    )
        return ids

    def add_ecu_info(self, session_id: int, ecu: str, item: str, values: Iterable[str],
                     *, item_name: str = "", status: str = "", raw_hex: str = "",
                     cycle_id: Optional[int] = None) -> list[int]:
        """Record one module's service 09 answers, in the order it gave them.

        Service 09 item ``02`` is refused outright.  That is the VIN, and this
        table is exported; making it structurally impossible to put an unmasked
        VIN here is worth more than a rule someone has to remember, in the same
        way ``uploader.UPLOADABLE_TABLES`` is a statement of intent in code.
        """
        item = _text(item).upper()
        if item in ("02", "2"):
            raise ValueError(
                "refusing service 09 item 02: that is the VIN, and this table is "
                "exported. Store it masked in vehicle_info instead")
        batch = list(values)
        if not batch:
            return []
        ts = _now()
        ids: list[int] = []
        with _transaction(self.conn):
            for seq, value in enumerate(batch):
                cur = self.conn.execute(
                    "INSERT INTO ecu_info(session_id, cycle_id, ts, ecu, item, item_name,"
                    " seq, value, status, raw_hex) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (session_id, cycle_id, ts, _text(ecu), item, _text(item_name),
                     seq, _text(value), _text(status), _text(raw_hex)),
                )
                ids.append(int(cur.lastrowid))
        return ids

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
