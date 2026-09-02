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
)


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



if __name__ == "__main__":
    unittest.main()
