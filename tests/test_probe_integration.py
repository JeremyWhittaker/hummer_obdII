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
from hummer_obd.decode import ecu_from_header
from hummer_obd.safety import ALLOWED_OBD_MODES, FORBIDDEN_SERVICES, is_safe
from hummer_obd.session import RECEIVE_FILTER_CLEAR

from elm_simulator import ElmSimulator

#: Exactly what the quick probe puts on the wire against the stock simulator.
#: Spelled out rather than derived, because the point of the assertion is that
#: work added behind ``--max`` cannot leak into the default path: a list built
#: from the same constants the probe uses would move whenever the probe did.
DEFAULT_COMMANDS = [
    "ATZ", "ATE0", "ATL0", "ATS0", "ATH1", "ATAT1",
    "ATI", "AT@1", "AT@2", "STI", "STDI", "ATRV",
    "ATSP0", "0100", "ATDP", "ATDPN",
    "0100", "0120", "0140", "0160",
    "0101", "0103", "0104", "0105", "0106", "0107", "010B", "010C", "010D",
    "010E", "010F", "0110", "0111", "0113", "0115", "011C", "011F", "0121",
    "0124", "012E", "012F", "0130", "0131", "0132", "0133", "013C", "0141",
    "0142", "0143", "0144", "0145", "0147", "0149", "014A", "014C", "014D",
    "014E", "0151", "0153",
    "03", "07", "0A",
    "0902", "0900", "0904", "0906", "0908", "090A",
]

#: A 29-bit vehicle, which is what this truck actually is (ISO 15765-4, CAN
#: 29-bit): replies arrive on ``18DAF1<ecu>``.  The support bitmap advertises
#: two PIDs so the expected command list stays short enough to read, and 0105 is
#: answered by two modules with different numbers -- the case a single-value
#: probe reports wrongly rather than incompletely.
WIDE_RESPONSES = {
    "0100": "18DAF110 06 41 00 18 00 00 00",
    "0104": "18DAF110 03 41 04 80",
    "0105": "18DAF110 03 41 05 5A\r18DAF145 03 41 05 5C",
    "03": "18DAF110 02 43 00",
    "07": "18DAF110 02 47 00",
    "0A": "18DAF110 02 4A 00",
    "0900": "18DAF110 06 49 00 40 00 00 00",
    "0902": ("18DAF110 10 14 49 02 01 31 47 31\r"
             "18DAF110 21 4A 43 35 34 34 34 52\r"
             "18DAF110 22 37 32 35 32 33 36 37"),
    # Service 06 advertises MIDs 01 and 02 and nothing in the next bank.
    "0600": "18DAF110 06 46 00 C0 00 00 00",
    "0601": "18DAF110 10 0A 46 01 8C 24 01 90\r18DAF110 21 00 00 03 E8 00 00 00",
    "0602": "NO DATA",
    "020400": "18DAF110 04 42 04 00 80",
    "020500": "18DAF110 04 42 05 00 5A",
}

#: One module's answer to 090A, keyed by its address.  "ECM" and "BCM" in hex.
ECU_NAME_FRAMES = {
    "10": "18DAF110 07 49 0A 01 45 43 4D",
    "45": "18DAF145 07 49 0A 01 42 43 4D",
}


class WideElm(ElmSimulator):
    """A 29-bit adapter that actually honours the CAN receive filter.

    Every module answers ``090A`` at once, so the probe narrows reception to one
    address at a time to learn which name belongs to which module.  A simulator
    that ignored ``ATCRA`` would let a broken narrowing pass unnoticed, which is
    the one thing this fixture exists to catch.
    """

    RESPONSES = dict(ElmSimulator.RESPONSES, **WIDE_RESPONSES)

    def __init__(self, responses=None):
        super().__init__(responses)
        self.receive_filter = ""

    def answer(self, command: str) -> str:
        if command.startswith("ATCRA"):
            self.receive_filter = ecu_from_header(command[len("ATCRA"):])
            return "OK"
        if command == RECEIVE_FILTER_CLEAR:
            self.receive_filter = ""
            return "OK"
        if command == "090A":
            if self.receive_filter in ECU_NAME_FRAMES:
                return ECU_NAME_FRAMES[self.receive_filter]
            return "\r".join(ECU_NAME_FRAMES.values())
        return super().answer(command)


