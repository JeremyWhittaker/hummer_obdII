"""Exercise reserve shutdown, power-return races and late-hook admission offline."""

import argparse
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from hummer_obd import pisugar3_power as P


class Bus:
    def __init__(self, percent=84, volts=3.866, control=0x7C):
        mv = round(volts * 1000)
        self.registers = {P.CTR1: control, 0x22: mv >> 8, 0x23: mv & 255,
                          0x2A: percent, 0x09: 42}
        self.registers.update({0xE2 + i: c for i, c in enumerate(b"v1.3.4".ljust(15, b"\0"))})
        self.writes = []

    def read(self, register):
        return self.registers[register]

    def protected_write(self, register, value):
        self.writes.append((register, value))
        self.registers[register] = value


class StateTests(unittest.TestCase):
    def test_measured_board_reports_charge_and_external_power_separately(self):
        version, state = P.identify(Bus())
        self.assertEqual(version, "v1.3.4")
        self.assertEqual(state.percent, 84)
        self.assertAlmostEqual(state.volts, 3.866)
        self.assertFalse(state.plugged)
        self.assertTrue(state.restore)
        self.assertTrue(P.read_state(Bus(control=0xFC)).plugged)

    def test_bad_bus_and_unsupported_firmware_are_refused(self):
        for kwargs in ({"percent": 255}, {"volts": 0.771}, {"control": 0x03}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                P.identify(Bus(**kwargs))
        bus = Bus()
        bus.registers[0xE2 + 5] = ord("3")  # v1.3.3 is supported, v1.1.3 is not.
        bus.registers[0xE2 + 3] = ord("1")
        with self.assertRaises(ValueError):
            P.identify(bus)

    def test_input_transition_during_sample_does_not_become_a_low_reading(self):
        bus = Bus(percent=5)
        read = bus.read
        controls = iter([0x7C, 0xFC])
        bus.read = lambda reg: next(controls) if reg == P.CTR1 else read(reg)
        with self.assertRaisesRegex(ValueError, "changed"):
            P.read_state(bus)

    def test_restore_is_idempotent_and_preserves_other_control_bits(self):
        bus = Bus(control=0xEC)
        P.ensure_restore(bus)
        self.assertEqual(bus.writes, [(0x02, 0xFC)])
        P.ensure_restore(bus)
        self.assertEqual(len(bus.writes), 1)


class ReserveTests(unittest.TestCase):
    def test_threshold_is_inclusive_and_requires_three_low_samples(self):
        reserve = P.Reserve()
        state = P.read_state(Bus(percent=10, volts=3.65))
        self.assertEqual([reserve.evaluate(state) for _ in range(3)], [False, False, True])

    def test_input_recovery_invalid_read_or_lost_restore_breaks_low_streak(self):
        for interruption in (None, P.read_state(Bus(percent=9, control=0xFC)),
                             P.read_state(Bus(percent=9, control=0x6C)),
                             P.read_state(Bus(percent=11))):
            with self.subTest(interruption=interruption):
                reserve = P.Reserve()
                low = P.read_state(Bus(percent=9))
                reserve.evaluate(low)
                reserve.evaluate(low)
                self.assertFalse(reserve.evaluate(interruption))
                self.assertFalse(reserve.evaluate(low))

    def test_power_present_never_triggers_even_with_empty_low_voltage_cell(self):
        reserve = P.Reserve()
        low = P.read_state(Bus(percent=0, volts=3.2, control=0xFC))
        self.assertFalse(any(reserve.evaluate(low) for _ in range(100)))

    def test_emergency_voltage_floor_protects_against_optimistic_percentage(self):
        reserve = P.Reserve()
        state = P.read_state(Bus(percent=50, volts=3.39))
        self.assertEqual([reserve.evaluate(state) for _ in range(3)], [False, False, True])

    def test_configuration_cannot_erase_the_requested_reserve_or_debounce(self):
        for kwargs in ({"percent": 0}, {"percent": 11}, {"consecutive": 1},
                       {"voltage": float("nan")}, {"voltage": 3.0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                P.Reserve(**kwargs)

    def test_power_return_before_shutdown_cancels_the_request(self):
        reserve = P.Reserve()
        reserve.streak = 3
        with patch.object(P.subprocess, "run") as run:
            self.assertFalse(P.request_shutdown(Bus(percent=9, control=0xFC), reserve))
        run.assert_not_called()

    def test_failed_systemctl_is_detected_and_does_not_leave_a_shutdown_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "marker"
            reserve = P.Reserve()
            reserve.streak = 3
            with patch.object(P, "MARKER", marker), patch.object(
                    P.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "systemctl")):
                with self.assertRaises(subprocess.CalledProcessError):
                    P.request_shutdown(Bus(percent=9), reserve)
            self.assertFalse(marker.exists())

    def test_successful_request_marks_shutdown_and_uses_normal_systemd_poweroff(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "marker"
            reserve = P.Reserve()
            reserve.streak = 3
            with patch.object(P, "MARKER", marker), patch.object(P.subprocess, "run") as run:
                self.assertTrue(P.request_shutdown(Bus(percent=9), reserve))
            self.assertTrue(marker.exists())
            run.assert_called_once_with(["/usr/bin/systemctl", "poweroff"], check=True)

    def test_dry_run_neither_requests_poweroff_nor_writes_to_the_board(self):
        bus = Bus(percent=9)
        args = argparse.Namespace(reserve_percent=10, voltage_floor=3.4,
                                  consecutive=3, interval_s=0.01, dry_run=True)
        # Four loops including the threshold, then SIGTERM through its handler.
        handlers = {}
        cycles = 0

        def sleep(_):
            nonlocal cycles
            cycles += 1
            if cycles >= 4:
                handlers[P.signal.SIGTERM]()

        with patch.object(P.signal, "signal", side_effect=lambda s, f: handlers.update({s: f})), \
                patch.object(P.time, "sleep", side_effect=sleep), \
                patch.object(P.time, "monotonic", side_effect=range(100)), \
                patch.object(P, "request_shutdown") as request:
            # Larger interval allows the fake clock to enter the sleep branch.
            args.interval_s = 2
            P.watch(bus, args)
        request.assert_not_called()
        self.assertEqual(bus.writes, [])


class LateHookTests(unittest.TestCase):
    def test_transient_bus_error_is_retried_but_permanent_failure_is_bounded(self):
        for failures in (1, 99):
            with self.subTest(failures=failures), \
                    patch.object(P.os, "geteuid", return_value=0), \
                    patch.object(P.Path, "resolve", return_value=Path("/usr/lib/systemd/systemd-shutdown")), \
                    patch.object(P.os, "statvfs", return_value=argparse.Namespace(f_flag=P.os.ST_RDONLY)), \
                    patch.object(P.time, "sleep"), \
                    patch.object(P, "finish_poweroff", side_effect=[OSError("transient")] * failures + [0]) as finish:
                if failures == 1:
                    self.assertEqual(P.late_shutdown(Bus(), "poweroff"), 0)
                    self.assertEqual(finish.call_count, 2)
                else:
                    with self.assertRaises(OSError):
                        P.late_shutdown(Bus(), "poweroff")
                    self.assertEqual(finish.call_count, 5)

    def test_reboot_and_kexec_do_not_even_open_the_bus(self):
        with patch.object(P, "I2C") as i2c:
            self.assertEqual(P.main(["reboot"]), 0)
            self.assertEqual(P.main(["kexec"]), 0)
        i2c.assert_not_called()

    def test_manual_invocation_cannot_cut_a_running_pi(self):
        bus = Bus()
        with patch.object(P.os, "geteuid", return_value=0), \
                patch.object(P.Path, "resolve", return_value=Path("/usr/lib/systemd/systemd")):
            with self.assertRaisesRegex(RuntimeError, "late systemd shutdown"):
                P.late_shutdown(bus, "poweroff")
        self.assertEqual(bus.writes, [])

    def test_writable_root_prevents_output_cut_even_in_shutdown_phase(self):
        bus = Bus()
        with patch.object(P.os, "geteuid", return_value=0), \
                patch.object(P.Path, "resolve", return_value=Path("/usr/lib/systemd/systemd-shutdown")), \
                patch.object(P.os, "statvfs", return_value=argparse.Namespace(f_flag=0)):
            with self.assertRaises(RuntimeError):
                P.late_shutdown(bus, "poweroff")
        self.assertEqual(bus.writes, [])

    def test_final_cut_zeros_delay_and_retains_restore_and_unrelated_bits(self):
        bus = Bus(control=0x6C)  # restore absent; must be set before cutting.
        with patch.object(P.os, "geteuid", return_value=0), \
                patch.object(P.Path, "resolve", return_value=Path("/usr/lib/systemd/systemd-shutdown")), \
                patch.object(P.os, "statvfs", return_value=argparse.Namespace(f_flag=P.os.ST_RDONLY)), \
                patch.object(P.os, "sync") as sync:
            self.assertEqual(P.late_shutdown(bus, "poweroff"), 0)
        sync.assert_called_once()
        self.assertEqual(bus.writes, [(0x02, 0x7C), (0x09, 0), (0x02, 0x5C)])

    def test_power_return_during_automatic_shutdown_reboots_without_cutting(self):
        bus = Bus(control=0xFC)
        with patch.object(P.os, "geteuid", return_value=0), \
                patch.object(P.Path, "resolve", return_value=Path("/usr/lib/systemd/systemd-shutdown")), \
                patch.object(P.os, "statvfs", return_value=argparse.Namespace(f_flag=P.os.ST_RDONLY)), \
                patch.object(P.Path, "exists", return_value=True), \
                patch.object(P.subprocess, "run") as run:
            P.late_shutdown(bus, "poweroff")
        self.assertEqual(bus.writes, [])
        run.assert_called_once_with(["/usr/bin/systemctl", "reboot", "--force", "--force"], check=True)

    def test_input_returning_at_the_last_control_read_also_prevents_output_cut(self):
        bus = Bus()
        write = bus.protected_write

        def protected_write(register, value):
            write(register, value)
            if register == 0x09:
                bus.registers[P.CTR1] |= P.PLUGGED

        bus.protected_write = protected_write
        with patch.object(P.Path, "exists", return_value=True), \
                patch.object(P.os, "sync"), patch.object(P.subprocess, "run") as run:
            P.finish_poweroff(bus)
        self.assertEqual(bus.writes, [(0x09, 0)])
        run.assert_called_once_with(["/usr/bin/systemctl", "reboot", "--force", "--force"], check=True)

    def test_failed_protected_write_still_relocks_the_chip(self):
        bus = object.__new__(P.I2C)
        calls = []

        def write(register, value):
            calls.append((register, value))
            if register == P.CTR1:
                raise OSError("bus failed")

        bus.write = write
        with self.assertRaises(OSError):
            bus.protected_write(P.CTR1, 0xFC)
        self.assertEqual(calls, [(0x0B, 0x29), (0x02, 0xFC), (0x0B, 0)])


if __name__ == "__main__":
    unittest.main()
