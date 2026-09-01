"""Storage, configuration and display-rendering tests."""

import tempfile
import textwrap
import unittest
from pathlib import Path

from hummer_obd.config import load_config
from hummer_obd.decode import PidValue
from hummer_obd.display.status import StatusData, render_status_image
from hummer_obd.storage import Storage


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
