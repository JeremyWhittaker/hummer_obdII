"""The raw log must preserve bytes exactly and only ever append."""

import base64
import json
import tempfile
import unittest
from pathlib import Path

from hummer_obd.rawlog import RawLog, decode_record, iter_records, render_display


class TestRawLog(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "raw.jsonl"

    def test_bytes_round_trip_exactly(self):
        payloads = [b"010C\r", b"41 0C 1A F8\r\r>", bytes(range(256)), b"", b"\xff\xfe\x00\x01"]
        with RawLog(self.path, "s") as log:
            for payload in payloads:
                log.log_rx(payload)
        records = [r for r in iter_records(self.path) if r.get("kind") == "io"]
        self.assertEqual(len(records), len(payloads))
        for payload, record in zip(payloads, records):
            self.assertEqual(decode_record(record), payload)
            self.assertEqual(base64.b64decode(record["b64"]), payload)
            self.assertEqual(bytes.fromhex(record["hex"]), payload)
            self.assertEqual(record["len"], len(payload))

    def test_display_field_is_lossy_but_present(self):
        with RawLog(self.path, "s") as log:
            record = log.log_tx(b"AT\x00Z\r")
        self.assertEqual(record["display"], "AT\\x00Z\\r")
        self.assertEqual(decode_record(record), b"AT\x00Z\r")

    def test_appends_never_rewrite(self):
        with RawLog(self.path, "s1") as log:
            log.log_tx(b"first")
        first_bytes = self.path.read_bytes()
        with RawLog(self.path, "s2") as log:
            log.log_tx(b"second")
        second_bytes = self.path.read_bytes()
        self.assertTrue(second_bytes.startswith(first_bytes))
        self.assertGreater(len(second_bytes), len(first_bytes))

    def test_sequence_numbers_are_monotonic(self):
        with RawLog(self.path, "s") as log:
            for i in range(5):
                log.log_tx(b"x")
        seqs = [r["seq"] for r in iter_records(self.path)]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs))

    def test_truncated_line_is_reported_not_dropped(self):
        with RawLog(self.path, "s") as log:
            log.log_tx(b"ok")
        with open(self.path, "a") as fh:
            fh.write('{"kind": "io", "b6')
        records = list(iter_records(self.path))
        self.assertEqual(records[-1]["kind"], "corrupt")

    def test_rejects_non_bytes(self):
        with RawLog(self.path, "s") as log:
            with self.assertRaises(TypeError):
                log.log_tx("a string")
            with self.assertRaises(ValueError):
                log.log_bytes("sideways", b"x")

    def test_inconsistent_record_is_detected(self):
        with RawLog(self.path, "s") as log:
            record = log.log_tx(b"abc")
        record = dict(record)
        record["hex"] = "00"
        with self.assertRaises(ValueError):
            decode_record(record)

    def test_render_display(self):
        self.assertEqual(render_display(b"A\r\n\t\x01"), "A\\r\\n\\t\\x01")


if __name__ == "__main__":
    unittest.main()
