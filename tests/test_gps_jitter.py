"""A parked truck must not be drawn driving around its own driveway.

Every threshold here was measured on this vehicle rather than chosen, and the
tests say which measurement, because two of them were wrong the first time and
the reason was always an assumption about how clean a signal is.
"""

import unittest

from hummer_obd import gps


def fix(lat, lon, speed=0.0, epx=15.0):
    return {"gps_lat": lat, "gps_lon": lon,
            "gps_speed_mps": speed, "gps_epx_m": epx}


#: About 10 m at this latitude, which is inside every error estimate seen.
JITTER = 0.00009
HOME = (33.3277, -111.7417)


class DistanceTests(unittest.TestCase):
    def test_haversine_against_a_known_degree(self):
        # One degree of latitude is 111.19 km anywhere on a sphere.
        self.assertAlmostEqual(gps.haversine_m(33.0, -111.0, 34.0, -111.0),
                               111195, delta=50)

    def test_zero_distance_for_the_same_point(self):
        self.assertEqual(gps.haversine_m(33.3, -111.7, 33.3, -111.7), 0.0)


class AnchorTests(unittest.TestCase):
    def test_a_parked_truck_accumulates_no_distance(self):
        # The measured failure: 62 m of travel from a truck that never moved.
        points = [fix(HOME[0] + (i % 3 - 1) * JITTER,
                      HOME[1] + (i % 2 - 1) * JITTER) for i in range(40)]
        self.assertGreater(sum(
            gps.haversine_m(a["gps_lat"], a["gps_lon"], b["gps_lat"], b["gps_lon"])
            for a, b in zip(points, points[1:])), 50, "test data must jitter")
        self.assertEqual(gps.track_distance_m(points), 0.0)

    def test_a_single_spurious_speed_does_not_release_the_anchor(self):
        """Doppler is better than differenced positions, not clean.

        On the parked session of 2026-09-10 it read 0.5 m/s or more in 8 of 60
        fixes and peaked at 4.62 m/s -- 16.6 kph of standing still. Treating
        any one fix as authority left 77 m of invented travel.
        """
        points = [fix(HOME[0], HOME[1]) for _ in range(10)]
        points[5] = fix(HOME[0] + JITTER, HOME[1] + JITTER, speed=4.62)
        self.assertEqual(gps.track_distance_m(points), 0.0)

    def test_sustained_movement_does_release_it(self):
        points = ([fix(HOME[0], HOME[1]) for _ in range(3)]
                  + [fix(HOME[0] + 0.001 * i, HOME[1], speed=10.0)
                     for i in range(1, 6)])
        self.assertGreater(gps.track_distance_m(points), 300)

    def test_a_real_departure_is_not_swallowed(self):
        # Held for MOVING_RUN fixes, then the distance is picked up in full --
        # the anchor jumps to the current position, it does not interpolate.
        points = ([fix(HOME[0], HOME[1]) for _ in range(3)]
                  + [fix(HOME[0] + 0.01, HOME[1], speed=15.0) for _ in range(3)])
        self.assertGreater(gps.track_distance_m(points), 1000)

    def test_a_fix_beyond_the_safety_valve_always_releases(self):
        # A receiver stuck reporting zero speed must not pin the vehicle.
        far = gps.MAX_ANCHOR_M * 2 / 111195.0
        points = [fix(HOME[0], HOME[1]), fix(HOME[0] + far, HOME[1], speed=0.0)]
        self.assertGreater(gps.track_distance_m(points), gps.MAX_ANCHOR_M)

    def test_held_fixes_are_marked_as_held(self):
        points = [fix(HOME[0] + i * JITTER, HOME[1]) for i in range(6)]
        out = gps.anchor_stationary(points)
        self.assertTrue(any(p["gps_anchored"] for p in out))
        self.assertTrue(all(p["gps_lat"] == HOME[0] for p in out))

    def test_the_input_is_not_modified(self):
        points = [fix(HOME[0] + JITTER, HOME[1])]
        gps.anchor_stationary(points)
        self.assertNotIn("gps_anchored", points[0])
        self.assertEqual(points[0]["gps_lat"], HOME[0] + JITTER)

    def test_rows_without_a_fix_pass_through(self):
        points = [fix(HOME[0], HOME[1]), {"gps_lat": None, "gps_lon": None},
                  fix(HOME[0], HOME[1])]
        out = gps.anchor_stationary(points)
        self.assertEqual(len(out), 3)
        self.assertIsNone(out[1]["gps_lat"])

    def test_no_doppler_falls_back_to_the_error_estimate(self):
        # A step smaller than the receiver's own stated error is not a step.
        # Oscillating, not drifting. A monotonic walk of the same step size is
        # a real departure and must not be suppressed -- an earlier version of
        # this test drifted 50 m and then asserted the filter should ignore it.
        small = [{"gps_lat": HOME[0] + JITTER * (i % 3 - 1), "gps_lon": HOME[1],
                  "gps_epx_m": 30.0} for i in range(12)]
        self.assertEqual(gps.track_distance_m(small), 0.0)

    def test_no_doppler_still_follows_a_real_move(self):
        big = [{"gps_lat": HOME[0] + 0.005 * i, "gps_lon": HOME[1],
                "gps_epx_m": 30.0} for i in range(4)]
        self.assertGreater(gps.track_distance_m(big), 1000)

    def test_an_empty_track_is_zero_not_an_error(self):
        self.assertEqual(gps.track_distance_m([]), 0.0)


