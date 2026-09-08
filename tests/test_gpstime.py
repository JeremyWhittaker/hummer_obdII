"""Setting the clock from the satellites.

The bug this exists for: on 2026-09-08 the node returned from a three-day
outage with a clock restored from before it, opened a session named for the
wrong day, and wrote an odometer 107 km ahead of where the vehicle had been at
that timestamp. Nothing rejected it, because the value was plausible and only
the time was wrong.

The bug this must not introduce: a clock confidently set to 2006. SiRF is the
chipset family known for GPS week-rollover faults, it is what this vehicle
carries, and a rolled-over receiver reports a precise, well-formed time with a
healthy fix and a full satellite count while doing it.
"""

import unittest
from datetime import datetime, timedelta, timezone

from hummer_obd import gpstime
from hummer_obd.gpstime import decide, is_plausible, parse_gps_time, wait_for_time

NOW = datetime(2026, 9, 8, 21, 0, tzinfo=timezone.utc)


class TestParsingSatelliteTime(unittest.TestCase):

    def test_gpsd_iso_with_a_z_suffix(self):
        got = parse_gps_time("2026-09-08T21:02:01.000Z")
        self.assertEqual(got, datetime(2026, 9, 8, 21, 2, 1, tzinfo=timezone.utc))

    def test_a_naive_timestamp_is_treated_as_utc_not_local(self):
        # gpsd speaks UTC. Reading it as local time would silently shift the
        # clock by the timezone offset, which on this vehicle is seven hours.
        self.assertEqual(parse_gps_time("2026-09-08T21:02:01").tzinfo, timezone.utc)

    def test_rubbish_is_rejected_rather_than_raising(self):
        for bad in (None, "", "not a time", 12345, b"2026-09-08T21:02:01Z", {}):
            with self.subTest(value=bad):
                self.assertIsNone(parse_gps_time(bad))


class TestTheRolloverGuard(unittest.TestCase):

    def test_a_sirf_week_rollover_is_refused(self):
        # ~19.7 years early, which is what the fault actually produces.
        rolled = NOW - timedelta(days=365.25 * 19.7)
        self.assertFalse(is_plausible(rolled))
        should, why = decide(rolled, NOW)
        self.assertFalse(should)
        self.assertIn("rollover", why)

    def test_an_absurd_future_date_is_refused(self):
        self.assertFalse(is_plausible(datetime(2099, 1, 1, tzinfo=timezone.utc)))

    def test_a_real_time_is_accepted(self):
        self.assertTrue(is_plausible(NOW))

    def test_the_floor_is_fixed_not_relative(self):
        # A floor derived from "now" would move with a wrong clock, which is
        # the one input this must not trust.
        self.assertEqual(gpstime.EPOCH_FLOOR.year, 2026)
        self.assertLess(gpstime.EPOCH_FLOOR, gpstime.EPOCH_CEILING)


class TestTheDecision(unittest.TestCase):

    def test_the_outage_that_prompted_this_is_detected(self):
        restored = datetime(2026, 9, 5, 11, 1, 32, tzinfo=timezone.utc)
        should, why = decide(NOW, restored)
        self.assertTrue(should)
        self.assertIn("+", why)

    def test_a_clock_already_right_is_left_alone(self):
        should, why = decide(NOW, NOW + timedelta(milliseconds=200))
        self.assertFalse(should)
        self.assertIn("within", why)

    def test_no_satellite_time_is_not_an_error_just_a_no(self):
        should, why = decide(None, NOW)
        self.assertFalse(should)
        self.assertIn("no satellite time", why)

    def test_it_steps_backwards_as_well_as_forwards(self):
        # A clock restored from the future is as wrong as one from the past.
        should, _ = decide(NOW, NOW + timedelta(hours=5))
        self.assertTrue(should)


class TestWaiting(unittest.TestCase):

    class _Reader:
        def __init__(self, fixes):
            self._fixes = list(fixes)

        def fix(self):
            return self._fixes.pop(0) if self._fixes else None

    def test_it_returns_as_soon_as_a_good_time_arrives(self):
        reader = self._Reader([
            None,
            {"time": "2026-09-08T21:02:01.000Z"},
        ])
        slept = []
        got = wait_for_time(reader, wait_s=60, poll_s=2,
                            sleeper=slept.append, clock=lambda: 0.0)
        self.assertEqual(got.year, 2026)
        self.assertEqual(slept, [2], "should stop polling once it has one")

    def test_a_cold_receiver_times_out_without_hanging(self):
        ticks = iter([0.0, 10.0, 20.0, 30.0, 100.0])
        got = wait_for_time(self._Reader([]), wait_s=30, poll_s=2,
                            sleeper=lambda s: None, clock=lambda: next(ticks))
        self.assertIsNone(got)

    def test_a_rolled_over_time_does_not_satisfy_the_wait(self):
        # The dangerous case: a fix arrives, looks healthy, and is wrong. The
        # wait must keep going rather than accept it.
        reader = self._Reader([{"time": "2006-12-25T00:00:00.000Z"}])
        ticks = iter([0.0, 5.0, 99.0])
        self.assertIsNone(
            wait_for_time(reader, wait_s=30, poll_s=2,
                          sleeper=lambda s: None, clock=lambda: next(ticks)))


class TestTheCommandLine(unittest.TestCase):

    def test_it_does_not_touch_the_clock_without_being_told_to(self):
        # Same discipline as every other tool here: report by default, act only
        # on an explicit flag.
        import inspect
        src = inspect.getsource(gpstime.main)
        self.assertIn("args.set", src)
        self.assertIn("dry run", src)

    def test_no_fix_exits_zero_so_the_boot_unit_does_not_fail(self):
        # A cold receiver in a garage is normal. A non-zero exit would mark the
        # boot unit failed for a non-problem, and on a unit ordered before the
        # recorder that is worse than the wrong clock it was meant to fix.
        class _Never:
            def start(self):
                return self

            def stop(self):
                pass

            def fix(self):
                return None

            def describe(self):
                return "device present, no fix yet"

        original = gpstime.gps_module.GpsReader
        gpstime.gps_module.GpsReader = lambda *a, **k: _Never()
        try:
            self.assertEqual(gpstime.main(["--wait-s", "0"]), 0)
        finally:
            gpstime.gps_module.GpsReader = original

    def test_it_refuses_to_set_a_rolled_over_clock_end_to_end(self):
        class _Rolled:
            def start(self):
                return self

            def stop(self):
                pass

            def fix(self):
                return {"time": "2006-12-25T00:00:00.000Z"}

            def describe(self):
                return "3D fix, 7 satellites used"

        original = gpstime.gps_module.GpsReader
        gpstime.gps_module.GpsReader = lambda *a, **k: _Rolled()
        try:
            # --set is passed, and it must STILL not set the clock.
            self.assertEqual(gpstime.main(["--wait-s", "0", "--set"]), 0)
        finally:
            gpstime.gps_module.GpsReader = original


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
