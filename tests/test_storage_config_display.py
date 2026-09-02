"""Storage, configuration and display-rendering tests."""

import sqlite3
import tempfile
import textwrap
import unittest
from dataclasses import dataclass
from pathlib import Path

from hummer_obd.config import load_config
from hummer_obd.decode import PidValue
from hummer_obd.display.status import StatusData, render_status_image
from hummer_obd.storage import SCHEMA_VERSION, Storage, migrate

#: The version-1 schema, copied from the build that is running on the reference
#: node.  Building the fixture with the *current* schema would test nothing:
#: the gap between the two is the entire subject of a migration test.
V1_SCHEMA = """
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

#: The version-1 sample columns, named rather than starred: version 2 appends
#: ``ecu``, so ``SELECT *`` would compare two different shapes and call the
#: difference a lost row.
V1_SAMPLE_COLUMNS = "id, session_id, ts, pid, name, value, unit, status, raw_hex, uploaded_at"


@dataclass
class EcuSample:
    """A decoded reading that carries the module which answered.

    Storage reads a sample by attribute, not by type, so this stands in for the
    decoder's per-ECU value object and leaves the two free to ship separately.
    """

    pid: str
    name: str
    value: float | None
    unit: str
    raw_hex: str
    status: str
    ecu: str = ""


@dataclass
class MonitorResult:
    """One service 06 on-board monitoring result, as a decoder hands it over."""

    ecu: str = ""
    mid: int | str = 0
    tid: int | str = 0
    uasid: int | str = 0
    value: int = 0
    min_limit: int = 0
    max_limit: int = 0
    unit: str = ""
    scaled_value: float | None = None
    scaled_min: float | None = None
    scaled_max: float | None = None
    raw_hex: str = ""


def build_v1_database(path: Path) -> None:
    """Create a version-1 database holding rows that could not be taken again."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(V1_SCHEMA)
        conn.execute("INSERT INTO schema_version(version) VALUES (1)")
        conn.execute(
            "INSERT INTO sessions(session_uid, started_at, ended_at, adapter_id, protocol,"
            " raw_log_path, notes) VALUES (?,?,?,?,?,?,?)",
            ("collect-20260830T090000Z", "2026-08-30T09:00:00+00:00", "2026-08-30T11:00:00+00:00",
             "OBDLink MX+ r5.7", "ISO 15765-4 (CAN 29/500)", "logs/raw/collect.jsonl", "overnight"),
        )
        conn.executemany(
            "INSERT INTO samples(session_id, ts, pid, name, value, unit, status, raw_hex,"
            " uploaded_at) VALUES (1,?,?,?,?,?,?,?,?)",
            [
                ("2026-08-30T09:00:01+00:00", "0D", "vehicle speed", 0.0, "km/h", "ok",
                 "410d00", None),
                ("2026-08-30T09:00:02+00:00", "42", "control module voltage", 13.812, "V", "ok",
                 "41423600", "2026-08-30T10:00:00+00:00"),
                # A PID the vehicle never answered.  NULL value and NULL unit
                # have to come through the upgrade as NULLs, not as zeroes.
                ("2026-08-30T09:00:03+00:00", "5B", "hybrid/EV battery pack remaining life",
                 None, None, "no_data", "", None),
            ],
        )
        conn.execute(
            "INSERT INTO dtc_reads(session_id, ts, mode, codes, raw_hex, uploaded_at)"
            " VALUES (1,?,?,?,?,NULL)",
            ("2026-08-30T09:05:00+00:00", "07", "P0AA6", "4701 0AA6"),
        )
        conn.execute(
            "INSERT INTO vehicle_info(session_id, ts, item, value_masked, raw_hex) VALUES (1,?,?,?,?)",
            ("VIN", "1GT***21 (len=17)", "raw in log", "4902014731"),
        )
        conn.execute(
            "INSERT INTO events(ts, session_id, kind, detail) VALUES (?,1,?,?)",
            ("2026-08-30T09:00:00+00:00", "session_start", "collector"),
        )
        conn.commit()
    finally:
        conn.close()


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "db.sqlite3"
        self.store = Storage(self.path)
        self.sid = self.store.start_session("uid-1", adapter_id="OBDLink MX+")

    def tearDown(self):
        self.store.close()

    def test_sample_round_trip(self):
        self.store.add_sample(self.sid, PidValue("0C", "engine speed", 1726.0, "rpm", "410c1af8", "ok"))
        rows = self.store.latest_samples()
        self.assertEqual(rows[0]["pid"], "0C")
        self.assertEqual(rows[0]["raw_hex"], "410c1af8")
        self.assertIsNone(rows[0]["uploaded_at"])

    def test_local_buffer_semantics(self):
        for i in range(3):
            self.store.add_sample(self.sid, PidValue("0D", "vehicle speed", i, "km/h", "410d00", "ok"))
        self.assertEqual(self.store.pending_count(), 3)
        ids = [row["id"] for row in self.store.pending_samples(limit=2)]
        self.assertEqual(self.store.mark_uploaded(ids), 2)
        self.assertEqual(self.store.pending_count(), 1)

    def test_dtc_and_vehicle_info(self):
        self.store.add_dtc_read(self.sid, "03", ["P0143"], "430201 43")
        self.store.add_vehicle_info(self.sid, "VIN", "1G1***67 (len=17)", "raw in log")
        rows = self.store.conn.execute("SELECT * FROM vehicle_info").fetchall()
        self.assertEqual(rows[0]["value_masked"], "1G1***67 (len=17)")

    def test_reopen_is_idempotent(self):
        self.store.close()
        with Storage(self.path) as store:
            self.assertEqual(store.pending_count(), 0)

    def test_a_sample_without_an_ecu_attribute_is_stored_unattributed(self):
        # The empty string is the honest answer for a reading taken before the
        # collector knew which module replied; NULL would make every reader of
        # the column handle a second kind of "nothing".
        self.store.add_sample(self.sid, PidValue("0C", "engine speed", 1726.0, "rpm", "410c", "ok"))
        self.assertEqual(self.store.latest_samples()[0]["ecu"], "")

    def test_every_module_that_answered_is_kept_with_its_own_row(self):
        ids = self.store.add_samples(self.sid, [
            EcuSample("05", "engine coolant temperature", 51.0, "degC", "41054b", "ok", "18DAF145"),
            EcuSample("05", "engine coolant temperature", 38.0, "degC", "410542", "ok", "18DAF117"),
            EcuSample("05", "engine coolant temperature", None, "degC", "", "no_data", "18DAF11A"),
        ])
        self.assertEqual(len(ids), 3)
        rows = {row["ecu"]: row for row in self.store.latest_samples()}
        self.assertEqual(sorted(rows), ["18DAF117", "18DAF11A", "18DAF145"])
        self.assertEqual(rows["18DAF145"]["value"], 51.0)
        self.assertEqual(rows["18DAF117"]["value"], 38.0)
        # A module that did not answer is still a row: "the drive unit was
        # silent" is a reading about the bus, and dropping it would leave a gap
        # nothing else records.
        self.assertIsNone(rows["18DAF11A"]["value"])
        self.assertEqual(rows["18DAF11A"]["status"], "no_data")
        # One request is one observation, so the batch shares a timestamp and
        # can be grouped back into the cycle it came from.
        self.assertEqual(len({row["ts"] for row in rows.values()}), 1)

    def test_an_empty_batch_writes_nothing(self):
        self.assertEqual(self.store.add_samples(self.sid, []), [])
        self.assertEqual(self.store.add_monitor_tests(self.sid, []), [])
        self.assertEqual(self.store.pending_count(), 0)

    def test_monitor_tests_round_trip_with_their_module_and_limits(self):
        ids = self.store.add_monitor_tests(self.sid, [
            MonitorResult(ecu="18DAF145", mid=0x01, tid=0x81, uasid=0x0B, value=256,
                          min_limit=0, max_limit=512, unit="V",
                          scaled_value=0.256, scaled_min=0.0, scaled_max=0.512),
            MonitorResult(ecu="18DAF117", mid=0x21, tid=0x8F, uasid=0x0B, value=100,
                          min_limit=10, max_limit=200, unit="V",
                          scaled_value=0.1, scaled_min=0.01, scaled_max=0.2),
        ], raw_hex="46 01 81 0B 0100 0000 0200")
        self.assertEqual(len(ids), 2)
        rows = {row["ecu"]: row for row in self.store.latest_monitor_tests()}
        first = rows["18DAF145"]
        self.assertEqual((first["mid"], first["tid"], first["uasid"]), (1, 129, 11))
        self.assertEqual((first["value"], first["min_limit"], first["max_limit"]), (256, 0, 512))
        self.assertEqual(first["unit"], "V")
        self.assertAlmostEqual(first["scaled_value"], 0.256)
        self.assertAlmostEqual(first["scaled_max"], 0.512)
        self.assertEqual(first["raw_hex"], "46 01 81 0B 0100 0000 0200")
        self.assertIsNone(first["uploaded_at"])
        self.assertEqual(len({row["ts"] for row in rows.values()}), 1)

    def test_an_unknown_scaling_is_stored_as_null_not_as_a_guess(self):
        self.store.add_monitor_test(
            self.sid,
            MonitorResult(ecu="18DAF145", mid=0xA1, tid=0x0C, uasid=0xFE, value=4321,
                          min_limit=0, max_limit=65535),
            raw_hex="46 A1 0C FE 10E1 0000 FFFF",
        )
        row = self.store.latest_monitor_tests()[0]
        # The counts the module reported are kept exactly as they arrived...
        self.assertEqual((row["value"], row["max_limit"]), (4321, 65535))
        # ...and the columns that would state a physical quantity stay NULL,
        # because this build cannot name the scaling for UASID FE.  A guessed
        # conversion would be indistinguishable from a measurement.
        self.assertIsNone(row["scaled_value"])
        self.assertIsNone(row["scaled_min"])
        self.assertIsNone(row["scaled_max"])
        self.assertEqual(row["unit"], "")

    def test_hex_identifiers_and_a_carried_raw_frame_are_accepted(self):
        # A MID written "A1" and a MID held as 0xA1 are the same monitor; two
        # rows that disagree about that would be two monitors.
        self.store.add_monitor_test(
            self.sid,
            MonitorResult(ecu="18DAF145", mid="A1", tid="0C", uasid="0B",
                          raw_hex="46 A1 0C 0B 0000 0000 0000"),
        )
        row = self.store.latest_monitor_tests()[0]
        self.assertEqual((row["mid"], row["tid"], row["uasid"]), (0xA1, 0x0C, 0x0B))
        self.assertEqual(row["raw_hex"], "46 A1 0C 0B 0000 0000 0000")

    def test_an_explicit_none_ecu_becomes_the_empty_string(self):
        # A decoder that says "I do not know which module answered" hands over
        # None.  The column is NOT NULL, so something has to be written; the
        # empty string is the one value that reads as "unattributed".  Passing
        # it through ``str`` instead would store the literal text "None", an
        # ECU identifier that looks real enough to be grouped and reported on.
        self.store.add_sample(
            self.sid, EcuSample("0C", "engine speed", 1726.0, "rpm", "410c", "ok", None)
        )
        self.store.add_monitor_test(
            self.sid, MonitorResult(ecu=None, mid=1, tid=1, uasid=1, unit=None)
        )
        self.assertEqual(self.store.latest_samples()[0]["ecu"], "")
        row = self.store.latest_monitor_tests()[0]
        self.assertEqual(row["ecu"], "")
        self.assertEqual(row["unit"], "")

    def test_latest_monitor_tests_returns_the_newest_first(self):
        for mid in (1, 2, 3):
            self.store.add_monitor_test(self.sid, MonitorResult(mid=mid, tid=0x81, uasid=1))
        # "latest" has to mean latest.  Reading oldest-first would quietly show
        # a display or a report the first tests a node ever recorded and go on
        # calling them current for the rest of the deployment.
        self.assertEqual([row["mid"] for row in self.store.latest_monitor_tests()], [3, 2, 1])
        self.assertEqual([row["mid"] for row in self.store.latest_monitor_tests(limit=2)], [3, 2])

    def test_a_batch_that_fails_part_way_writes_no_rows_at_all(self):
        # One request answered by eight modules is a single observation of the
        # bus.  Half of it committed is not a smaller observation, it is a
        # false one: a reader grouping by timestamp would conclude the missing
        # modules stayed silent when in truth the write died.
        good = EcuSample("05", "coolant", 51.0, "degC", "41054b", "ok", "18DAF145")
        with self.assertRaises(AttributeError):
            self.store.add_samples(self.sid, [good, object()])
        self.assertEqual(self.store.pending_count(), 0)
        self.assertEqual(self.store.latest_samples(), [])

        with self.assertRaises(ValueError):
            self.store.add_monitor_tests(self.sid, [
                MonitorResult(ecu="18DAF145", mid=0x01, tid=0x81, uasid=0x0B),
                MonitorResult(ecu="18DAF117", mid="not-hex", tid=0x81, uasid=0x0B),
            ])
        self.assertEqual(self.store.latest_monitor_tests(), [])


