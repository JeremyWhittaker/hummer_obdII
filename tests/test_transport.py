"""Transport tests: the gate runs before I/O, and raw bytes are always logged."""

import tempfile
import unittest
from pathlib import Path

from hummer_obd.rawlog import RawLog, decode_record, iter_records
from hummer_obd.safety import UnsafeCommandError
from hummer_obd.transport import SerialTransport, TransportError


class FakeSerial:
    """Minimal pyserial stand-in that records writes and replays canned data."""

    def __init__(self, *args, **kwargs):
        self.is_open = True
        self.written = []
        self.replies = []
        self.fail_on_write = False
        self._buffer = b""

    def reset_input_buffer(self):
        pass

    def write(self, data):
        if self.fail_on_write:
            raise OSError("link down")
        self.written.append(data)
        self._buffer = self.replies.pop(0) if self.replies else b">"
        return len(data)

    def flush(self):
        pass

    def read(self, size):
        chunk, self._buffer = self._buffer[:size], self._buffer[size:]
        return chunk

    def close(self):
        self.is_open = False


class FakeSerialModule:
    def __init__(self):
        self.instance = FakeSerial()

    def Serial(self, *args, **kwargs):
        return self.instance


class TestTransport(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.rawlog = RawLog(self.dir / "raw.jsonl", "t")
        self.module = FakeSerialModule()
        self.transport = SerialTransport(
            "/dev/null", self.rawlog, serial_module=self.module, command_timeout_s=1.0
        )
        self.transport.open()

    def tearDown(self):
        self.transport.close()
        self.rawlog.close()

    def test_unsafe_command_never_reaches_the_wire(self):
        for command in ("04", "2E1234", "3101FF", "22ABCD"):
            with self.subTest(command=command):
                with self.assertRaises(UnsafeCommandError):
                    self.transport.send(command)
        self.assertEqual(self.module.instance.written, [])

    def test_safe_command_is_written_with_carriage_return(self):
        self.module.instance.replies = [b"41 0C 1A F8\r\r>"]
        response = self.transport.send("010C")
        self.assertEqual(self.module.instance.written, [b"010C\r"])
        self.assertEqual(response.data, b"41 0C 1A F8\r\r>")
        self.assertFalse(response.timed_out)

    def test_raw_log_contains_exact_tx_and_rx(self):
        self.module.instance.replies = [b"41 0D 40\r\r>"]
        self.transport.send("01 0d")
        records = [r for r in iter_records(self.rawlog.path) if r.get("kind") == "io"]
        self.assertEqual(decode_record(records[0]), b"010D\r")
        self.assertEqual(decode_record(records[1]), b"41 0D 40\r\r>")

    def test_timeout_is_reported_and_logged(self):
        self.module.instance.replies = [b""]  # never sends a prompt
        response = self.transport.send("010C", timeout=0.05)
        self.assertTrue(response.timed_out)
        notes = [r.get("note", "") for r in iter_records(self.rawlog.path)]
        self.assertTrue(any("timeout" in n for n in notes))

    def test_write_failure_raises_transport_error(self):
        self.module.instance.fail_on_write = True
        with self.assertRaises(TransportError):
            self.transport.send("010C")

    def test_send_on_closed_transport(self):
        self.transport.close()
        with self.assertRaises(TransportError):
            self.transport.send("010C")


if __name__ == "__main__":
    unittest.main()
