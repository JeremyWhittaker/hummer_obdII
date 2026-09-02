"""The battery watch can power the node off, so its refusals matter most.

A watch that shuts down when it should not is worse than no watch: the node is
then simply gone until somebody walks out to the vehicle. Nearly all of these
tests are about the cases where it must decline to act.
"""

import unittest

from hummer_obd.battery import (
    I2C_ADDRESS,
    IP5209,
    IP5312,
    BatteryWatch,
    identify_chip,
    read_voltage,
)

#: The bytes actually read from this node's PiSugar2.
REAL = {0xA2: 0x34, 0xA3: 0x15, 0xD0: 0x00, 0xD1: 0x00}


def reader_from(registers):
    def read(address, register):
        assert address == I2C_ADDRESS, f"unexpected i2c address {address:#x}"
        return registers[register]
    return read


def volts_reader(volts, chip=IP5209):
    """A reader that reports *volts* through *chip*'s encoding."""
    count = int(round((volts * 1000.0 - chip.base_mv) / chip.step_mv))
    return reader_from({chip.low_register: count & 0xFF,
                        chip.high_register: (count >> 8) & 0x1F,
                        IP5312.low_register: 0x00, IP5312.high_register: 0x00})


class TestChipIdentification(unittest.TestCase):
    def test_the_real_registers_identify_an_ip5209(self):
        # PiSugar2 carries an IP5209 and PiSugar2 Pro an IP5312, and they
        # report voltage from different registers.  Reading the wrong pair here
        # gives 2.6 V -- below the voltage at which the Pi could have taken the
        # reading at all, which is what makes the identification decidable.
        chip = identify_chip(reader_from(REAL))
        self.assertIsNotNone(chip)
        self.assertEqual(chip.name, "IP5209")
        self.assertAlmostEqual(read_voltage(reader_from(REAL), chip).volts, 4.058, places=3)

    def test_the_wrong_profile_produces_an_implausible_reading(self):
        reading = read_voltage(reader_from(REAL), IP5312)
        self.assertFalse(reading.plausible)
        self.assertAlmostEqual(reading.volts, 2.6, places=3)

    def test_an_ambiguous_identification_is_refused(self):
        # If both profiles read plausibly, neither is trustworthy: everything
        # downstream would be a confident misreading.
        both = {0xA2: 0x34, 0xA3: 0x15, 0xD0: 0x34, 0xD1: 0x15}
        self.assertIsNone(identify_chip(reader_from(both)))

    def test_a_bus_failure_is_not_a_voltage(self):
        def broken(address, register):
            raise OSError("no such device")
        self.assertIsNone(identify_chip(broken))
        reading = read_voltage(broken, IP5209)
        self.assertFalse(reading.plausible)
        self.assertIn("i2c read failed", reading.detail)