class VehicleVetoTests(unittest.TestCase):
    """The vehicle's own wheels outrank the receiver, and the veto latches.

    Three versions of this filter arbitrated between GPS position and GPS
    Doppler -- two views of one noisy signal -- while an independent witness
    sat in the same CSV row saying the wheels were not turning. On a parked
    session the anchor released twice on Doppler noise (0.612 m/s at 39.9 m
    error, then 1.575 m/s), leaving three positions 120 m and 132 m apart and
    148 m of invented path. MOVING_RUN cannot reject those: both bursts held
    above threshold for two consecutive fixes, which is what it tests for.
    """

    def test_doppler_noise_cannot_move_a_truck_whose_wheels_read_zero(self):
        points = [fix(HOME[0], HOME[1]) for _ in range(10)]
        points[4]["gps_speed_mps"] = 0.612
        points[5]["gps_speed_mps"] = 1.575
        for p in points:
            p["speed_kph"] = 0.0
        self.assertEqual(gps.track_distance_m(points), 0.0)

    def test_the_wheel_sensors_count_when_the_legislated_pid_is_silent(self):
        # speed_kph answered in NONE of 261 rows on the session that motivated
        # this, while all four wheel sensors answered and read zero.
        points = [fix(HOME[0], HOME[1]) for _ in range(10)]
        points[4]["gps_speed_mps"] = 1.575
        points[5]["gps_speed_mps"] = 1.575
        for p in points:
            p["speed_kph"] = None
            p["wheel_fl_kph"] = 0.0
        self.assertEqual(gps.track_distance_m(points), 0.0)

    def test_the_veto_latches_across_rows_the_vehicle_did_not_answer(self):
        """12 rows in 261 answered. A veto read per-row would be idle for 249.

        A vehicle that reported zero does not start moving without reporting
        something.
        """
        points = [fix(HOME[0], HOME[1]) for _ in range(30)]
        points[0]["wheel_fl_kph"] = 0.0          # the only row that answers
        for p in points[1:]:
            p["speed_kph"] = None
        points[20]["gps_speed_mps"] = 1.6
        points[21]["gps_speed_mps"] = 1.6
        self.assertEqual(gps.track_distance_m(points), 0.0)

    def test_a_vehicle_that_reports_moving_releases_normally(self):
        points = ([fix(HOME[0], HOME[1]) for _ in range(3)]
                  + [dict(fix(HOME[0] + 0.002 * i, HOME[1], speed=12.0),
                          speed_kph=45.0) for i in range(1, 6)])
        self.assertGreater(gps.track_distance_m(points), 500)

    def test_one_turning_wheel_is_a_moving_vehicle(self):
        # Fastest, not average: a wheel reading zero beside one that turns is
        # a vehicle in motion, and the only question here is stationarity.
        self.assertGreaterEqual(
            gps._vehicle_kph({"wheel_fl_kph": 0.0, "wheel_fr_kph": 40.0}), 40.0)

    def test_no_vehicle_reading_falls_back_to_the_gps_rules(self):
        points = [fix(HOME[0], HOME[1]) for _ in range(8)]
        points[3]["gps_speed_mps"] = 12.0
        points[4]["gps_speed_mps"] = 12.0
        points[5] = fix(HOME[0] + 0.01, HOME[1], speed=12.0)
        self.assertGreater(gps.track_distance_m(points), 500)

    def test_a_stuck_zero_cannot_pin_a_vehicle_across_a_real_journey(self):
        # MAX_ANCHOR_M stays an unconditional escape even under the veto.
        far = gps.MAX_ANCHOR_M * 2 / 111195.0
        points = [dict(fix(HOME[0], HOME[1]), speed_kph=0.0),
                  dict(fix(HOME[0] + far, HOME[1]), speed_kph=0.0)]
        self.assertGreater(gps.track_distance_m(points), gps.MAX_ANCHOR_M)


