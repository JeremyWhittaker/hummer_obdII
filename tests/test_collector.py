"""Collector behaviour: safe PID validation, buffering, backoff, reconnect,
and bounded trial runs that stop themselves."""

import contextlib
import io
import signal
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from hummer_obd.collector import Collector, main as collector_main
from hummer_obd.collector import RunLimits
from hummer_obd.config import CollectorConfig, Config
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


class TestBoundedRun(unittest.TestCase):
    """A trial run has to end by itself, promptly, and say why it stopped."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def _run(self, transport, cfg, **kwargs):
        collector = Collector(cfg, logger=lambda *_: None, **kwargs)
        with mock.patch("hummer_obd.collector.SerialTransport", return_value=transport):
            rc = collector.run()
        return rc, collector

    def _events(self, cfg, kind):
        with Storage(cfg.path(cfg.collector.database)) as store:
            rows = store.conn.execute(
                "SELECT detail FROM events WHERE kind=? ORDER BY id", (kind,)
            ).fetchall()
            return [row["detail"] for row in rows]

    @contextlib.contextmanager
    def _signal_handlers_restored(self):
        """Put the process-wide SIGINT/SIGTERM handlers back after ``main``.

        ``main`` installs ``collector.stop`` as the handler for both.  Left in
        place those outlive the test and point at a dead Collector, so Ctrl-C
        would be silently swallowed for the rest of the suite.
        """
        saved = {sig: signal.getsignal(sig)
                 for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            yield
        finally:
            for sig, handler in saved.items():
                signal.signal(sig, handler)

    def _sample_count(self, cfg):
        with Storage(cfg.path(cfg.collector.database)) as store:
            return store.conn.execute("SELECT COUNT(*) AS n FROM samples").fetchone()["n"]

    def test_max_cycles_stops_after_exactly_that_many_cycles(self):
        cfg = make_config(self.root)
        cfg.collector.max_cycles = 3
        rc, collector = self._run(FakeTransport(), cfg)
        self.assertEqual(rc, 0)
        self.assertEqual(collector.cycles, 3)
        self.assertEqual(self._sample_count(cfg), 6)  # three passes over two PIDs
        self.assertEqual(self._events(cfg, "stopped"), ["max_cycles reached"])

    def test_max_cycles_can_be_overridden_per_run(self):
        cfg = make_config(self.root)
        cfg.collector.max_cycles = 0  # unlimited in the deployed config
        rc, collector = self._run(FakeTransport(), cfg, max_cycles=2)
        self.assertEqual(rc, 0)
        self.assertEqual(collector.cycles, 2)

    def test_an_idle_cycle_counts_toward_max_cycles(self):
        # A sleeping vehicle answers NO DATA to everything.  If those cycles
        # did not count, a bounded trial would never end.
        cfg = make_config(self.root)
        cfg.collector.max_cycles = 2
        rc, collector = self._run(FakeTransport(replies={}), cfg)
        self.assertEqual(rc, 0)
        self.assertEqual(collector.cycles, 2)
        self.assertEqual(self._events(cfg, "idle_backoff"), ["no data this cycle"])
        self.assertEqual(self._events(cfg, "stopped"), ["max_cycles reached"])

    def test_a_zero_max_cycles_override_is_rejected_not_treated_as_unlimited(self):
        """The one input where a typo would remove a bound instead of adding one.

        ``max_cycles = 0`` means "no limit" in the config file.  Accepting the
        same value from the command line would let ``--max-cycles 0`` turn a
        config that said ``20`` into an unbounded run on a real vehicle, which
        is the opposite of what every other check here does.
        """
        collector_cfg = CollectorConfig()
        collector_cfg.max_cycles = 20
        with self.assertRaises(ValueError) as caught:
            RunLimits.from_config(collector_cfg, max_cycles=0)
        self.assertIn("--max-cycles", str(caught.exception))
        # Omitting the flag still uses the configured bound.
        self.assertEqual(RunLimits.from_config(collector_cfg).max_cycles, 20)

    def test_duration_stops_without_overshooting_a_long_backoff(self):
        cfg = make_config(self.root)
        cfg.collector.poll_interval_s = 30.0
        cfg.collector.idle_backoff_s = 30.0
        cfg.collector.duration_s = 0.3
        started = time.monotonic()
        rc, collector = self._run(FakeTransport(), cfg)
        elapsed = time.monotonic() - started
        self.assertEqual(rc, 0)
        self.assertEqual(collector.cycles, 1)  # the deadline landed inside the first sleep
        self.assertLess(elapsed, 5.0)  # the 30s sleep was interrupted, not slept through
        self.assertEqual(self._events(cfg, "stopped"), ["duration reached"])

    def test_the_wait_between_cycles_wakes_promptly_on_stop(self):
        collector = Collector(make_config(self.root), logger=lambda *_: None)
        timer = threading.Timer(0.02, collector.stop)  # stands in for SIGTERM
        with mock.patch("hummer_obd.collector._SLEEP_SLICE_S", 0.01):
            timer.start()
            started = time.monotonic()
            collector._sleep(30.0, None)
            elapsed = time.monotonic() - started
        timer.cancel()
        self.assertLess(elapsed, 1.0)

    def test_once_is_unaffected_by_the_trial_limits(self):
        cfg = make_config(self.root)
        cfg.collector.max_cycles = 5
        cfg.collector.duration_s = 0.0001  # long expired by the time the cycle ends
        rc, collector = self._run(FakeTransport(), cfg, once=True)
        self.assertEqual(rc, 0)
        self.assertEqual(collector.cycles, 1)
        self.assertEqual(self._sample_count(cfg), 2)
        self.assertEqual(self._events(cfg, "stopped"), [])  # no limit was in force

    def test_a_pass_cut_short_by_a_stop_does_not_count_as_a_cycle(self):
        # SIGTERM lands between two PIDs of the same pass.  That half pass is
        # not a cycle: counting it would let a bounded trial report one more
        # cycle than it actually completed, and would let the last PID of the
        # list be silently dropped from every reported count.
        collector = Collector(make_config(self.root), logger=lambda *_: None)
        transport = FakeTransport()
        original_send = transport.send

        def send(command, timeout=None):
            reply = original_send(command, timeout)
            if command == "010C":  # first PID of the pass; stop before the second
                collector.stop()
            return reply

        transport.send = send
        with mock.patch("hummer_obd.collector.SerialTransport", return_value=transport):
            rc = collector.run()
        self.assertEqual(rc, 0)
        self.assertEqual(collector.cycles, 0)  # the pass never finished
        self.assertEqual(self._events(make_config(self.root), "stopped"), [])

    def test_non_positive_overrides_are_rejected(self):
        for kwargs in ({"poll_interval_s": 0}, {"poll_interval_s": -1},
                       {"max_cycles": -1}, {"duration_s": -0.5}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    Collector(make_config(self.root), logger=lambda *_: None, **kwargs)

    def test_main_rejects_a_bad_interval_before_opening_the_device(self):
        with mock.patch("hummer_obd.collector.SerialTransport") as serial:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    collector_main(["--force", "--root", str(self.root),
                                    "--poll-interval-s", "0"])
        self.assertEqual(caught.exception.code, 2)
        serial.assert_not_called()

    def test_main_runs_a_bounded_trial_and_returns_zero(self):
        transport = FakeTransport()
        with self._signal_handlers_restored():
            with mock.patch("hummer_obd.collector.SerialTransport", return_value=transport):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = collector_main(["--force", "--root", str(self.root),
                                         "--max-cycles", "2", "--poll-interval-s", "0.01"])
        self.assertEqual(rc, 0)
        cfg = Config()
        cfg.root = self.root
        self.assertEqual(self._events(cfg, "stopped"), ["max_cycles reached"])


if __name__ == "__main__":
    unittest.main()
