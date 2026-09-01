"""Session tests run the real command sequence against a scripted adapter."""

import tempfile
import unittest
from pathlib import Path

from hummer_obd.rawlog import RawLog
from hummer_obd.safety import validate_command
from hummer_obd.session import AdapterSession
from hummer_obd.transport import Response


class ScriptedTransport:
    """Answers from a table and records every command it is asked to send."""

    def __init__(self, table):
        self.table = table
        self.sent = []

    def send(self, command, timeout=None):
        command = validate_command(command)  # same gate the real transport uses
        self.sent.append(command)
        data = self.table.get(command, b"NO DATA\r\r>")
        return Response(command=command, data=data, elapsed_s=0.0)

    def open(self):
        pass

    def close(self):
        pass


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
    "03": b"43 00\r>",
}


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


if __name__ == "__main__":
    unittest.main()