class OdometerAuthorityTests(unittest.TestCase):
    """The odometer outranks everything, because it is the only signal here
    with no noise in it.

    Wheel speed and Doppler both answer "is it moving right now", a question
    with error bars: this vehicle's Doppler reported 4.62 m/s while parked and
    speed_kph went silent for 261 rows straight. The odometer answers "has it
    gone anywhere", which is cumulative and monotonic -- it either counted a
    revolution or it did not.
    """

    def rows(self, n, odo, lat=HOME[0], lon=HOME[1], **kw):
        return [dict(fix(lat + (i % 3 - 1) * JITTER, lon, **kw),
                     odometer_km=odo) for i in range(n)]

    def test_an_unmoving_odometer_pins_the_truck_absolutely(self):
        # Doppler screaming, position wandering, odometer still. Held.
        points = self.rows(12, 2500.0, speed=9.0)
        self.assertEqual(gps.track_distance_m(points), 0.0)

    def test_the_smallest_advance_this_vehicle_reports_releases_it(self):
        # 0.1 km, because that is what the vehicle actually reports. Across
        # every session recorded 2026-09-08 to 2026-09-10 there are 1,182
        # non-zero odometer steps and the distinct values are 0.1 (820),
        # 0.2 (284), 0.3 (75) and 0.4 km (3). There has never been one smaller.
        # The earlier version of this test used 0.02 km and passed, which made
        # a threshold calibrated to a resolution this truck does not have look
        # verified.
        points = (self.rows(4, 2500.0)
                  + [dict(fix(HOME[0] + 0.01, HOME[1]), odometer_km=2500.1)])
        self.assertGreater(gps.track_distance_m(points), 500)

    def test_an_advance_below_the_reporting_step_does_not(self):
        # Below half a real step is float noise, not travel.
        points = (self.rows(4, 2500.000)
                  + [dict(fix(HOME[0] + 0.01, HOME[1]), odometer_km=2500.004)])
        self.assertEqual(gps.track_distance_m(points), 0.0)

    def test_a_turning_wheel_releases_the_anchor_between_odometer_ticks(self):
        """The defect this class was written confidently enough to miss.

        The odometer branch used to be absolute: if the counter had not
        advanced it returned the anchor and skipped every other test, including
        MAX_ANCHOR_M. That was justified by a comment claiming the counter
        resolved to 0.01 km. It resolves to 0.1 km, so between two ticks the
        truck covers up to a hundred metres of road with the reading unchanged
        -- and the displayed position froze there, with nothing able to release
        it.

        Measured on four real drives before the fix: on rows where the
        vehicle's own wheels read above 20 km/h, the anchor held 10 of 61,
        10 of 49, 6 of 25 and 8 of 48 fixes, displacing the shown position by
        up to 194.8 m. After it, zero of all four.

        Every existing test here asserts a SUMMED track distance, which is why
        none of them saw it: holding a fix and then releasing it still travels
        the same total. This one asserts where each emitted point actually is.
        """
        points = []
        for i in range(8):
            # A truck moving steadily, wheels reporting it, odometer stuck
            # between ticks the entire time.
            points.append(dict(fix(HOME[0] + i * 0.0009, HOME[1]),
                               odometer_km=2500.0, wheel_fl_kph=55.0,
                               speed_kph=55.0))
        out = gps.anchor_stationary(points)
        worst = max(gps.haversine_m(src["gps_lat"], src["gps_lon"],
                                    got["gps_lat"], got["gps_lon"])
                    for src, got in zip(points, out))
        self.assertLess(worst, 1.0,
                        f"a fix was moved {worst:.1f} m while the wheels "
                        f"reported 55 km/h")
        self.assertFalse(any(got["gps_anchored"] for got in out),
                         "a moving truck was anchored")

    def test_the_odometer_still_outranks_a_wild_fix_on_a_parked_truck(self):
        """The property the fix above must not cost.

        Letting a turning wheel release the anchor is right. Letting DISTANCE
        release it is not: a receiver that puts a stationary truck a kilometre
        away is exactly what this filter exists for, and the first attempt at
        the fix above reintroduced those jumps by allowing MAX_ANCHOR_M to
        override a silent odometer.
        """
        points = (self.rows(4, 2500.0)
                  + [dict(fix(HOME[0] + 0.01, HOME[1]), odometer_km=2500.0)])
        out = gps.anchor_stationary(points)
        self.assertEqual(gps.track_distance_m(points), 0.0)
        self.assertTrue(out[-1]["gps_anchored"],
                        "a 1.1 km jump was accepted from a truck whose "
                        "odometer never moved")

    def test_the_anchor_cannot_get_stuck(self):
        """The failure the wheel-speed veto actually had.

        On session 021134 it reported 0 m for a session whose odometer moved
        100 m: wheel speed answered rarely, read zero, and the latch never
        released. An anchor that cannot release is not a filter, it is a
        deletion.
        """
        points = []
        for leg in range(4):
            points += self.rows(5, 2500.0 + leg * 0.5,
                                lat=HOME[0] + leg * 0.004)
        self.assertGreater(gps.track_distance_m(points), 800)

    def test_a_silent_odometer_falls_back_to_the_speed_rules(self):
        # It goes quiet with the rest of service 01, so the tests below it
        # still have to work.
        points = [fix(HOME[0], HOME[1]) for _ in range(10)]
        points[4]["gps_speed_mps"] = 4.62
        for p in points:
            p["wheel_fl_kph"] = 0.0
        self.assertEqual(gps.track_distance_m(points), 0.0)

    def test_a_partly_silent_odometer_still_governs_the_rows_it_reaches(self):
        points = self.rows(6, 2500.0, speed=6.0)
        points += [fix(HOME[0], HOME[1], speed=6.0) for _ in range(4)]
        # The odometer rows are pinned; the silent ones fall to Doppler, which
        # needs MOVING_RUN consecutive samples and has them here.
        self.assertLess(gps.track_distance_m(points), 60)


