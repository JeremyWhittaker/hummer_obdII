"""Transport tests: the gate runs before I/O, and raw bytes are always logged."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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


class TestReconnectBackoffIsInterruptible(unittest.TestCase):
    """The backoff wait is the longest sleep in a collector cycle.

    ``reconnect_max_s`` defaults to 120 s.  If that wait were a bare
    ``time.sleep`` a time-boxed trial could overshoot by two minutes and a
    stop request would look ignored for the same window, which is exactly what
    the collector's sliced wait exists to prevent everywhere else.
    """

    def _transport(self, sleeper=None):
        rawlog = mock.MagicMock()
        serial_module = mock.MagicMock()
        return SerialTransport(
            "/dev/null-device",
            rawlog,
            reconnect_initial_s=90.0,
            reconnect_max_s=120.0,
            serial_module=serial_module,
            sleeper=sleeper,
        )

    def test_the_backoff_wait_goes_through_the_injected_sleeper(self):
        waited = []
        transport = self._transport(sleeper=waited.append)
        with mock.patch.object(time, "sleep", side_effect=AssertionError("bare sleep")):
            transport.reconnect(attempt=1)
        self.assertEqual(waited, [90.0])

    def test_an_injected_sleeper_can_cut_the_wait_short(self):
        # A caller that is out of time returns immediately instead of waiting
        # out the full backoff.
        transport = self._transport(sleeper=lambda seconds: None)
        started = time.monotonic()
        transport.reconnect(attempt=3)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_the_default_is_still_plain_time_sleep(self):
        transport = self._transport()
        with mock.patch.object(time, "sleep") as slept:
            transport.reconnect(attempt=0)
        slept.assert_called_once_with(90.0)



if __name__ == "__main__":
    unittest.main()
