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

    def test_location_off_still_returns_rows(self):
        from hummer_obd import dashboard
        points = dashboard._history(self.rows(), location=False)
        self.assertEqual(len(points), 20)
        self.assertNotIn("gps_lat", points[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
