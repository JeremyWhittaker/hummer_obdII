"""The safety gate is the most important code in this project."""

import unittest

from hummer_obd import safety
from hummer_obd.safety import (
    ALLOWED_OBD_MODES,
    FORBIDDEN_SERVICES,
    UnsafeCommandError,
    describe_command,
    is_safe,
    validate_command,
    validate_scan_command,
)
from hummer_obd.transport import SerialTransport


class TestAllowedCommands(unittest.TestCase):
    def test_read_only_services_are_allowed(self):
        for command in ("0100", "010C", "0105", "03", "07", "0A", "0902", "0904", "090A"):
            with self.subTest(command=command):
                self.assertEqual(validate_command(command), command)

    def test_whitespace_and_case_are_normalised(self):
        self.assertEqual(validate_command("01 0c"), "010C")
        self.assertEqual(validate_command(" atrv "), "ATRV")

    def test_adapter_commands(self):
        for command in (
            "ATZ", "ATE0", "ATH1", "ATAL", "ATSP0", "ATDPN", "ATRV",
            "STI", "STDI", "ATST64",
        ):
            with self.subTest(command=command):
                self.assertTrue(is_safe(command))

    def test_response_count_suffix(self):
        self.assertEqual(validate_command("010C1"), "010C1")


class TestForbiddenCommands(unittest.TestCase):
    def test_mode_04_is_never_allowed(self):
        for command in ("04", "0400", "04 00", "  04  ", "04FF"):
            with self.subTest(command=command):
                with self.assertRaises(UnsafeCommandError):
                    validate_command(command)

    def test_uds_write_control_and_security_services(self):
        for command in ("2E1234", "2701", "2F010301", "3101FF00", "1101", "1001", "85021"[:5], "3E00"):
            with self.subTest(command=command):
                self.assertFalse(is_safe(command))

    def test_mode_08_actuator_service(self):
        self.assertFalse(is_safe("08"))
        self.assertFalse(is_safe("0801"))

    def test_mode_22_is_deferred_in_this_build(self):
        self.assertFalse(is_safe("22ABCD"))

    def test_command_batching_is_rejected(self):
        for command in ("0100\r04", "0100\n04", "0100;04", "0100\x0004"):
            with self.subTest(command=command):
                self.assertFalse(is_safe(command))

    def test_unknown_adapter_commands_are_rejected(self):
        for command in ("ATFOO", "ATBRD", "STWBR", "AT", "ST"):
            with self.subTest(command=command):
                self.assertFalse(is_safe(command))

    def test_malformed_input(self):
        for command in ("", "   ", "01C", "zz", "01" * 40, None):
            with self.subTest(command=command):
                with self.assertRaises(UnsafeCommandError):
                    validate_command(command)

    def test_forbidden_services_are_a_superset_of_the_disallowed_modes(self):
        self.assertIn("04", safety.FORBIDDEN_SERVICES)
        self.assertTrue(safety.FORBIDDEN_SERVICES.isdisjoint(safety.ALLOWED_OBD_MODES))

    def test_every_forbidden_service_is_rejected(self):
        for service in safety.FORBIDDEN_SERVICES:
            with self.subTest(service=service):
                self.assertFalse(is_safe(service))
                self.assertFalse(is_safe(service + "00"))


class TestDescriptions(unittest.TestCase):
    def test_describe(self):
        self.assertIn("current data", safety.describe_command("010C"))
        self.assertIn("stored DTCs", safety.describe_command("03"))
        self.assertIn("adapter command", safety.describe_command("ATI"))


