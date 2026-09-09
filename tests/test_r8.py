"""Reading the radar detector's published state.

This is another project's file, written by another process on its own
schedule. Not suspect -- same operator, same machine -- but a reader that
assumes well-formed input is one truncated write away from taking the
dashboard down with it, and the dashboard's job is the vehicle.
"""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hummer_obd import r8

NOW = datetime(2026, 9, 9, 3, 30, tzinfo=timezone.utc)


def _state(tmp, doc):
    path = Path(tmp) / "state.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _good(**over):
    doc = {
        "schema": 1,
        "updated_at": "2026-09-09T03:29:50Z",
        "link": {"connected": True, "compatible": True},
        "collector": {"mode": "continuous"},
        "counters": {"alert_packets": 583},
        "telemetry": {"voltage": 13.6, "gps_locked": True},
        "alerts": [],
    }
    doc.update(over)
    return doc


class TestTheStateFileIsTreatedAsUntrusted(unittest.TestCase):

    def test_a_missing_file_is_a_reason_not_an_exception(self):
        got = r8.read_state(Path("/nonexistent/state.json"))
        self.assertFalse(got["available"])
        self.assertIn("no state file", got["reason"])

    def test_rubbish_json_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertFalse(r8.read_state(path)["available"])

    def test_an_implausibly_large_file_is_refused_unparsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("[" + "0," * r8.MAX_BYTES + "0]", encoding="utf-8")
            got = r8.read_state(path)
            self.assertFalse(got["available"])
            self.assertIn("large", got["reason"])

    def test_the_schema_is_pinned_exactly_never_a_minimum(self):
        # The sibling's tests pin its key sets as literals because this
        # project consumes them. A >= would accept a schema 2 whose fields
        # moved and read them as though they had not.
        with tempfile.TemporaryDirectory() as tmp:
            for schema in (0, 2, 3, "1", None):
                with self.subTest(schema=schema):
                    got = r8.read_state(_state(tmp, _good(schema=schema)))
                    self.assertFalse(got["available"])

    def test_a_good_file_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            got = r8.read_state(_state(tmp, _good()), now=NOW)
            self.assertTrue(got["available"])
            self.assertEqual(got["voltage"], 13.6)
            self.assertTrue(got["connected"])
            self.assertEqual(got["alert_packets"], 583)
            self.assertAlmostEqual(got["age_s"], 10.0, places=0)
            self.assertFalse(got["stale"])


class TestStalenessIsComputedHere(unittest.TestCase):

    def test_age_comes_from_updated_at_not_the_files_own_stale_flag(self):
        # The file's `stale` is packet age at write time, so it stops updating
        # when the writer dies -- exactly when staleness matters most.
        with tempfile.TemporaryDirectory() as tmp:
            doc = _good(telemetry={"voltage": 13.6, "gps_locked": True,
                                   "stale": False, "age_s": 0.1})
            doc["updated_at"] = (NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
            got = r8.read_state(_state(tmp, doc), now=NOW)
            self.assertTrue(got["available"])
            self.assertTrue(got["stale"], "two hours old must not read as fresh")

    def test_a_future_timestamp_is_refused_rather_than_read_as_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = _good()
            doc["updated_at"] = (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
            self.assertFalse(r8.read_state(_state(tmp, doc), now=NOW)["available"])


class TestEnumeratedFieldsAreAllowlisted(unittest.TestCase):

    def test_an_unknown_band_becomes_unknown_not_free_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = _good(alerts=[{"band": "<script>", "strength": 4,
                                 "direction": "sideways"}])
            got = r8.read_state(_state(tmp, doc), now=NOW)
            self.assertEqual(got["alerts"][0]["band"], "unknown")
            self.assertEqual(got["alerts"][0]["direction"], "unknown")

    def test_a_strength_outside_one_to_eight_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in (0, 9, -1, 4.5, True, "4"):
                with self.subTest(strength=bad):
                    doc = _good(alerts=[{"band": "KA", "strength": bad,
                                         "direction": "front"}])
                    got = r8.read_state(_state(tmp, doc), now=NOW)
                    self.assertIsNone(got["alerts"][0]["strength"])

    def test_gps_locked_stays_tri_state(self):
        # null means the detector never said, which is not the same as "no
        # lock". Collapsing it would report a fault that was never observed.
        with tempfile.TemporaryDirectory() as tmp:
            for value, expect in ((True, True), (False, False),
                                  (None, None), ("yes", None)):
                with self.subTest(value=value):
                    doc = _good(telemetry={"gps_locked": value})
                    self.assertIs(
                        r8.read_state(_state(tmp, doc), now=NOW)["gps_locked"], expect)


class TestHistoryIsReadOnlyAndPositionIsGated(unittest.TestCase):

    def _db(self, tmp):
        path = Path(tmp) / "history.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE alert_events (id INTEGER PRIMARY KEY, kind TEXT, at TEXT,"
            " band TEXT, strength INT, frequency_ghz REAL, direction TEXT,"
            " max_strength INT, duration_s REAL, lat REAL, lon REAL)")
        conn.execute(
            "INSERT INTO alert_events VALUES (1,'alert_end','2026-09-09T01:00:12Z',"
            "'KA',1,35.592,'front',6,3.34,33.335,-111.785)")
        conn.commit()
        conn.close()
        return path

    def test_coordinates_are_withheld_by_default(self):
        # The detector's database really does record position. A dashboard
        # that leaked it through the radar panel while the telemetry panel
        # withheld it would be a hole in one wall of the same room.
        with tempfile.TemporaryDirectory() as tmp:
            got = r8.recent_alerts(self._db(tmp))
            self.assertEqual(len(got), 1)
            self.assertNotIn("lat", got[0])
            self.assertNotIn("lon", got[0])

    def test_coordinates_appear_only_when_asked_for(self):
        with tempfile.TemporaryDirectory() as tmp:
            got = r8.recent_alerts(self._db(tmp), location=True)
            self.assertAlmostEqual(got[0]["lat"], 33.335, places=3)

    def test_a_missing_database_is_an_empty_list_not_an_error(self):
        self.assertEqual(r8.recent_alerts(Path("/nonexistent/history.db")), [])

    def test_only_completed_alerts_are_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db(tmp)
            conn = sqlite3.connect(path)
            conn.execute("INSERT INTO alert_events VALUES (2,'alert_start',"
                         "'2026-09-09T02:00:00Z','K',3,24.1,'rear',NULL,NULL,NULL,NULL)")
            conn.commit()
            conn.close()
            got = r8.recent_alerts(path)
            self.assertEqual([a["band"] for a in got], ["KA"])

    def test_it_opens_read_only_so_it_cannot_migrate_the_siblings_database(self):
        import inspect
        self.assertIn("mode=ro", inspect.getsource(r8.recent_alerts))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