class TestItRefusesToActOnBadInput(unittest.TestCase):
    def watch(self, **kw):
        calls = []
        kw.setdefault("reader", volts_reader(4.0))
        kw.setdefault("chip", IP5209)
        kw.setdefault("logger", lambda *_: None)
        return BatteryWatch(shutdown=lambda: calls.append("off"), **kw), calls

    def test_a_healthy_cell_never_triggers(self):
        watch, calls = self.watch(reader=volts_reader(4.0), consecutive=2)
        for _ in range(20):
            watch.evaluate(watch.sample())
        self.assertEqual(calls, [])
        self.assertEqual(watch.low_streak, [])

    def test_one_low_reading_does_nothing(self):
        watch, _ = self.watch(reader=volts_reader(3.2), consecutive=5)
        self.assertIsNone(watch.evaluate(watch.sample()))

    def test_a_run_of_low_readings_triggers(self):
        watch, _ = self.watch(reader=volts_reader(3.2), consecutive=3)
        self.assertIsNone(watch.evaluate(watch.sample()))
        self.assertIsNone(watch.evaluate(watch.sample()))
        reason = watch.evaluate(watch.sample())
        self.assertIsNotNone(reason)
        self.assertIn("3 consecutive", reason)

    def test_a_recovery_breaks_the_streak(self):
        watch, _ = self.watch(reader=volts_reader(3.2), consecutive=3)
        watch.evaluate(watch.sample())
        watch.evaluate(watch.sample())
        watch.reader = volts_reader(3.9)
        self.assertIsNone(watch.evaluate(watch.sample()))
        self.assertEqual(watch.low_streak, [])

    def test_an_implausible_reading_breaks_the_streak_rather_than_extending_it(self):
        """A flapping bus must not accumulate towards powering the node off."""
        watch, _ = self.watch(reader=volts_reader(3.2), consecutive=3)
        watch.evaluate(watch.sample())
        watch.evaluate(watch.sample())
        watch.reader = reader_from({0xA2: 0x00, 0xA3: 0x00, 0xD0: 0x00, 0xD1: 0x00})
        self.assertIsNone(watch.evaluate(watch.sample()))
        self.assertEqual(watch.low_streak, [])

    def test_a_dead_bus_never_triggers_a_shutdown(self):
        def broken(address, register):
            raise OSError("bus error")
        watch, calls = self.watch(reader=broken, chip=IP5209, consecutive=2)
        for _ in range(10):
            watch.evaluate(watch.sample())
        self.assertEqual(calls, [])

    def test_a_charging_cell_is_not_shut_down(self):
        """Below threshold but rising means it is on a charger."""
        watch, calls = self.watch(consecutive=3)
        for volts in (3.20, 3.25, 3.30):
            watch.reader = volts_reader(volts)
            reason = watch.evaluate(watch.sample())
        self.assertIsNone(reason)
        self.assertEqual(calls, [])

    def test_a_falling_cell_at_the_same_levels_is_shut_down(self):
        watch, _ = self.watch(consecutive=3)
        for volts in (3.30, 3.25, 3.20):
            watch.reader = volts_reader(volts)
            reason = watch.evaluate(watch.sample())
        self.assertIsNotNone(reason)


class TestThePlausibilityFloorIsPhysical(unittest.TestCase):
    """3.0 V is not a tuned constant.

    The reading is taken by a running Pi, so the cell is above the PiSugar
    boost converter's cutoff by construction.  A value below that did not come
    from the cell.  This is also what makes the IP5312 profile decidably wrong
    on this node rather than merely unlikely.
    """

    def test_a_reading_below_the_boost_cutoff_is_distrusted(self):
        self.assertFalse(read_voltage(volts_reader(2.8, IP5209), IP5209).plausible)

    def test_the_default_threshold_sits_above_the_floor(self):
        # Otherwise a genuinely draining cell would become "implausible"
        # before it ever tripped the shutdown, and the watch would do nothing.
        watch = BatteryWatch(reader=volts_reader(4.0), chip=IP5209)
        from hummer_obd.battery import PLAUSIBLE_MIN_V
        self.assertGreater(watch.shutdown_v, PLAUSIBLE_MIN_V)


class TestConfigurationIsValidated(unittest.TestCase):
    def test_an_impossible_threshold_is_refused(self):
        for bad in (0.0, 1.5, 5.0, 12.0):
            with self.assertRaises(ValueError):
                BatteryWatch(reader=volts_reader(4.0), shutdown_v=bad)

    def test_a_non_positive_interval_or_count_is_refused(self):
        with self.assertRaises(ValueError):
            BatteryWatch(reader=volts_reader(4.0), interval_s=0)
        with self.assertRaises(ValueError):
            BatteryWatch(reader=volts_reader(4.0), consecutive=0)


