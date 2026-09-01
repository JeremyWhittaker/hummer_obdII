"""End-to-end probe run against a simulated adapter on a real PTY.

This exercises the actual serial transport, safety gate, raw log and decoders
together — everything except the vehicle.
"""

import json
import tempfile
import unittest
from pathlib import Path

from hummer_obd import probe
from hummer_obd.rawlog import decode_record, iter_records
from hummer_obd.safety import is_safe

from elm_simulator import ElmSimulator


class Args:
    def __init__(self, **kw):
        self.device = None
        self.config = None
        self.root = "."
        self.summary = None
        self.database = None
        self.protocol_timeout = 5.0
        self.replay = None
        self.__dict__.update(kw)


class TestProbeEndToEnd(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.sim = ElmSimulator().start()

    def tearDown(self):
        self.sim.stop()

    def test_full_probe(self):
        summary_path = self.root / "summary.json"
        rc = probe.main(["--device", self.sim.device, "--root", str(self.root),
                         "--summary", str(summary_path), "--protocol-timeout", "5"])
        self.assertEqual(rc, 0)
        summary = json.loads(summary_path.read_text())

        # Adapter identification and protocol.
        self.assertIn("OBDLink MX+", summary["adapter"]["ATI"])
        self.assertIn("STN2255", summary["adapter"]["STI"])
        self.assertEqual(summary["adapter"]["ATRV"], "13.9V")
        self.assertIn("CAN", summary["adapter"]["protocol"])

        # Supported PIDs and a decoded sample.
        self.assertIn("0C", summary["supported_pids"])
        self.assertAlmostEqual(summary["samples"]["0C"]["value"], 1726.0)
        self.assertEqual(summary["samples"]["42"]["unit"], "V")

        # DTC reads happened for all three read-only services and found none.
        self.assertEqual(set(summary["dtcs"]), {"03", "07", "0A"})
        self.assertEqual(summary["dtcs"]["03"]["codes"], [])

        # The VIN is decoded but only ever reported masked.
        self.assertTrue(summary["vin_masked"].startswith("1G1"))
        self.assertNotIn("1G1JC5444R7252367", json.dumps(summary))

        # A PID the vehicle does not advertise is skipped, not guessed at,
        # and is reported as such rather than as a value.
        self.assertEqual(summary["samples"]["5C"]["status"], "not_supported")
        self.assertNotIn("015C", self.sim.received)
        # Service 09 item 0A is advertised but answers NO DATA: reported, not invented.
        self.assertEqual(summary["service09"]["0A"]["status"], "no_data")
        self.assertIsNone(summary["service09"]["0A"]["value"])

    def test_no_forbidden_command_reached_the_port(self):
        probe.main(["--device", self.sim.device, "--root", str(self.root),
                    "--protocol-timeout", "5"])
        self.assertTrue(self.sim.received, "the simulator saw no commands at all")
        for command in self.sim.received:
            with self.subTest(command=command):
                self.assertTrue(is_safe(command), f"unsafe command reached the port: {command}")
        self.assertNotIn("04", self.sim.received)

    def test_raw_log_is_byte_exact_and_complete(self):
        probe.main(["--device", self.sim.device, "--root", str(self.root),
                    "--protocol-timeout", "5"])
        logs = sorted((self.root / "logs" / "raw").glob("probe-*.jsonl"))
        self.assertEqual(len(logs), 1)
        records = list(iter_records(logs[0]))
        io_records = [r for r in records if r.get("kind") == "io"]
        self.assertTrue(io_records)
        sent = [decode_record(r).decode("ascii").strip() for r in io_records if r["dir"] == "tx"]
        self.assertEqual(sent, self.sim.received)
        received = [decode_record(r) for r in io_records if r["dir"] == "rx"]
        self.assertTrue(all(b">" in r for r in received))
        self.assertEqual(records[0]["kind"], "event")
        self.assertEqual(records[0]["event"], "session_start")

    def test_replay_reads_the_log_back(self):
        probe.main(["--device", self.sim.device, "--root", str(self.root),
                    "--protocol-timeout", "5"])
        log = sorted((self.root / "logs" / "raw").glob("probe-*.jsonl"))[0]
        self.assertEqual(probe.main(["--replay", str(log)]), 0)


if __name__ == "__main__":
    unittest.main()