class TestScanGate(unittest.TestCase):
    """The DID scan gets one narrow gate that no ordinary path inherits."""

    def test_every_two_byte_identifier_is_accepted_including_f190(self):
        for did in range(0x10000):
            command = f"22{did:04X}"
            self.assertEqual(validate_scan_command(command), command)
        self.assertEqual(validate_scan_command("22 f1 90"), "22F190")

    def test_scan_gate_accepts_no_payload_or_malformed_identifier(self):
        for command in (
            "22", "2200", "2200000", "22000000", "22F19000", "22GGGG",
            "22-0000", "", "   ", None, 0x220000, b"220000", ["220000"],
        ):
            with self.subTest(command=command):
                with self.assertRaises(UnsafeCommandError):
                    validate_scan_command(command)

    def test_scan_gate_refuses_every_other_service(self):
        for service in range(0x100):
            if service == 0x22:
                continue
            command = f"{service:02X}0000"
            with self.subTest(command=command):
                with self.assertRaises(UnsafeCommandError):
                    validate_scan_command(command)

    def test_forbidden_services_hit_the_independent_barrier(self):
        for service in FORBIDDEN_SERVICES:
            with self.subTest(service=service):
                with self.assertRaisesRegex(
                    UnsafeCommandError, "permanently forbidden"
                ):
                    validate_scan_command(service + "0000")

    def test_forbidden_services_reach_zero_transport_writes(self):
        class Wire:
            is_open = True

            def __init__(self):
                self.writes = []

            def write(self, payload):
                self.writes.append(payload)

        class Log:
            def log_tx(self, *args, **kwargs):
                raise AssertionError("a refused command must not reach the raw log")

            def log_rx(self, *args, **kwargs):
                raise AssertionError("a refused command must not be read")

            def write_event(self, *args, **kwargs):
                raise AssertionError("a refused command must not reach I/O")

        wire = Wire()
        transport = SerialTransport(
            "/dev/null", Log(), serial_module=object(),
            validator=validate_scan_command,
        )
        transport._serial = wire
        for service in FORBIDDEN_SERVICES:
            with self.subTest(service=service):
                with self.assertRaises(UnsafeCommandError):
                    transport.send(service + "0000")
        self.assertEqual(wire.writes, [])

    def test_speed_and_dtc_reads_require_the_ordinary_gate(self):
        for command in ("010D", "03", "07", "0A"):
            with self.subTest(command=command):
                self.assertEqual(validate_command(command), command)
                with self.assertRaises(UnsafeCommandError):
                    validate_scan_command(command)

    def test_batching_is_refused(self):
        for command in (
            "220000;04", "220000\r04", "220000\n03", "220000\x00",
            "220000;22FFFF", "220000\r\n22FFFF",
        ):
            with self.subTest(command=command):
                with self.assertRaises(UnsafeCommandError):
                    validate_scan_command(command)

    def test_adapter_commands_delegate_to_the_ordinary_gate(self):
        for command in ("ATZ", "ATRV", "ATSP7", "ATCP14", "ATFCSM1"):
            with self.subTest(command=command):
                self.assertEqual(
                    validate_scan_command(command), validate_command(command)
                )
        for command in ("ATMA", "ATFOO", "STMA", "STCMM0"):
            with self.subTest(command=command):
                with self.assertRaises(UnsafeCommandError):
                    validate_scan_command(command)

    def test_ordinary_and_existing_supervised_gates_do_not_gain_scan_access(self):
        self.assertNotIn("22", ALLOWED_OBD_MODES)
        with self.assertRaises(UnsafeCommandError):
            validate_command("2200FF")
        with self.assertRaises(UnsafeCommandError):
            safety.validate_enhanced_command("22F190")
        with self.assertRaises(UnsafeCommandError):
            safety.validate_supervised_command("22F190")


class TestFreezeFrameAndMonitorServices(unittest.TestCase):
    """Services 02 and 06, added 2026-09-01.

    Both are standard SAE J1979 *read* services, defined by the same
    specification as 01/03/07/09/0A.  Unlike mode 22 they need no vendor
    identifier to be guessed: 02 returns the snapshot an ECU stored alongside a
    DTC, and 06 returns monitor results the ECU computed on its own.  The point
    of these tests is that widening the allowlist did not widen anything else.
    """

    def test_freeze_frame_request_shapes_are_accepted(self):
        for command in ("0200", "0202", "020200", "020201", "02020F"):
            self.assertTrue(is_safe(command), command)

    def test_monitor_test_result_request_shapes_are_accepted(self):
        for command in ("0600", "0601", "0620", "06A1", "06010"):
            self.assertTrue(is_safe(command), command)

    def test_a_bare_service_byte_is_still_not_a_request(self):
        # 02 and 06 both need a parameter; a bare mode byte is malformed and
        # must not be waved through just because the mode is now allowed.
        for command in ("02", "06"):
            with self.assertRaises(UnsafeCommandError):
                validate_command(command)

    def test_over_long_payloads_are_rejected(self):
        for command in ("020000FF", "060102", "0203040506", "02000000000"):
            with self.assertRaises(UnsafeCommandError):
                validate_command(command)

    def test_widening_the_allowlist_did_not_admit_a_forbidden_service(self):
        self.assertEqual(ALLOWED_OBD_MODES & FORBIDDEN_SERVICES, frozenset())
        for command in ("04", "0400", "08", "0800", "22F190", "2E1234",
                        "2701", "3101FF", "1101", "3E00", "14FFFFFF"):
            self.assertFalse(is_safe(command), command)

    def test_batching_is_still_refused_behind_the_new_services(self):
        for command in ("0200;04", "0600\r04", "0202\n0400"):
            with self.assertRaises(UnsafeCommandError):
                validate_command(command)

    def test_the_new_services_are_described_for_the_log(self):
        self.assertIn("freeze frame", describe_command("0202"))
        self.assertIn("monitoring", describe_command("0601"))



