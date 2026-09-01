"""The safety gate is the most important code in this project."""

import unittest

from hummer_obd import safety
from hummer_obd.safety import UnsafeCommandError, validate_command, is_safe


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


if __name__ == "__main__":
    unittest.main()
