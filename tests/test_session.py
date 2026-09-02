"""Session tests run the real command sequence against a scripted adapter."""

import tempfile
import unittest
from pathlib import Path

from hummer_obd.rawlog import RawLog
from hummer_obd.safety import validate_command
from hummer_obd.session import RECEIVE_FILTER_CLEAR, AdapterSession
from hummer_obd.transport import Response, TransportError


class ScriptedTransport:
    """Answers from a table and records every command it is asked to send."""

    def __init__(self, table, fail_on=()):
        self.table = table
        self.sent = []
        #: Commands that raise instead of answering, so a test can put a dropped
        #: Bluetooth link in the middle of a sequence rather than at its end.
        self.fail_on = frozenset(fail_on)

    def send(self, command, timeout=None):
        command = validate_command(command)  # same gate the real transport uses
        self.sent.append(command)
        if command in self.fail_on:
            raise TransportError(f"link dropped during {command}")
        data = self.table.get(command, b"NO DATA\r\r>")
        return Response(command=command, data=data, elapsed_s=0.0)

    def open(self):
        pass

    def close(self):
        pass


#: One module's answer to 090A, keyed by the address the receive filter picked.
#: "ECM" and "BCM" in hex.
ECU_NAME_REPLIES = {
    "10": b"18DAF110 07 49 0A 01 45 43 4D\r>",
    "45": b"18DAF145 07 49 0A 01 42 43 4D\r>",
}

#: Two modules answer 0142: the same request, two different measurements.
TWO_ECU_VOLTAGE = b"18DAF110 04 41 42 33 A0\r18DAF145 04 41 42 33 90\r>"

TABLE = {
    "ATZ": b"ELM327 v1.5\r\r>",
    "ATI": b"OBDLink MX+ r5.7\r>",
    "AT@1": b"OBDLink MX+\r>",
    "STI": b"STN2255 v5.7.0\r>",
    "STDI": b"OBDLink MX+ r5.7\r>",
    "ATRV": b"12.4V\r>",
    "ATDP": b"ISO 15765-4 (CAN 11/500)\r>",
    "ATDPN": b"A6\r>",
    "0100": b"41 00 BE 3F A8 13\r>",
    "010C": b"41 0C 1A F8\r>",
    "0142": TWO_ECU_VOLTAGE,
    "03": b"43 00\r>",
    # 0600 advertises MIDs 01 and 02 and no further bank; 0620 answers only
    # because the table is permissive, and must never be asked for.
    "0600": b"18DAF110 06 46 00 C0 00 00 00\r>",
    "0620": b"18DAF110 06 46 20 80 00 00 00\r>",
    "0601": b"18DAF110 0A 46 01 8C 24 01 90 00 00 03 E8\r>",
    "020C00": b"18DAF110 05 42 0C 00 1A F8\r>",
    "ATCRA18DAF110": b"OK\r>",
    "ATCRA18DAF145": b"OK\r>",
    RECEIVE_FILTER_CLEAR: b"OK\r>",
    "090A": b"18DAF110 07 49 0A 01 45 43 4D\r>",
}


class FilteringTransport(ScriptedTransport):
    """Answers 090A as whichever module the CAN receive filter has selected.

    A transport that ignored the filter would answer every module's name with
    the same module's name, and a session that set no filter -- or set the wrong
    one -- would still produce a full-looking map.  That is precisely the bug
    the filter exists to prevent, so the fixture has to model it.
    """

    def __init__(self):
        super().__init__(dict(TABLE))
        self.receive_filter = ""

    def send(self, command, timeout=None):
        if command.startswith("ATCRA"):
            self.receive_filter = command[-2:]
        elif command == RECEIVE_FILTER_CLEAR:
            self.receive_filter = ""
        self.table["090A"] = ECU_NAME_REPLIES.get(self.receive_filter, b"NO DATA\r\r>")
        return super().send(command, timeout=timeout)


