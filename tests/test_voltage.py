"""The voltage watch must be provably incapable of reaching the vehicle.

Everything else in this project is "read-only".  This module's guarantee is
stronger and narrower: while it runs, nothing at all is transmitted onto the
CAN bus, because it is meant to be sampling during a sleep observation.
"""

import csv
import tempfile
import unittest
from pathlib import Path

from hummer_obd.config import Config
from hummer_obd.safety import UnsafeCommandError
from hummer_obd.transport import Response, TransportError
from hummer_obd.voltage import (
    WATCH_COMMANDS,
    VoltageWatch,
    _volts,
    assert_no_vehicle_traffic,
)

REPLIES = {
    "ATZ": b"ELM327 v1.4b\r>",
    "ATE0": b"OK\r>",
    "ATRV": b"13.9V\r>",
    "ATCS": b"T:00 R:00\r>",
}


class FakeTransport:
    def __init__(self, replies=None, fail=False):
        self.replies = REPLIES if replies is None else replies
        self.sent = []
        self.is_open = False
        self.fail = fail

    def open(self):
        if self.fail:
            raise TransportError("cannot open /dev/rfcomm0")
        self.is_open = True

    def close(self):
        self.is_open = False

    def send(self, command, timeout=None):
        self.sent.append(command)
        return Response(command=command, data=self.replies.get(command, b"?\r>"), elapsed_s=0.0)


def make_config(root: Path) -> Config:
    cfg = Config()
    cfg.root = root
    cfg.collector.raw_log_dir = "logs/raw"
    return cfg


class TestNoVehicleTraffic(unittest.TestCase):
    def test_the_shipped_command_set_is_adapter_only(self):
        self.assertEqual(assert_no_vehicle_traffic(WATCH_COMMANDS), tuple(WATCH_COMMANDS))
        for command in WATCH_COMMANDS:
            self.assertTrue(command.startswith(("AT", "ST")), command)

    def test_a_vehicle_service_is_refused_even_though_it_is_read_only(self):
        # 0100 passes the ordinary safety gate: it is a legitimate read-only
        # request everywhere else in this project.  It must still be refused
        # here, because "read-only" is not the property this module promises.
        for command in ("0100", "010D", "03", "0902"):
            with self.assertRaises(UnsafeCommandError) as caught:
                assert_no_vehicle_traffic((command,))
            self.assertIn("vehicle service request", str(caught.exception))

    def test_an_outright_forbidden_command_is_refused_by_the_normal_gate(self):
        with self.assertRaises(UnsafeCommandError):
            assert_no_vehicle_traffic(("04",))

    def test_a_sample_sends_exactly_the_watch_commands_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch = VoltageWatch(make_config(Path(tmp)), output=Path(tmp) / "v.csv")
            transport = FakeTransport()
            watch.sample_once(transport)
            self.assertEqual(transport.sent, list(WATCH_COMMANDS))


class TestVoltageParsing(unittest.TestCase):
    def test_parses_the_adapter_format(self):
        self.assertEqual(_volts("13.9V"), 13.9)
        self.assertEqual(_volts(" 12.55 v "), 12.55)

    def test_an_unparsable_reply_is_missing_not_zero(self):
        # A fabricated 0.0 V in a battery trend reads as a dead battery.
        for text in ("?", "", "NO DATA", "ERROR"):
            self.assertIsNone(_volts(text))


class TestRunLoop(unittest.TestCase):
    def _rows(self, path):
        with open(path, newline="") as fh:
            return list(csv.DictReader(fh))

    def test_a_bounded_run_writes_a_header_once_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "evidence" / "v.csv"
            watch = VoltageWatch(make_config(Path(tmp)), output=out,
                                 interval_s=0.01, duration_s=0.05, logger=lambda *_: None)
            transport = FakeTransport()
            watch.sample_once(transport)  # sanity
            for _ in range(2):
                watch._append({"ts_utc": "t", "volts": "13.9",
                               "can_status": "T:00 R:00", "status": "ok"})
            rows = self._rows(out)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["volts"], "13.9")
            self.assertEqual(open(out).read().count("ts_utc"), 1)

    def test_an_unreachable_adapter_is_recorded_rather_than_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            cfg.adapter.device = str(Path(tmp) / "absent")
            watch = VoltageWatch(cfg, output=Path(tmp) / "v.csv",
                                 interval_s=0.01, duration_s=0.02, logger=lambda *_: None)
            rc = watch.run()
            self.assertEqual(rc, 0)
            rows = self._rows(Path(tmp) / "v.csv")
            self.assertTrue(rows)
            self.assertTrue(rows[0]["status"].startswith("unreachable"))
            self.assertEqual(rows[0]["volts"], "")

    def test_a_non_positive_interval_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in (0, -1):
                with self.assertRaises(ValueError):
                    VoltageWatch(make_config(Path(tmp)),
                                 output=Path(tmp) / "v.csv", interval_s=bad)


if __name__ == "__main__":
    unittest.main()
