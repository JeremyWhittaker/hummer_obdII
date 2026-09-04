"""Passive capture: what is transmitted, and how we know that is all of it.

Every other transport test in this repo asserts against a fake's list of
*command names*. That is the right check for request/response, and by
construction it cannot catch a write that bypassed ``log_tx`` -- the fake would
record the bytes, the raw log would not, and both lists would still look
plausible. A tool whose entire claim is "nothing reaches the vehicle" has to be
checked the other way round, against the bytes themselves.

So the load-bearing assertion here is
:meth:`TestNothingIsTransmittedUnlogged.test_the_raw_log_equals_what_was_written`:
the concatenation of the raw log's ``tx`` records must equal the concatenation
of everything the fake serial port was handed, byte for byte. If those agree,
the transcript is the transmission record rather than a summary of it.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hummer_obd
from hummer_obd import monitor
from hummer_obd.monitor import (
    MAX_CAPTURE_BYTES,
    MAX_CAPTURE_SECONDS,
    MONITOR_COMMANDS,
    STOP_CHARACTER,
    MonitorTransport,
    assert_no_vehicle_traffic,
)
from hummer_obd.rawlog import RawLog, decode_record, iter_records
from hummer_obd.safety import (
    MONITOR_CAN_MODE,
    MONITOR_STREAM_COMMAND,
    UnsafeCommandError,
)
from hummer_obd.transport import SerialTransport, TransportError


class Clock:
    """A monotonic clock that advances a fixed step per reading."""

    def __init__(self, step=0.1):
        self.t = 0.0
        self.step = step

    def __call__(self):
        now = self.t
        self.t += self.step
        return now


class FakeSerial:
    """A serial port that can stream, and that remembers every byte handed over.

    ``handed`` is the point: it records what ``read`` actually returned, so a
    test can assert the raw log's ``rx`` records account for all of it. Bytes
    that reached the program and not the transcript are exactly the failure the
    raw log exists to make impossible.
    """

    def __init__(self, *args, **kwargs):
        self.is_open = True
        self.written = []
        self.handed = bytearray()
        self.buffer = bytearray()
        self.stream = b""
        self.drip = False          # hide in_waiting: bytes arrive one at a time
        self.reset_calls = 0
        self.reads = 0
        self.fail_read_after = None

    def reset_input_buffer(self):
        self.reset_calls += 1
        self.buffer.clear()

    def write(self, data):
        data = bytes(data)
        self.written.append(data)
        if data == (MONITOR_STREAM_COMMAND + "\r").encode():
            self.buffer += self.stream
        else:
            self.buffer += b">"
        return len(data)

    def flush(self):
        pass

    @property
    def in_waiting(self):
        return 0 if self.drip else len(self.buffer)

    def read(self, size):
        self.reads += 1
        if self.fail_read_after is not None and self.reads > self.fail_read_after:
            raise OSError("link down")
        chunk = bytes(self.buffer[:size])
        del self.buffer[:size]
        self.handed += chunk
        return chunk

    def close(self):
        self.is_open = False


class FakeSerialModule:
    def __init__(self):
        self.instance = FakeSerial()

    def Serial(self, *args, **kwargs):
        return self.instance


class MonitorCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "raw.jsonl"
        self.rawlog = RawLog(self.path, "monitor-test", fsync=False)
        self.module = FakeSerialModule()
        self.fake = self.module.instance
        self.clock = Clock()
        self.transport = MonitorTransport(
            "/dev/null", self.rawlog, serial_module=self.module,
            command_timeout_s=1.0, clock=self.clock,
        )
        self.transport.open()

    def tearDown(self):
        self.transport.close()
        self.rawlog.close()

    def records(self, direction):
        return [r for r in iter_records(self.path)
                if r.get("kind") == "io" and r.get("dir") == direction]

    def joined(self, direction):
        return b"".join(decode_record(r) for r in self.records(direction))

    def events(self, name):
        return [r for r in iter_records(self.path)
                if r.get("kind") == "event" and r.get("event") == name]

    def run_capture(self, **kwargs):
        for command in MONITOR_COMMANDS:
            self.transport.send(command, timeout=1.0)
        kwargs.setdefault("max_seconds", 1.0)
        kwargs.setdefault("max_bytes", 10_000)
        return self.transport.capture(MONITOR_STREAM_COMMAND, **kwargs)


class TestNothingIsTransmittedUnlogged(MonitorCase):
    def test_the_raw_log_equals_what_was_written(self):
        """The assertion this whole module exists to make.

        Not "the fake saw the commands we expected" -- that check passes for a
        write that skipped the transcript entirely. Byte-for-byte equality
        between the port and the log is the only form that does not.
        """
        self.fake.stream = b"18DAF117 03 7F 22 31\r" * 8
        self.run_capture()
        self.transport.send("ATCS", timeout=1.0)
        self.assertEqual(self.joined("tx"), b"".join(self.fake.written))

    def test_every_received_byte_reached_the_transcript(self):
        # The other direction: bytes that got into the program without getting
        # into the log are the failure the raw log exists to prevent.
        self.fake.stream = b"18DAF117 04 62 2B 43 01\r" * 5
        self.run_capture()
        self.assertEqual(self.joined("rx"), bytes(self.fake.handed))

    def test_nothing_transmitted_is_outside_the_declared_manifest(self):
        self.fake.stream = b"x" * 32
        self.run_capture()
        allowed = {(c + "\r").encode() for c in MONITOR_COMMANDS}
        allowed.add((MONITOR_STREAM_COMMAND + "\r").encode())
        allowed.add(STOP_CHARACTER)
        for payload in self.fake.written:
            with self.subTest(payload=payload):
                self.assertIn(payload, allowed)

    def test_the_stop_character_is_a_single_byte_to_the_adapter(self):
        self.run_capture()
        self.assertEqual(self.fake.written[-1], STOP_CHARACTER)
        self.assertEqual(len(STOP_CHARACTER), 1)


class TestTheTwoGatesMakeTheMistakeUnreachable(MonitorCase):
    def test_send_refuses_the_stream_command(self):
        """The failure mode the validation doc warns about, made unreachable.

        ``send`` reads until the adapter's ``>``, which a monitor stream never
        emits: it would block for the full timeout and return truncated bytes
        flagged as a timeout. Building this transport with the *setup*
        validator turns that from a mistake someone must avoid into one the
        object refuses to make.
        """
        with self.assertRaises(UnsafeCommandError):
            self.transport.send(MONITOR_STREAM_COMMAND)
        self.assertEqual(self.fake.written, [])

    def test_capture_refuses_everything_but_the_stream_command(self):
        for command in (MONITOR_CAN_MODE, "ATMA", "STM", "ATRV", "010D"):
            with self.subTest(command=command):
                with self.assertRaises(UnsafeCommandError):
                    self.transport.capture(command, max_seconds=1.0, max_bytes=10)
        self.assertEqual(self.fake.written, [])

    def test_the_setup_gate_still_admits_ordinary_adapter_commands(self):
        self.transport.send("ATRV", timeout=1.0)
        self.assertEqual(self.fake.written, [b"ATRV\r"])

    def test_a_uds_request_is_refused_on_this_transport_too(self):
        for command in ("22F190", "04", "2E1234", "3101FF00"):
            with self.subTest(command=command):
                with self.assertRaises(UnsafeCommandError):
                    self.transport.send(command)
        self.assertEqual(self.fake.written, [])


class TestUnreachableFromTheCollector(unittest.TestCase):
    """Structural, not conventional.

    ``collector.py`` constructs a ``SerialTransport``. If capture lived on that
    class, every object the unattended collector holds could start a stream and
    "the collector cannot monitor" would be a convention rather than a fact.
    """

    def test_the_base_transport_cannot_capture(self):
        self.assertFalse(hasattr(SerialTransport, "capture"))

    def test_importing_the_collector_does_not_import_the_monitor(self):
        root = str(Path(hummer_obd.__file__).resolve().parent.parent)
        env = dict(os.environ)
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-c",
             "import hummer_obd.collector, sys;"
             " print('hummer_obd.monitor' in sys.modules)"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "False")


class TestTheSetupCommandsCannotReachTheBus(unittest.TestCase):
    def test_auto_protocol_detection_is_refused(self):
        # Auto-detection discovers a protocol *by transmitting*.  A tool
        # promising nothing reaches the vehicle cannot auto-detect its way on.
        for command in ("ATSP0", "ATTP0", "STP0"):
            with self.subTest(command=command):
                with self.assertRaises(UnsafeCommandError):
                    assert_no_vehicle_traffic((command,))

    def test_a_request_is_refused_even_though_it_passes_the_gate(self):
        # 010D is perfectly safe and perfectly transmitted.  Passing the gate
        # is necessary here and not sufficient.
        with self.assertRaises(UnsafeCommandError):
            assert_no_vehicle_traffic(("010D",))

    def test_the_shipped_list_pins_the_protocol(self):
        self.assertIn("ATSP7", MONITOR_COMMANDS)      # ISO 15765-4, 29-bit, 500k
        self.assertIn(MONITOR_CAN_MODE, MONITOR_COMMANDS)
        self.assertIn("ATCS", MONITOR_COMMANDS)       # counters, read either side
        self.assertNotIn(MONITOR_STREAM_COMMAND, MONITOR_COMMANDS)

    def test_the_receive_only_mode_is_the_one_that_does_not_acknowledge(self):
        # STCMM1 is a normal node: it asserts the dominant acknowledgement bit
        # on every frame it hears, which is a transmission.
        self.assertEqual(MONITOR_CAN_MODE, "STCMM0")


class TestBounds(MonitorCase):
    def test_a_capture_stops_at_the_byte_limit(self):
        self.fake.stream = b"A" * 500
        result = self.run_capture(max_bytes=64)
        self.assertEqual(result.stop_reason, "byte_limit")
        self.assertTrue(result.hit_byte_bound)
        self.assertGreaterEqual(result.bytes_captured, 64)

    def test_a_capture_stops_at_the_deadline(self):
        result = self.run_capture(max_seconds=0.5)
        self.assertEqual(result.stop_reason, "duration")
        self.assertFalse(result.hit_byte_bound)

    def test_a_caller_can_stop_it(self):
        result = self.run_capture(should_stop=lambda: True)
        self.assertEqual(result.stop_reason, "stopped")

    def test_the_bounds_are_themselves_bounded(self):
        # A bound the caller can set to infinity is not a bound, and this runs
        # against a vehicle.
        for kwargs in ({"max_seconds": 0, "max_bytes": 10},
                       {"max_seconds": -1, "max_bytes": 10},
                       {"max_seconds": MAX_CAPTURE_SECONDS + 1, "max_bytes": 10},
                       {"max_seconds": 1, "max_bytes": 0},
                       {"max_seconds": 1, "max_bytes": MAX_CAPTURE_BYTES + 1}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    self.transport.capture(MONITOR_STREAM_COMMAND, **kwargs)
        self.assertEqual(self.fake.written, [])

    def test_a_closed_transport_refuses_rather_than_writing(self):
        self.transport.close()
        with self.assertRaises(TransportError):
            self.transport.capture(MONITOR_STREAM_COMMAND,
                                   max_seconds=1.0, max_bytes=10)


class TestSilenceIsAResult(MonitorCase):
    def test_a_capture_of_nothing_is_not_an_error(self):
        """The documented negative outcome, not a failure to retry.

        The validation doc's own conclusion: proving we do not transmit says
        nothing about whether there is anything to hear.
        """
        self.fake.stream = b""
        result = self.run_capture()
        self.assertEqual(result.bytes_captured, 0)
        self.assertEqual(result.records_written, 0)
        self.assertEqual(result.stop_reason, "duration")

    def test_the_transcript_records_the_bounds_it_ran_under(self):
        # A zero-byte capture is only readable as evidence if the transcript
        # says how long it listened for.
        self.run_capture(max_seconds=2.0, max_bytes=1234)
        start = self.events("capture_start")[0]["payload"]
        self.assertEqual(start["command"], MONITOR_STREAM_COMMAND)
        self.assertEqual(start["max_seconds"], 2.0)
        self.assertEqual(start["max_bytes"], 1234)
        end = self.events("capture_end")[0]["payload"]
        self.assertEqual(end["bytes"], 0)
        self.assertEqual(end["stop_reason"], "duration")


class TestStreamingBehaviour(MonitorCase):
    def test_bytes_already_waiting_are_recorded_rather_than_discarded(self):
        # send() clears the port before every command, which is right for
        # request/response and would silently drop stream bytes here.
        for command in MONITOR_COMMANDS:
            self.transport.send(command, timeout=1.0)
        self.fake.buffer += b"RESIDUE"
        before = self.fake.reset_calls
        self.transport.capture(MONITOR_STREAM_COMMAND,
                               max_seconds=0.5, max_bytes=1000)
        self.assertEqual(self.fake.reset_calls, before,
                         "capture must not reset the input buffer")
        self.assertIn(b"RESIDUE", self.joined("rx"))

    def test_a_drip_of_bytes_does_not_become_a_record_per_byte(self):
        """The flush schedule is a deadline, not "elapsed >= interval".

        The latter is true on every iteration once the first interval passes,
        which writes one raw-log record -- and, with fsync on, one fsync -- for
        every single byte. The capture would then be measuring its own logging.
        """
        self.fake.drip = True
        self.clock.step = 0.01
        self.fake.stream = b"Z" * 40
        result = self.run_capture(max_seconds=2.0, flush_interval_s=0.25)
        self.assertEqual(result.bytes_captured, 40)
        self.assertLess(result.records_written, 8)
        self.assertGreater(result.records_written, 0)

    def test_bytes_that_arrived_before_a_read_error_still_reach_the_log(self):
        """Not "we kept the stream" -- "we kept everything we were handed".

        The link drops between the one-byte read and the drain of whatever else
        is waiting. Banking each read into the pending buffer before attempting
        the next is what makes that byte survivable; building one chunk across
        both and appending afterwards drops it, and it had already reached the
        program.
        """
        for command in MONITOR_COMMANDS:
            self.transport.send(command, timeout=1.0)
        self.fake.stream = b"KEPT"
        self.fake.fail_read_after = self.fake.reads + 1
        with self.assertRaises(TransportError):
            self.transport.capture(MONITOR_STREAM_COMMAND,
                                   max_seconds=1.0, max_bytes=1000)
        self.assertEqual(self.joined("rx"), bytes(self.fake.handed))
        self.assertTrue(self.joined("rx").endswith(b"K"))
        self.assertTrue(self.events("capture_read_failed"))

    def test_the_adapter_prompt_after_stopping_is_recorded(self):
        result = self.run_capture()
        self.assertTrue(result.stop_acknowledged)
        self.assertIn(b">", self.joined("rx"))


class TestCli(unittest.TestCase):
    def test_a_dry_run_transmits_nothing_and_needs_no_pyserial(self):
        # serial mapped to None makes any `import serial` raise, so this fails
        # loudly if the dry run ever reaches the transport.
        with mock.patch.dict(sys.modules, {"serial": None}):
            with mock.patch("sys.stdout") as out:
                self.assertEqual(monitor.main([]), 0)
        printed = " ".join(str(c) for c in out.write.call_args_list)
        self.assertIn("DRY RUN", printed)
        self.assertIn(MONITOR_STREAM_COMMAND, printed)

    def test_confirm_is_required_to_open_the_device(self):
        parser_default = monitor.main(["--seconds", "5"])
        self.assertEqual(parser_default, 0)  # still a dry run


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