class Args:
    def __init__(self, **kw):
        self.device = None
        self.config = None
        self.root = "."
        self.summary = None
        self.database = None
        self.protocol_timeout = 5.0
        self.replay = None
        self.max = False
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

    def test_the_default_probe_sends_exactly_what_it_always_sent(self):
        # The extra questions live behind --max.  Without the flag the wire
        # traffic has to be identical to what shipped, command for command.
        probe.main(["--device", self.sim.device, "--root", str(self.root),
                    "--protocol-timeout", "5"])
        self.assertEqual(self.sim.received, DEFAULT_COMMANDS)
        leaked = [c for c in self.sim.received
                  if c.startswith(("02", "06", "ATCRA", "ATCM"))]
        self.assertEqual(leaked, [], "a --max request reached the default path")

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

    def test_exact_command_mode_preserves_order_and_masks_vin_summary(self):
        commands = [
            "ATZ", "ATE0", "ATL0", "ATS0", "ATH1", "ATAL", "ATSP0",
            "ATDP", "ATDPN", "ATRV", "0100", "0120", "0140", "0160",
            "0180", "0900", "0902", "0904", "0906", "03", "07", "0A",
            "0142", "010D", "015B",
        ]
        summary_path = self.root / "commands-summary.json"
        rc = probe.main([
            "--device", self.sim.device,
            "--root", str(self.root),
            "--summary", str(summary_path),
            "--protocol-timeout", "5",
            "--commands", *commands,
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(self.sim.received, commands)
        summary = json.loads(summary_path.read_text())
        self.assertEqual([item["command"] for item in summary["commands"]], commands)
        vin_item = next(item for item in summary["commands"] if item["command"] == "0902")
        self.assertIn("vin_masked", vin_item)
        self.assertNotIn("lines", vin_item)
        self.assertNotIn("1G1JC5444R7252367", summary_path.read_text())
        log = next((self.root / "logs" / "raw").glob("command-probe-*.jsonl"))
        tx = [
            decode_record(record).decode("ascii").strip()
            for record in iter_records(log)
            if record.get("kind") == "io" and record["dir"] == "tx"
        ]
        self.assertEqual(tx, commands)

    def test_exact_command_mode_rejects_whole_set_before_open(self):
        rc = probe.main([
            "--device", self.sim.device,
            "--root", str(self.root),
            "--commands", "0100", "04", "ATRV",
        ])
        self.assertEqual(rc, 2)
        self.assertEqual(self.sim.received, [])
        self.assertFalse(list((self.root / "logs" / "raw").glob("command-probe-*.jsonl")))


class TestThoroughProbe(unittest.TestCase):
    """``--max`` asks more of more modules -- and only ever asks."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.sim = WideElm().start()

    def tearDown(self):
        self.sim.stop()

    def probe_run(self, *extra):
        summary_path = self.root / "summary.json"
        rc = probe.main(["--device", self.sim.device, "--root", str(self.root),
                         "--summary", str(summary_path), "--protocol-timeout", "5",
                         *extra])
        self.assertEqual(rc, 0)
        return json.loads(summary_path.read_text())

    def assert_every_command_is_a_read(self, commands):
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(is_safe(command), f"unsafe command reached the port: {command}")
                if command.startswith(("AT", "ST")):
                    continue
                self.assertIn(command[:2], ALLOWED_OBD_MODES)
                self.assertNotIn(command[:2], FORBIDDEN_SERVICES)

    def test_max_transmits_exactly_this_read_only_conversation(self):
        summary = self.probe_run("--max")
        self.assertTrue(summary["max"])
        self.assertEqual(self.sim.received, [
            "ATZ", "ATE0", "ATL0", "ATS0", "ATH1", "ATAT1",
            "ATI", "AT@1", "AT@2", "STI", "STDI", "ATRV",
            "ATSP0", "0100", "ATDP", "ATDPN",
            "0100",
            "0104", "0105",
            "03", "07", "0A",
            "0600", "0601", "0602",
            "0902", "0900",
            "ATCRA18DAF110", "090A", "ATCRA18DAF145", "090A", RECEIVE_FILTER_CLEAR,
        ])
        self.assert_every_command_is_a_read(self.sim.received)
        # Named individually as well, because these are the ones that would do
        # damage: clear codes, actuate a component, and the UDS services the
        # gate exists to keep off a live truck.
        for never in ("04", "08", "22F190", "2210F1", "1003", "2701", "2E0100",
                      "3101", "3E00", "1101"):
            self.assertNotIn(never, self.sim.received)
        self.assertEqual([c for c in self.sim.received if c[:2] in FORBIDDEN_SERVICES], [])

    def test_every_module_that_answered_is_recorded_not_just_the_first(self):
        summary = self.probe_run("--max")
        by_ecu = summary["samples_by_ecu"]["05"]
        self.assertEqual([entry["ecu"] for entry in by_ecu], ["10", "45"])
        self.assertEqual([entry["value"] for entry in by_ecu], [50.0, 52.0])
        # The single-value key keeps its old meaning -- the first answer -- so
        # anything already reading `samples` is unaffected.
        self.assertEqual(summary["samples"]["05"]["value"], 50.0)
        self.assertEqual(summary["ecus"]["addresses"], ["10", "45"])

    def test_each_module_is_asked_for_its_own_name_behind_a_receive_filter(self):
        summary = self.probe_run("--max")
        self.assertEqual(summary["ecus"]["names"], {"10": "ECM", "45": "BCM"})
        # And the adapter is left listening to everyone again.
        self.assertEqual(self.sim.received[-1], RECEIVE_FILTER_CLEAR)
        self.assertEqual(self.sim.receive_filter, "")

    def test_monitor_ids_are_discovered_then_read_bank_by_bank(self):
        summary = self.probe_run("--max")
        self.assertEqual(summary["monitors"]["supported_mids"], ["01", "02"])
        self.assertEqual(set(summary["monitors"]["results"]), {"01", "02"})
        self.assertEqual(summary["monitors"]["results"]["01"]["status"], "ok")
        self.assertIsInstance(summary["monitors"]["results"]["01"]["tests"], list)
        # A MID that answers nothing is reported as nothing, not as a decode.
        self.assertEqual(summary["monitors"]["results"]["02"]["status"], "no_data")
        self.assertEqual(summary["monitors"]["results"]["02"]["tests"], [])
        # The bitmap points at no further bank, so no further bank is asked for.
        self.assertNotIn("0620", self.sim.received)

    def test_freeze_frames_are_not_requested_when_no_code_is_stored(self):
        summary = self.probe_run("--max")
        self.assertEqual(summary["dtcs"]["03"]["codes"], [])
        self.assertEqual(summary["freeze_frames"]["frames"], {})
        self.assertIn("DTC", summary["freeze_frames"]["skipped"])
        self.assertEqual([c for c in self.sim.received if c.startswith("02")], [])

    def test_freeze_frames_are_requested_once_a_code_is_stored(self):
        self.sim.responses["03"] = "18DAF110 04 43 01 01 33"
        summary = self.probe_run("--max")
        self.assertEqual(summary["dtcs"]["03"]["codes"], ["P0133"])
        self.assertEqual(summary["freeze_frames"]["skipped"], "")
        self.assertEqual(sorted(summary["freeze_frames"]["frames"]), ["04", "05"])
        self.assertEqual([c for c in self.sim.received if c.startswith("02")],
                         ["020400", "020500"])
        snapshot = summary["freeze_frames"]["frames"]["05"]
        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["ecu"], "10")
        self.assertEqual(snapshot["value"], 50.0)
        self.assert_every_command_is_a_read(self.sim.received)

    def test_the_vin_is_masked_on_the_thorough_path_too(self):
        summary = self.probe_run("--max")
        self.assertTrue(summary["vin_masked"].startswith("1G1"))
        self.assertNotIn("1G1JC5444R7252367", json.dumps(summary))
        self.assertNotIn("1G1JC5444R7252367", (self.root / "summary.json").read_text())


if __name__ == "__main__":
    unittest.main()
