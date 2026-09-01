"""Collector behaviour: safe PID validation, buffering, backoff, reconnect."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hummer_obd.collector import Collector
from hummer_obd.config import Config
from hummer_obd.safety import UnsafeCommandError
from hummer_obd.storage import Storage
from hummer_obd.transport import Response, TransportError

REPLIES = {
    "ATZ": b"ELM327 v1.5\r>",
    "ATI": b"OBDLink MX+ r5.7\r>",
    "ATRV": b"12.3V\r>",
    "ATDP": b"ISO 15765-4 (CAN 11/500)\r>",
    "0100": b"41 00 BE 3F A8 13\r>",
    "010C": b"41 0C 1A F8\r>",
    "010D": b"41 0D 32\r>",
    "03": b"43 00\r>",
    "07": b"47 00\r>",
    "0A": b"4A 00\r>",
}


class FakeTransport:
    def __init__(self, replies=None, fail_after=None):
        self.replies = replies if replies is not None else REPLIES
        self.sent = []
        self.is_open = False
        self.opens = 0
        self.fail_after = fail_after

    def open(self):
        self.is_open = True
        self.opens += 1

    def close(self):
        self.is_open = False

    def reconnect(self, attempt=0):
        self.close()
        self.open()

    def send(self, command, timeout=None):
        self.sent.append(command)
        if self.fail_after is not None and len(self.sent) > self.fail_after:
            raise TransportError("link dropped")
        return Response(command=command, data=self.replies.get(command, b"NO DATA\r>"), elapsed_s=0.0)


def make_config(root: Path) -> Config:
    cfg = Config()
    cfg.root = root
    cfg.collector.enabled = True
    cfg.collector.pids = ["010C", "010D"]
    cfg.collector.poll_interval_s = 0.0
    cfg.collector.idle_backoff_s = 0.0
    cfg.collector.dtc_interval_s = 1.0
    return cfg


class TestCollector(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def _run(self, transport, cfg=None):
        cfg = cfg or make_config(self.root)
        collector = Collector(cfg, once=True, logger=lambda *_: None)
        with mock.patch("hummer_obd.collector.SerialTransport", return_value=transport):
            rc = collector.run()
        return rc, cfg

    def test_bad_pid_in_config_is_rejected_before_any_io(self):
        cfg = make_config(self.root)
        cfg.collector.pids = ["04"]
        with self.assertRaises(UnsafeCommandError):
            Collector(cfg, once=True, logger=lambda *_: None)

    def test_single_cycle_stores_samples_and_dtcs(self):
        transport = FakeTransport()
        rc, cfg = self._run(transport)
        self.assertEqual(rc, 0)
        with Storage(cfg.path(cfg.collector.database)) as store:
            rows = store.latest_samples(10)
            pids = {row["pid"] for row in rows}
            self.assertEqual(pids, {"0C", "0D"})
            dtcs = store.conn.execute("SELECT mode FROM dtc_reads ORDER BY mode").fetchall()
            self.assertEqual([r["mode"] for r in dtcs], ["03", "07", "0A"])

    def test_samples_stay_buffered_locally(self):
        transport = FakeTransport()
        rc, cfg = self._run(transport)
        with Storage(cfg.path(cfg.collector.database)) as store:
            self.assertEqual(store.pending_count(), 2)  # nothing uploaded

    def test_only_allowlisted_commands_are_sent(self):
        transport = FakeTransport()
        self._run(transport)
        for command in transport.sent:
            self.assertFalse(command.startswith("04"), command)
            self.assertFalse(command.startswith("22"), command)

    def test_sleeping_vehicle_is_recorded_not_escalated(self):
        transport = FakeTransport(replies={})  # every request answers NO DATA
        rc, cfg = self._run(transport)
        self.assertEqual(rc, 0)
        with Storage(cfg.path(cfg.collector.database)) as store:
            rows = store.latest_samples(10)
            self.assertTrue(all(row["status"] == "no_data" for row in rows))

    def test_transport_failure_gives_up_after_the_error_ceiling(self):
        cfg = make_config(self.root)
        cfg.collector.max_consecutive_errors = 2
        transport = FakeTransport(fail_after=0)
        collector = Collector(cfg, once=False, logger=lambda *_: None)
        with mock.patch("hummer_obd.collector.SerialTransport", return_value=transport):
            rc = collector.run()
        self.assertEqual(rc, 3)
        self.assertGreaterEqual(transport.opens, 2)  # it did try to reconnect

    def test_raw_log_is_written_for_the_session(self):
        transport = FakeTransport()
        rc, cfg = self._run(transport)
        logs = list((self.root / "logs" / "raw").glob("collect-*.jsonl"))
        self.assertEqual(len(logs), 1)
        self.assertIn("session_start", logs[0].read_text())


if __name__ == "__main__":
    unittest.main()