class TestSchemaMigration(unittest.TestCase):
    """Version 2 has to reach a node that is already holding a vehicle's history."""

    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "db.sqlite3"

    def read(self, sql, path=None):
        conn = sqlite3.connect(str(path or self.path))
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def columns(self, table, path=None):
        return [row[1] for row in self.read(f"PRAGMA table_info({table})", path)]

    def tables(self, path=None):
        return [row[0] for row in self.read("SELECT name FROM sqlite_master WHERE type='table'", path)]

    def version(self, path=None):
        return self.read("SELECT version FROM schema_version", path)[0][0]

    def schema_objects(self, path=None):
        """Every table and index this module defines, with its DDL."""
        return dict(self.read(
            "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'", path))

    def snapshot(self, path=None):
        """Every row of every version-1 table, by value."""
        return {
            "samples": self.read(f"SELECT {V1_SAMPLE_COLUMNS} FROM samples ORDER BY id", path),
            "sessions": self.read("SELECT * FROM sessions ORDER BY id", path),
            "dtc_reads": self.read("SELECT * FROM dtc_reads ORDER BY id", path),
            "vehicle_info": self.read("SELECT * FROM vehicle_info ORDER BY id", path),
            "events": self.read("SELECT * FROM events ORDER BY id", path),
        }

    def test_a_fresh_database_is_created_at_the_current_version(self):
        with Storage(self.path):
            pass
        self.assertEqual(SCHEMA_VERSION, 2)
        self.assertEqual(self.version(), 2)
        self.assertIn("ecu", self.columns("samples"))
        self.assertIn("monitor_tests", self.tables())
        self.assertEqual(
            self.columns("monitor_tests"),
            ["id", "session_id", "ts", "ecu", "mid", "tid", "uasid", "value", "min_limit",
             "max_limit", "unit", "scaled_value", "scaled_min", "scaled_max", "raw_hex",
             "uploaded_at"],
        )
        # The node keeps months of rows on an SD card, so both read paths are
        # indexed: one to pull a session back out by time, and one for the
        # upload queue.  The queue index is *partial* on purpose -- indexing
        # only the unsent rows keeps it the size of the backlog instead of the
        # size of the history.
        indexes = self.schema_objects()
        self.assertIn("idx_monitor_tests_session_ts", indexes)
        self.assertIn("uploaded_at IS NULL", indexes["idx_monitor_tests_pending"])
        self.assertIn("uploaded_at IS NULL", indexes["idx_samples_pending"])

    def test_a_populated_version_1_database_survives_the_upgrade(self):
        build_v1_database(self.path)
        before = self.snapshot()
        self.assertNotIn("ecu", self.columns("samples"))
        self.assertNotIn("monitor_tests", self.tables())

        with Storage(self.path) as store:
            self.assertEqual(len(store.latest_samples()), 3)

        # The point of the test: not one recorded value moved.  These rows came
        # off a vehicle that will not repeat them.
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.version(), 2)
        self.assertIn("ecu", self.columns("samples"))
        self.assertIn("monitor_tests", self.tables())
        # The upgrade cannot invent attribution for readings taken before it.
        self.assertEqual(self.read("SELECT DISTINCT ecu FROM samples"), [("",)])
        # NULLs stayed NULL rather than becoming a plausible zero.
        self.assertEqual(self.read("SELECT value, unit FROM samples WHERE pid='5B'"), [(None, None)])

    def test_migrate_upgrades_a_version_1_database_on_its_own(self):
        # ``migrate`` is exported and documented as the upgrade path, so it has
        # to do the whole upgrade by itself.  Reaching it only through
        # ``Storage`` hides an omission: ``Storage`` also runs the full
        # CREATE TABLE IF NOT EXISTS script, which would supply a table the
        # migration forgot and leave an operator who calls ``migrate`` directly
        # with a database stamped version 2 that is missing part of version 2.
        build_v1_database(self.path)
        before = self.snapshot()
        conn = sqlite3.connect(str(self.path))
        try:
            self.assertEqual(migrate(conn), SCHEMA_VERSION)
        finally:
            conn.close()
        self.assertIn("ecu", self.columns("samples"))
        self.assertIn("monitor_tests", self.tables())
        self.assertEqual(self.version(), SCHEMA_VERSION)
        self.assertEqual(self.snapshot(), before)

    def test_an_upgraded_database_ends_up_shaped_like_a_fresh_one(self):
        # Fresh and migrated databases are built by two different code paths.
        # If they drift, a query written against one silently misbehaves on the
        # other, and the node in the vehicle is always the migrated one.
        fresh = Path(tempfile.mkdtemp()) / "fresh.sqlite3"
        with Storage(fresh):
            pass
        build_v1_database(self.path)
        with Storage(self.path):
            pass

        fresh_objects = self.schema_objects(fresh)
        upgraded_objects = self.schema_objects()
        self.assertEqual(sorted(upgraded_objects), sorted(fresh_objects))
        for name in sorted(fresh_objects):
            with self.subTest(object=name):
                if name == "samples":
                    # The one documented difference: ADD COLUMN appends, so the
                    # upgraded table carries ``ecu`` last.  The columns must
                    # still be the same columns -- only their order may differ,
                    # which is why nothing in this project selects by position.
                    self.assertEqual(
                        sorted(self.columns("samples")),
                        sorted(self.columns("samples", fresh)),
                    )
                    self.assertNotEqual(self.columns("samples"), self.columns("samples", fresh))
                    continue
                self.assertEqual(upgraded_objects[name], fresh_objects[name])
        for table in ("monitor_tests", "sessions", "dtc_reads", "vehicle_info", "events"):
            with self.subTest(table=table):
                self.assertEqual(self.columns(table), self.columns(table, fresh))

    def test_the_migration_can_be_run_again(self):
        build_v1_database(self.path)
        before = self.snapshot()
        with Storage(self.path):
            pass
        with Storage(self.path):
            pass
        conn = sqlite3.connect(str(self.path))
        try:
            # Directly, twice more: a re-run must be a no-op, not a second
            # ALTER TABLE that fails on the column it already added.
            self.assertEqual(migrate(conn), SCHEMA_VERSION)
            self.assertEqual(migrate(conn), SCHEMA_VERSION)
        finally:
            conn.close()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.columns("samples").count("ecu"), 1)
        self.assertEqual(self.version(), 2)

    def test_an_unknown_schema_version_is_refused_and_nothing_is_written(self):
        for unknown in (0, 3, 99):
            with self.subTest(version=unknown):
                path = Path(tempfile.mkdtemp()) / "db.sqlite3"
                build_v1_database(path)
                conn = sqlite3.connect(str(path))
                conn.execute("UPDATE schema_version SET version=?", (unknown,))
                conn.commit()
                conn.close()
                before = self.snapshot(path)

                with self.assertRaises(RuntimeError) as caught:
                    Storage(path)
                self.assertIn(str(unknown), str(caught.exception))

                # A layout this build cannot read is left exactly as found:
                # no new column, no new table, no version rewritten under it.
                self.assertEqual(self.snapshot(path), before)
                self.assertNotIn("ecu", self.columns("samples", path))
                self.assertNotIn("monitor_tests", self.tables(path))
                self.assertEqual(self.version(path), unknown)

    def test_the_upload_queue_still_works_after_the_upgrade(self):
        build_v1_database(self.path)
        with Storage(self.path) as store:
            self.assertEqual(store.pending_count(), 2)
            pending = store.pending_samples()
            self.assertEqual([row["pid"] for row in pending], ["0D", "5B"])
            self.assertEqual([row["ecu"] for row in pending], ["", ""])
            self.assertEqual(store.mark_uploaded([pending[0]["id"]]), 1)
            self.assertEqual(store.pending_count(), 1)
            # Stamping is not deleting: the whole history is still on the node.
            self.assertEqual(len(store.latest_samples()), 3)
            store.add_samples(store.start_session("uid-after-upgrade"), [
                EcuSample("0D", "vehicle speed", 3.0, "km/h", "410d03", "ok", "18DAF110"),
            ])
            self.assertEqual(store.pending_count(), 2)