class TestItOnlyEverReads(unittest.TestCase):
    def test_the_module_contains_no_i2c_write(self):
        # The power IC controls whether the node has power at all.  Reading it
        # is diagnostics; writing to it is a different risk class entirely, and
        # there is no reason for this module to do it.
        import inspect
        from hummer_obd import battery
        source = inspect.getsource(battery)
        for forbidden in ("write_byte", "write_word", "write_block", "write_i2c"):
            self.assertNotIn(forbidden, source)

    def test_dry_run_never_powers_off(self):
        calls = []
        watch = BatteryWatch(reader=volts_reader(3.1), chip=IP5209, consecutive=2,
                             interval_s=0.01, dry_run=True, logger=lambda *_: None,
                             shutdown=lambda: calls.append("off"))
        watch.run(max_cycles=6)
        self.assertEqual(calls, [])

    def test_a_real_run_acts_once_the_streak_is_met(self):
        calls = []
        watch = BatteryWatch(reader=volts_reader(3.1), chip=IP5209, consecutive=2,
                             interval_s=0.01, action="poweroff",
                             logger=lambda *_: None,
                             shutdown=lambda: calls.append("off"))
        self.assertEqual(watch.run(max_cycles=10), 0)
        self.assertEqual(calls, ["off"])


class TestTheDefaultActionDoesNotStrandTheNode(unittest.TestCase):
    """A PiSugar2 cannot power the Pi back on.

    Its own library says so: ``toggle_power_restore`` is implemented for the
    PiSugar 3 and returns "not supported" for the IP5209.  The documented way
    to restore output after a shutdown is to toggle the physical switch by
    hand.  Halting therefore strands an unattended node in a vehicle, which is
    worse than the flat cell it was meant to prevent -- so halting is not the
    default, and the default is not silently a halt either.
    """

    def test_the_default_stops_the_collector_rather_than_halting(self):
        watch = BatteryWatch(reader=volts_reader(4.0), chip=IP5209,
                             logger=lambda *_: None)
        self.assertEqual(watch.action, "stop-collector")
        self.assertEqual(watch._shutdown.__name__, "_stop_collector")

    def test_poweroff_remains_available_but_must_be_asked_for(self):
        watch = BatteryWatch(reader=volts_reader(4.0), chip=IP5209,
                             action="poweroff", logger=lambda *_: None)
        self.assertEqual(watch._shutdown.__name__, "_poweroff")

    def test_an_unknown_action_is_refused(self):
        for bad in ("halt", "reboot", "", "shutdown"):
            with self.assertRaises(ValueError):
                BatteryWatch(reader=volts_reader(4.0), chip=IP5209, action=bad)

    def test_stopping_the_collector_keeps_watching(self):
        """The node stays up, so the watch has to carry on.

        If the cell recovers nothing more is needed; if it does not, saying so
        again is the only useful thing left to do.  Returning after one action
        would leave the node running blind.
        """
        calls = []
        watch = BatteryWatch(reader=volts_reader(3.1), chip=IP5209, consecutive=2,
                             interval_s=0.01, logger=lambda *_: None,
                             shutdown=lambda: calls.append("stop"))
        watch.run(max_cycles=6)
        self.assertGreater(len(calls), 1, "the watch stopped watching after acting")
        self.assertEqual(watch.low_streak, [])

    def test_poweroff_stops_the_loop_because_the_node_is_going_away(self):
        calls = []
        watch = BatteryWatch(reader=volts_reader(3.1), chip=IP5209, consecutive=2,
                             interval_s=0.01, action="poweroff",
                             logger=lambda *_: None,
                             shutdown=lambda: calls.append("off"))
        self.assertEqual(watch.run(max_cycles=10), 0)
        self.assertEqual(calls, ["off"])

    def test_the_stop_uses_a_signal_the_collector_handles(self):
        # SIGTERM, because the collector installs a handler that closes the
        # SQLite session and flushes the raw log.  SIGKILL would lose that.
        import inspect
        from hummer_obd import battery
        source = inspect.getsource(battery.BatteryWatch._stop_collector)
        self.assertIn("-TERM", source)
        self.assertNotIn("-KILL", source)



if __name__ == "__main__":
    unittest.main()