class TestTheMonitorGatesAreSeparateGates(unittest.TestCase):
    """Passive capture gets its own two gates, and they stay narrow.

    The obvious implementation -- add the monitor commands to
    ``_ALLOWED_AT_EXACT`` -- is wrong, and not marginally. That set feeds
    :func:`validate_command`, which is the *unattended collector's* gate and the
    default ``SerialTransport`` validator. Widening it would make monitoring
    reachable from a service that runs for hours with nobody watching, to buy
    nothing that two small functions do not.

    Splitting them also buys a structural guarantee. ``MonitorTransport`` is
    built with the *setup* validator, so ``send(STMA)`` raises rather than
    blocking to the full timeout and returning truncated bytes flagged as a
    timeout -- the exact failure ``docs/PASSIVE_CAN_VALIDATION.md`` warns about,
    turned from a mistake to avoid into one the object cannot make.
    """

    MONITOR_COMMANDS = ("STCMM0", "STCMM1", "STCMM2", "STM", "STMA", "ATMA")

    def test_the_production_gate_refuses_every_monitor_command(self):
        for command in self.MONITOR_COMMANDS:
            with self.subTest(command=command):
                with self.assertRaises(UnsafeCommandError):
                    validate_command(command)
                self.assertFalse(is_safe(command))

    def test_the_supervised_gate_refuses_them_too(self):
        # Supervised means a human is watching an enhanced *read*, not that
        # anything goes.
        for command in self.MONITOR_COMMANDS:
            with self.subTest(command=command):
                with self.assertRaises(UnsafeCommandError):
                    safety.validate_supervised_command(command)

    def test_the_setup_gate_adds_exactly_one_command(self):
        added = {c for c in self.MONITOR_COMMANDS
                 if safety.is_safe(c) is False
                 and _accepted(safety.validate_monitor_setup_command, c)}
        self.assertEqual(added, {safety.MONITOR_CAN_MODE})

    def test_the_setup_gate_is_otherwise_the_production_gate(self):
        for command in ("ATZ", "ATRV", "ATSP7", "ATCS", "010D"):
            with self.subTest(command=command):
                self.assertEqual(safety.validate_monitor_setup_command(command),
                                 validate_command(command))
        for command in ("04", "2E1234", "2701", "22F190"):
            with self.subTest(command=command):
                with self.assertRaises(UnsafeCommandError):
                    safety.validate_monitor_setup_command(command)

    def test_the_stream_gate_accepts_nothing_but_the_stream_command(self):
        self.assertEqual(
            safety.validate_monitor_stream_command(safety.MONITOR_STREAM_COMMAND),
            safety.MONITOR_STREAM_COMMAND)
        for command in ("STCMM0", "STM", "ATMA", "ATZ", "ATRV", "010D", "04"):
            with self.subTest(command=command):
                with self.assertRaises(UnsafeCommandError):
                    safety.validate_monitor_stream_command(command)

    def test_the_two_gates_do_not_overlap(self):
        # Neither gate can do the other's job, so a caller cannot start a
        # stream through the configuration path or vice versa.
        with self.assertRaises(UnsafeCommandError):
            safety.validate_monitor_setup_command(safety.MONITOR_STREAM_COMMAND)
        with self.assertRaises(UnsafeCommandError):
            safety.validate_monitor_stream_command(safety.MONITOR_CAN_MODE)

    def test_none_is_refused_rather_than_crashing(self):
        for gate in (safety.validate_monitor_setup_command,
                     safety.validate_monitor_stream_command):
            with self.subTest(gate=gate.__name__):
                with self.assertRaises(UnsafeCommandError):
                    gate(None)

    def test_the_receive_only_mode_is_the_one_that_does_not_acknowledge(self):
        # A CAN node normally asserts a dominant bit in the acknowledgement slot
        # of every frame it hears.  That is a transmission, and STCMM1 does it.
        self.assertEqual(safety.MONITOR_CAN_MODE, "STCMM0")
        with self.assertRaises(UnsafeCommandError):
            safety.validate_monitor_setup_command("STCMM1")


def _accepted(gate, command) -> bool:
    try:
        gate(command)
    except UnsafeCommandError:
        return False
    return True


if __name__ == "__main__":
    unittest.main()
