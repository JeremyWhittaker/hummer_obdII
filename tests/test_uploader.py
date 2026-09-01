"""Upload is off unless it is deliberately turned on, and never loses data."""

import tempfile
import unittest
from pathlib import Path

from hummer_obd.config import Config
from hummer_obd.decode import PidValue
from hummer_obd.storage import Storage
from hummer_obd.uploader import UploadDisabled, UploadError, Uploader


class TestUploader(unittest.TestCase):
    def setUp(self):
        self.store = Storage(Path(tempfile.mkdtemp()) / "db.sqlite3")
        self.sid = self.store.start_session("uid")
        for i in range(5):
            self.store.add_sample(self.sid, PidValue("0C", "engine speed", i, "rpm", "410c", "ok"))
        self.cfg = Config()

    def tearDown(self):
        self.store.close()

    def test_disabled_by_default(self):
        uploader = Uploader(self.cfg, self.store, sender=lambda *a, **k: 200)
        self.assertFalse(uploader.enabled())
        with self.assertRaises(UploadDisabled):
            uploader.run_once()
        self.assertEqual(self.store.pending_count(), 5)

    def test_enabled_without_endpoint_stays_off(self):
        self.cfg.upload.enabled = True
        uploader = Uploader(self.cfg, self.store, sender=lambda *a, **k: 200)
        self.assertFalse(uploader.enabled())
        with self.assertRaises(UploadDisabled):
            uploader.run_once()

    def test_successful_batch_marks_rows(self):
        self.cfg.upload.enabled = True
        self.cfg.upload.endpoint = "https://example.invalid/ingest"
        self.cfg.upload.batch_size = 3
        seen = {}

        def sender(endpoint, payload, timeout=30.0):
            seen["endpoint"] = endpoint
            seen["payload"] = payload
            return 200

        result = Uploader(self.cfg, self.store, sender=sender).run_once()
        self.assertEqual(result.sent, 3)
        self.assertEqual(result.remaining, 2)
        self.assertEqual(len(seen["payload"]["samples"]), 3)
        self.assertEqual(seen["payload"]["schema"], "hummer-obd/sample-batch/1")

    def test_failure_leaves_the_buffer_intact(self):
        self.cfg.upload.enabled = True
        self.cfg.upload.endpoint = "https://example.invalid/ingest"

        def sender(endpoint, payload, timeout=30.0):
            raise OSError("no route to host")

        with self.assertRaises(UploadError):
            Uploader(self.cfg, self.store, sender=sender).run_once()
        self.assertEqual(self.store.pending_count(), 5)

    def test_http_error_leaves_the_buffer_intact(self):
        self.cfg.upload.enabled = True
        self.cfg.upload.endpoint = "https://example.invalid/ingest"
        with self.assertRaises(UploadError):
            Uploader(self.cfg, self.store, sender=lambda *a, **k: 500).run_once()
        self.assertEqual(self.store.pending_count(), 5)

    def test_raw_transcripts_are_never_uploaded(self):
        self.cfg.upload.enabled = True
        self.cfg.upload.endpoint = "https://example.invalid/ingest"
        captured = {}

        def sender(endpoint, payload, timeout=30.0):
            captured["payload"] = payload
            return 200

        Uploader(self.cfg, self.store, sender=sender).run_once()
        keys = set(captured["payload"]["samples"][0])
        self.assertEqual(keys, {"ts", "pid", "name", "value", "unit", "status", "raw_hex"})


if __name__ == "__main__":
    unittest.main()