class ThresholdTests(unittest.TestCase):
    def test_the_stationary_threshold_admits_a_creeping_vehicle(self):
        # 0.5 m/s is 1.8 kph. Traffic crawl is well above it.
        self.assertLess(gps.STATIONARY_MPS, 1.0)

    def test_the_run_length_is_short_enough_not_to_clip_a_trip(self):
        # Each held fix is roughly ten seconds of a departure.
        self.assertLessEqual(gps.MOVING_RUN, 2)

    def test_the_safety_valve_is_far_beyond_any_observed_error(self):
        # Largest gps_epx_m seen on this vehicle is 72.9 m.
        self.assertGreater(gps.MAX_ANCHOR_M, 72.9 * 3)


class HistoryIsAnchoredTests(unittest.TestCase):
    """The trail the map draws must be the filtered one, not the raw fixes."""

    def rows(self):
        # A parked truck, jittering. Numeric, because the store converts
        # before _history sees a row -- a first version of this fixture used
        # strings and every field came back None, which looks like the filter
        # failing rather than the fixture being wrong.
        return [{"utc": f"2026-09-10T02:{i:02d}:00Z", "elapsed_s": float(i * 10),
                 "gps_lat": HOME[0] + (i % 3 - 1) * JITTER,
                 "gps_lon": HOME[1] + (i % 2 - 1) * JITTER,
                 "gps_speed_mps": 0.0, "gps_epx_m": 15.0}
                for i in range(20)]

    def test_the_history_endpoint_anchors_the_trail(self):
        from hummer_obd import dashboard
        points = dashboard._history(self.rows(), location=True)
        lats = {p["gps_lat"] for p in points if p.get("gps_lat") is not None}
        self.assertEqual(len(lats), 1,
                         "a parked truck must have one position, not a cloud")

    def test_anchoring_is_done_on_the_node_not_in_the_browser(self):
        # So the map, the trip length and anything else reading the API agree.
        # A filter applied only in the page leaves every other consumer wrong.
        import inspect
        from hummer_obd import dashboard
        source = inspect.getsource(dashboard._history)
        self.assertIn("anchor_stationary", source)

    def test_raw_hex_fields_survive_the_history(self):
        """`_finite` is a numeric test and a raw field is a string of hex.

        Every raw column read through `reading` came back None, which is how
        three thermal fields reached the history as absent while the part
        index told the reader they were drawn on the model. The page then had
        nothing to scale them against.
        """
        from hummer_obd import dashboard
        rows = [{"utc": f"2026-09-10T05:{i:02d}:00Z", "elapsed_s": float(i),
                 "coolant_1_raw": "01F4", "coolant_2_raw": "0283",
                 "compressor_temp_raw": "3C"} for i in range(5)]
        points = dashboard._history(rows)
        for name in ("coolant_1_raw", "coolant_2_raw", "compressor_temp_raw"):
            with self.subTest(name=name):
                self.assertTrue(all(p[name] for p in points),
                                f"{name} was dropped from the history")

    def test_location_off_still_returns_rows(self):
        from hummer_obd import dashboard
        points = dashboard._history(self.rows(), location=False)
        self.assertEqual(len(points), 20)
        self.assertNotIn("gps_lat", points[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
