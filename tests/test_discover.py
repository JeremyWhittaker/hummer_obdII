"""Per-module support census.

The property that matters: this asks only the questions the standard defines
for asking, and it must never become a sweep. Everything it sends is a support
bitmap or an addressing command.
"""

import unittest

from hummer_obd import discover
from hummer_obd.discover import MODULES, ModuleReport, census, format_census
from hummer_obd.safety import validate_command
from hummer_obd.session import SUPPORT_MIDS_06, SUPPORT_PIDS_01
from hummer_obd.transport import Response, Transport, TransportError


class _Fake(Transport):
    """Answers support bitmaps per module, so each can differ."""

    def __init__(self, bitmaps=None, silent=()):
        self.sent: list[str] = []
        self.bitmaps = bitmaps or {}
        self.silent = set(silent)
        self.current = None

    def open(self):  # pragma: no cover
        pass

    def close(self):  # pragma: no cover
        pass

    def send(self, command, timeout=None):
        self.sent.append(command)
        if command.startswith("ATSHDA"):
            self.current = command[6:8]
        if self.current in self.silent:
            return Response(command=command, data=b"NO DATA\r\r>", elapsed_s=0.01)
        data = self.bitmaps.get((self.current, command), "NO DATA\r\r>")
        return Response(command=command, data=data.encode("ascii"), elapsed_s=0.01)


class TestItOnlyAsksWhatTheStandardDefines(unittest.TestCase):
    """The line between discovery and sweeping."""

    def test_every_command_it_can_send_passes_the_read_only_gate(self):
        commands = list(SUPPORT_PIDS_01) + list(SUPPORT_MIDS_06) + ["0900"]
        for address, _name in MODULES:
            commands.extend(discover._address_commands(address))
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(validate_command(command), command)

    def test_it_sends_no_vendor_identifier(self):
        fake = _Fake()
        census(fake)
        for command in fake.sent:
            with self.subTest(command=command):
                # Service 22 is the vendor space.  Nothing here may touch it.
                self.assertFalse(
                    command.startswith("22"),
                    f"a support census must not send vendor identifiers: {command}",
                )

    def test_it_only_reads_bitmaps_never_values(self):
        fake = _Fake()
        census(fake)
        for command in fake.sent:
            if command.startswith("AT"):
                continue
            with self.subTest(command=command):
                self.assertIn(
                    command, set(SUPPORT_PIDS_01) | set(SUPPORT_MIDS_06) | {"0900"},
                    f"{command} is not a support bitmap",
                )

    def test_the_module_list_is_what_the_vehicle_named(self):
        addresses = {a for a, _ in MODULES}
        self.assertEqual(
            addresses, {"17", "1D", "1E", "28", "40", "45", "CB", "CD"},
            "the census must only address modules this vehicle named for itself",
        )


class TestBankWalking(unittest.TestCase):
    def test_it_stops_when_a_bitmap_does_not_point_onward(self):
        # 0100 advertises a few PIDs but NOT bank 20, so 0120 must never be sent.
        fake = _Fake(bitmaps={("17", "0100"): "18DAF117064100080000\r\r>"})
        census(fake, modules=(("17", "DMCM"),))
        self.assertIn("0100", fake.sent)
        self.assertNotIn(
            "0120", fake.sent,
            "asking for a bank the vehicle did not advertise puts a pointless "
            "request on a live bus",
        )

    def test_a_silent_module_costs_one_request_per_chain(self):
        fake = _Fake(silent={"40"})
        reports = census(fake, modules=(("40", "BCM"),))
        self.assertFalse(reports[0].answered)
        asked = [c for c in fake.sent if not c.startswith("AT")]
        self.assertLessEqual(
            len(asked), 3,
            f"a silent module should not be asked every bank, sent {asked}",
        )

    def test_a_transport_error_is_recorded_not_raised(self):
        class _Dead(_Fake):
            def send(self, command, timeout=None):
                self.sent.append(command)
                raise TransportError("link gone")

        reports = census(_Dead(), modules=(("CB", "BSM"),))
        self.assertFalse(reports[0].answered)
        self.assertTrue(reports[0].errors)


class TestReporting(unittest.TestCase):
    def test_bitmap_pointers_are_not_reported_as_readings(self):
        report = ModuleReport("17", "DMCM", service01=["00", "0C", "0D", "20", "A6"])
        self.assertEqual(report.readable_pids, ["0C", "0D", "A6"])

    def test_the_census_names_what_only_one_module_supports(self):
        # The comparison a functional broadcast can never make.
        reports = [
            ModuleReport("17", "DMCM", service01=["0D", "A6"], answered=True),
            ModuleReport("28", "BSCM", service01=["0D", "31"], answered=True),
        ]
        text = format_census(reports)
        self.assertIn("only 17 supports: A6", text)
        self.assertIn("only 28 supports: 31", text)
        self.assertIn("PIDs every answering module supports: 0D", text)

    def test_silent_modules_are_listed_rather_than_omitted(self):
        reports = [
            ModuleReport("CB", "BSM", service01=["0D"], answered=True),
            ModuleReport("40", "BCM", answered=False),
        ]
        text = format_census(reports)
        self.assertIn("silent", text)
        self.assertIn("40", text)

    def test_an_all_silent_census_does_not_raise(self):
        reports = [ModuleReport("40", "BCM"), ModuleReport("45", "GWM")]
        self.assertIn("silent", format_census(reports))


class TestTheDryRunTransmitsNothing(unittest.TestCase):
    def test_without_confirm_it_opens_nothing(self):
        self.assertEqual(discover.main([]), 0)

    def test_the_module_imports_no_serial_library_at_import_time(self):
        # SerialTransport is imported inside main() so a dry run needs no
        # serial library present at all.
        source = open(discover.__file__, encoding="utf-8").read()
        header = source.split("def main(")[0]
        self.assertNotIn("import serial", header)
        self.assertNotIn("SerialTransport", header)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