class TestSession(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.log = RawLog(self.dir / "raw.jsonl", "s")
        self.transport = ScriptedTransport(TABLE)
        self.session = AdapterSession(self.transport)

    def tearDown(self):
        self.log.close()

    def test_initialize_records_the_fingerprint(self):
        fp = self.session.initialize()
        self.assertIn("OBDLink MX+", fp.adapter_id)
        self.assertIn("STN2255", fp.stn_version)
        self.assertEqual(fp.voltage, "12.4V")

    def test_protocol_negotiation(self):
        self.session.initialize()
        fp = self.session.negotiate_protocol()
        self.assertIn("CAN", fp.protocol)
        self.assertEqual(fp.protocol_number, "A6")

    def test_every_command_sent_is_on_the_allowlist(self):
        self.session.initialize()
        self.session.negotiate_protocol()
        self.session.supported_service01_pids()
        self.session.read_pid("0C")
        self.session.read_dtcs("03")
        self.session.read_vin()
        self.session.supported_monitor_mids()
        self.session.read_monitor_tests("01")
        self.session.read_freeze_frame("0C")
        self.session.read_pid_per_ecu("42")
        self.session.ecu_name_map(["18DAF110"])
        for command in self.transport.sent:
            with self.subTest(command=command):
                self.assertEqual(validate_command(command), command)
        self.assertNotIn("04", self.transport.sent)

    def test_sleeping_vehicle_returns_no_data_without_retry_storm(self):
        transport = ScriptedTransport({})  # everything answers NO DATA
        session = AdapterSession(transport)
        value, reply = session.read_pid("0C")
        self.assertEqual(reply.status, "no_data")
        self.assertIsNone(value.value)
        self.assertEqual(len(transport.sent), 1)


class TestEverythingTheVehicleAdvertises(unittest.TestCase):
    """The reads that only exist to stop one module speaking for the truck."""

    def setUp(self):
        self.transport = ScriptedTransport(TABLE)
        self.session = AdapterSession(self.transport)

    def test_monitor_id_banks_are_walked_only_as_far_as_they_point(self):
        mids = self.session.supported_monitor_mids()
        self.assertEqual(mids, ["01", "02"])
        # 0600 does not advertise MID 20, so bank 20 is never requested -- even
        # though this table would happily answer it.
        self.assertEqual(self.transport.sent, ["0600"])

    def test_monitor_tests_are_requested_by_monitor_id(self):
        tests, reply = self.session.read_monitor_tests("01")
        self.assertEqual(self.transport.sent, ["0601"])
        self.assertEqual(reply.status, "ok")
        self.assertEqual([test.mid for test in tests], [0x01])
        self.assertEqual([test.ecu for test in tests], ["10"])

    def test_a_freeze_frame_request_names_the_pid_and_the_frame(self):
        value, reply = self.session.read_freeze_frame("0C")
        self.assertEqual(self.transport.sent, ["020C00"])
        self.assertEqual(reply.status, "ok")
        self.assertEqual(value.value, 1726.0)
        self.assertEqual(value.ecu, "10")

    def test_every_module_that_answers_a_pid_is_returned(self):
        values, reply = self.session.read_pid_per_ecu("42")
        # One request, as before: the difference is how many answers are kept.
        self.assertEqual(self.transport.sent, ["0142"])
        self.assertEqual([v.ecu for v in values], ["10", "45"])
        self.assertEqual([v.value for v in values], [13.216, 13.2])
        self.assertEqual(reply.status, "ok")

    def test_a_silent_pid_still_produces_one_reading(self):
        # "Nobody answered" has to reach the caller in the shape of a reading,
        # not as an empty list every call site has to remember to check.
        values, reply = self.session.read_pid_per_ecu("0D")
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].status, "no_data")
        self.assertIsNone(values[0].value)

    def test_each_module_is_named_behind_its_own_receive_filter(self):
        transport = FilteringTransport()
        session = AdapterSession(transport)
        names = session.ecu_name_map(["18DAF110", "45"])
        self.assertEqual(names, {"10": "ECM", "45": "BCM"})
        self.assertEqual(transport.sent, [
            "ATCRA18DAF110", "090A",
            "ATCRA18DAF145", "090A",
            RECEIVE_FILTER_CLEAR,
        ])
        # And the adapter is left hearing every module again.
        self.assertEqual(transport.receive_filter, "")

    def test_a_name_is_left_blank_when_the_filter_did_not_take_effect(self):
        # An adapter that ignored ATCRA answers with every module at once.  The
        # first of those names is not this address's name, and guessing would
        # put an untrue module name in the record.
        table = dict(TABLE)
        table["090A"] = (b"18DAF110 07 49 0A 01 45 43 4D\r"
                         b"18DAF145 07 49 0A 01 42 43 4D\r>")
        session = AdapterSession(ScriptedTransport(table))
        self.assertEqual(session.ecu_name_map(["10"]), {"10": ""})

    def test_an_eleven_bit_identifier_is_skipped_rather_than_mangled(self):
        # ATCRA18DAF17E8 would be nine hex digits and the gate would refuse it.
        # Skipping is the honest outcome; building it and catching the refusal
        # would mean the session had tried to transmit something unsafe.
        self.assertEqual(self.session.ecu_name_map(["7E8"]), {})
        self.assertEqual(self.transport.sent, [])

    def test_the_receive_filter_is_cleared_even_when_the_request_fails(self):
        transport = ScriptedTransport(TABLE, fail_on={"090A"})
        session = AdapterSession(transport)
        with self.assertRaises(TransportError):
            session.ecu_name_map(["10"])
        # A filter left set would make every later request look like a
        # one-module vehicle, and nothing downstream reports the difference.
        self.assertEqual(transport.sent[-1], RECEIVE_FILTER_CLEAR)

    def test_no_new_read_transmits_anything_the_gate_would_refuse(self):
        self.session.supported_monitor_mids()
        self.session.read_monitor_tests("01")
        self.session.read_freeze_frame("0C", frame=1)
        self.session.read_pid_per_ecu("42")
        self.session.ecu_name_map(["18DAF110"])
        for command in self.transport.sent:
            with self.subTest(command=command):
                self.assertEqual(validate_command(command), command)
        self.assertNotIn("04", self.transport.sent)
        self.assertEqual([c for c in self.transport.sent if c.startswith("22")], [])


if __name__ == "__main__":
    unittest.main()
