"""A browser may read telemetry, never a device, arbitrary file, or location."""

import csv
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from hummer_obd.dashboard import MAX_HISTORY, SessionStore, energy_budget, make_server


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.store = SessionStore(self.directory)
        self.name = "drive-20260101T120000Z.csv"
        self.now = datetime.fromisoformat("2026-01-01T12:00:10+00:00").timestamp()

    def write(self, rows, name=None):
        path = self.directory / (name or self.name)
        fields = list(dict.fromkeys(key for row in rows for key in row))
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fields)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def rows(self):
        return [
            {"utc": "2026-01-01T12:00:00Z", "elapsed_s": 0, "pack_v": 390,
             "pack_a": 20, "speed_kph": 0, "soc_pct": 80, "energy_kwh": 152},
            {"utc": "2026-01-01T12:00:10Z", "elapsed_s": 10, "pack_v": 389,
             "pack_a": 40, "speed_kph": 50, "soc_pct": 79, "energy_kwh": 150.1},
        ]

    def test_empty_waiting_state(self):
        result = self.store.snapshot(now=self.now)
        self.assertEqual(result["session"]["status"], "empty")
        self.assertEqual(result["signals"], {})
        self.assertEqual(self.store.sessions(), {"sessions": [], "latest": None})

    def test_live_snapshot_has_provenance_and_coherent_power(self):
        self.write(self.rows())
        with patch("serial.Serial", side_effect=AssertionError("must not open serial")):
            result = self.store.snapshot(now=self.now)
        self.assertEqual(result["session"]["status"], "live")
        self.assertEqual(result["signals"]["pack_v"]["identifier"], "0x2885")
        self.assertEqual(result["signals"]["pack_v"]["status"], "fresh")
        self.assertAlmostEqual(result["derived"]["pack_kw"], 15.56)
        self.assertEqual(result["history"][-1]["pack_kw"], 15.56)

    def test_stopped_file_ages_even_when_cached_and_hides_current_tiles(self):
        self.write(self.rows())
        first = self.store.snapshot(now=self.now)
        later = self.store.snapshot(now=self.now + 300)
        self.assertEqual(first["signals"]["pack_v"]["age_s"], 0)
        self.assertEqual(later["session"]["status"], "stale")
        self.assertEqual(later["signals"]["pack_v"]["age_s"], 300)
        self.assertIsNone(later["derived"]["pack_kw"])
        self.assertEqual(later["signals"]["pack_v"]["value"], 389)

    def test_historical_selection_cannot_be_labelled_live(self):
        self.write(self.rows())
        result = self.store.snapshot(self.name, now=self.now + 3600)
        self.assertEqual(result["session"]["status"], "historical")
        self.assertAlmostEqual(result["derived"]["pack_kw"], 15.56)
        self.assertEqual(result["session"]["age_s"], 3600)
        self.assertEqual(result["signals"]["pack_v"]["age_s"], 0)
        self.assertEqual(result["signals"]["pack_v"]["status"], "fresh")

    def test_signal_age_includes_time_since_recorder_last_row(self):
        rows = self.rows()
        rows[-1]["pack_a"] = ""
        self.write(rows)
        result = self.store.snapshot(now=self.now + 40)
        self.assertEqual(result["session"]["status"], "live")
        self.assertEqual(result["signals"]["pack_a"]["age_s"], 50)
        self.assertEqual(result["signals"]["pack_a"]["status"], "stale")
        self.assertIsNone(result["derived"]["pack_kw"])

    def test_alternating_fresh_sensors_cannot_rejuvenate_an_old_pair(self):
        rows = self.rows()[:1] + [
            dict(utc="2026-01-01T12:01:30Z", elapsed_s=90, pack_v=395),
            dict(utc="2026-01-01T12:01:35Z", elapsed_s=95, pack_a=100),
            dict(utc="2026-01-01T12:01:40Z", elapsed_s=100, pack_v=394),
        ]
        self.write(rows)
        result = self.store.snapshot(now=self.now + 90)
        self.assertEqual(result["signals"]["pack_v"]["status"], "fresh")
        self.assertEqual(result["signals"]["pack_a"]["status"], "fresh")
        self.assertIsNone(result["derived"]["pack_kw"])

    def test_no_location_identity_unknown_fields_or_absolute_path_in_api(self):
        rows = self.rows()
        rows[-1].update(gps_lat=12.345678, gps_lon=23.456789,
                        gps_time="2026-01-01T12:00:10Z", gps_sats=9,
                        private_note="PRIVATE_SENTINEL", vin="PRIVATE_IDENTITY")
        self.write(rows)
        result = self.store.snapshot(now=self.now)
        serialized = json.dumps(result, allow_nan=False)
        for private in ("gps_lat", "gps_lon", "gps_time", "12.345678", "23.456789",
                        "PRIVATE_SENTINEL", "PRIVATE_IDENTITY", str(self.directory)):
            self.assertNotIn(private, serialized)
        self.assertEqual(result["signals"]["gps_sats"]["value"], 9)

    def test_invalid_reading_and_future_clock_do_not_look_live(self):
        rows = self.rows()
        rows[-1]["pack_v"] = 1.0
        self.write(rows)
        result = self.store.snapshot(now=self.now)
        self.assertEqual(result["signals"]["pack_v"]["status"], "invalid")
        self.assertIsNone(result["derived"]["pack_v"])
        self.assertIsNone(result["history"][-1]["pack_kw"])
        future = self.store.snapshot(now=self.now - 100)
        self.assertEqual(future["session"]["status"], "stale")
        self.assertTrue(any("clock" in w for w in future["warnings"]))

    def test_unproven_raw_fields_are_not_labelled_decoded(self):
        rows = self.rows()
        rows[-1]["cell_extra_raw"] = "01020304"
        self.write(rows)
        item = self.store.snapshot(now=self.now)["signals"]["cell_extra_raw"]
        self.assertEqual(item["value"], "01020304")
        self.assertIsNone(item["confidence"])
        self.assertEqual(item["confidence_label"], "raw / unscaled")

    def test_sleep_transition_cannot_look_like_perfectly_balanced_cells(self):
        rows = self.rows()
        rows[-1].update(cell_spread_mv=0, cell_avg_v=0, cell_min_v=0, cell_max_v=0)
        self.write(rows)
        result = self.store.snapshot(now=self.now)
        self.assertIsNone(result["derived"]["cell_spread_mv"])
        self.assertIsNone(result["history"][-1]["cell_spread_mv"])
        self.assertEqual(result["signals"]["cell_min_v"]["status"], "invalid")

    def test_latest_uses_file_activity_after_a_clock_correction(self):
        older_name = "drive-20250101T120000Z.csv"
        current = self.write(self.rows(), older_name)
        future_named = self.write(self.rows())
        os.utime(future_named, (100, 100))
        os.utime(current, (200, 200))
        self.assertEqual(self.store.sessions()["latest"], older_name)
        self.assertEqual(self.store.snapshot(now=self.now)["session"]["id"], older_name)

    def test_traversal_symlinks_and_unknown_session_fail_closed(self):
        for name in ("../secret.csv", "/etc/passwd", "drive-other.csv"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.snapshot(name)
        with tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / "secret"
            secret.write_text("private")
            (self.directory / self.name).symlink_to(secret)
            self.assertEqual(self.store.sessions()["sessions"], [])
            with self.assertRaises(FileNotFoundError):
                self.store.snapshot(self.name)

    def test_cache_refreshes_when_recorder_adds_a_row(self):
        self.write(self.rows()[:1])
        self.assertEqual(self.store.snapshot(now=self.now)["session"]["rows"], 1)
        self.write(self.rows())
        self.assertEqual(self.store.snapshot(now=self.now)["session"]["rows"], 2)

    def test_invalid_elapsed_row_preserves_a_break_in_energy_coverage(self):
        rows = [self.rows()[0], dict(self.rows()[0], elapsed_s="bad-time"), self.rows()[1]]
        self.write(rows)
        result = self.store.snapshot(now=self.now)
        self.assertIsNone(result["energy_budget"]["observed_s"])
        self.assertNotIn("drawn_kwh_from_pack_current", result["report"]["energy"])
        self.assertTrue(any("elapsed" in w for w in result["warnings"]))

    def test_history_size_is_bounded(self):
        rows = [dict(self.rows()[0], elapsed_s=i, pack_a=0) for i in range(MAX_HISTORY + 10)]
        self.write(rows)
        history = self.store.snapshot(now=self.now)["history"]
        self.assertEqual(len(history), MAX_HISTORY)
        self.assertEqual(history[0]["elapsed_s"], 10)

    def test_energy_budget_separates_parked_load_from_driving_and_returned_energy(self):
        def row(t, current, speed):
            return dict(elapsed_s=t, pack_v=400, pack_a=current, speed_kph=speed)
        result = energy_budget([row(0, 10, 0), row(10, 10, 0),
                                row(20, 100, 50), row(30, -100, 50)])
        self.assertAlmostEqual(result["stationary_drawn_kwh"], 4 * 10 / 3600, places=5)
        # First moving interval averages 22 kW; second crosses from +40 to
        # -40, returning and drawing 0.02778 kWh each, not 0.05556 each.
        self.assertAlmostEqual(result["moving_drawn_kwh"], (22 * 10 + 100) / 3600, places=5)
        self.assertAlmostEqual(result["moving_regen_kwh"], 100 / 3600, places=5)
        self.assertEqual(result["observed_s"], 30)

    def test_energy_budget_preserves_gaps_and_missing_is_not_zero(self):
        row = dict(pack_v=400, pack_a=100, speed_kph=50)
        result = energy_budget([dict(row, elapsed_s=0), {"elapsed_s": 10},
                                dict(row, elapsed_s=20), dict(row, elapsed_s=500)])
        self.assertIsNone(result["moving_drawn_kwh"])
        self.assertIsNone(result["observed_s"])
        self.assertEqual(result["skipped_intervals"], 3)

    def test_stationary_energy_in_is_not_reported_as_regen(self):
        rows = [dict(elapsed_s=t, pack_v=400, pack_a=-20, speed_kph=0) for t in (0, 10)]
        result = energy_budget(rows)
        self.assertEqual(result["moving_regen_kwh"], 0)
        self.assertAlmostEqual(result["stationary_energy_in_kwh"], 8 * 10 / 3600, places=5)

    def test_http_is_read_only_and_never_serves_arbitrary_files(self):
        self.write(self.rows())
        server = make_server(self.store, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urlopen(base + "/api/snapshot", timeout=3) as response:
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
                self.assertEqual(json.load(response)["schema"], 1)
            for url, expected in (("/../../etc/passwd", 404),
                                  ("/api/snapshot?session=..%2Fsecret.csv", 400),
                                  ("/api/snapshot?session=x&session=y", 400),
                                  ("/api/snapshot?command=04", 400)):
                with self.subTest(url=url), self.assertRaises(HTTPError) as error:
                    urlopen(base + url, timeout=3)
                self.assertEqual(error.exception.code, expected)
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(base + "/api/snapshot", data=b"04", method="POST"), timeout=3)
            self.assertEqual(error.exception.code, 501)
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(base + "/api/snapshot", headers={"Host": "untrusted.example"}), timeout=3)
            self.assertEqual(error.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