class TestConfig(unittest.TestCase):
    def test_defaults_are_safe(self):
        cfg = load_config()
        self.assertFalse(cfg.upload.enabled)
        self.assertFalse(cfg.collector.enabled)
        self.assertEqual(cfg.adapter.device, "/dev/rfcomm0")

    def test_load_from_file(self):
        path = Path(tempfile.mkdtemp()) / "hummer.toml"
        path.write_text(textwrap.dedent("""
            [adapter]
            device = "/dev/rfcomm1"

            [collector]
            pids = ["010C", "010D"]
        """))
        cfg = load_config(path)
        self.assertEqual(cfg.adapter.device, "/dev/rfcomm1")
        self.assertEqual(cfg.collector.pids, ["010C", "010D"])

    def test_unknown_keys_are_rejected(self):
        path = Path(tempfile.mkdtemp()) / "hummer.toml"
        path.write_text("[adapter]\nnot_a_key = 1\n")
        with self.assertRaises(ValueError):
            load_config(path)

    def test_upload_enabled_without_endpoint_is_rejected(self):
        path = Path(tempfile.mkdtemp()) / "hummer.toml"
        path.write_text("[upload]\nenabled = true\n")
        with self.assertRaises(ValueError):
            load_config(path)


class TestDisplay(unittest.TestCase):
    def test_render_size_and_mode(self):
        status = StatusData(
            hostname="hummer", ssid="Hummer-Hotspot", signal="72%",
            lan_ip="192.0.2.15", tailscale_ip="100.64.0.15",
            uptime="3h12m", temperature="44C", obd_state="rfcomm0 bound",
            updated="20:41Z",
        )
        image = render_status_image(status)
        self.assertEqual(image.size, (250, 122))
        self.assertEqual(image.mode, "1")
        # The panel is monochrome: the render must actually contain ink.
        self.assertGreater(image.histogram()[0], 0)

    def test_lines_cover_every_required_field(self):
        status = StatusData(hostname="hummer", ssid="net", lan_ip="192.0.2.15",
                            tailscale_ip="100.64.0.20", uptime="1h", temperature="40C",
                            obd_state="not bound")
        text = " ".join(status.as_lines())
        for expected in ("hummer", "net", "192.0.2.15", "100.64.0.20", "1h", "40C", "not bound"):
            self.assertIn(expected, text)

    def test_missing_values_do_not_crash(self):
        image = render_status_image(StatusData())
        self.assertEqual(image.size, (250, 122))


if __name__ == "__main__":
    unittest.main()
