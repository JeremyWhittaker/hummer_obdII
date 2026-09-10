"""Named places, and the ways a fence can be wrong.

The point of this module is that "home to work" is a thing a person
remembers and a pair of coordinates is not. So the tests are mostly about the
cases where a fence would mislabel a trip, because a wrong label is worse than
no label: it is a memory the data invented.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from hummer_obd.places import (DEFAULT_RADIUS_M, MAX_RADIUS_M, MIN_RADIUS_M,
                               Place, Places)

#: The driveway this vehicle actually parks in, from its own fixes.
HOME = (33.32770, -111.74170)


class PlaceTests(unittest.TestCase):
    def test_a_point_at_the_centre_is_inside(self):
        p = Place("home", HOME[0], HOME[1])
        self.assertTrue(p.contains(*HOME))
        self.assertAlmostEqual(p.distance_m(*HOME), 0.0, places=6)

    def test_a_point_beyond_the_radius_is_outside(self):
        p = Place("home", HOME[0], HOME[1], radius_m=100.0)
        # ~0.0027 degrees of latitude is about 300 m.
        self.assertFalse(p.contains(HOME[0] + 0.0027, HOME[1]))

    def test_the_default_radius_covers_the_measured_parked_wander(self):
        # Fixes wander a measured 46 m while this truck sits still, and
        # gps_epx_m has been seen at 72.9 m. A fence inside that flaps.
        self.assertGreater(DEFAULT_RADIUS_M, 72.9)

    def test_a_fence_smaller_than_the_receiver_error_is_refused(self):
        with self.assertRaises(ValueError):
            Place("home", HOME[0], HOME[1], radius_m=MIN_RADIUS_M - 1)

    def test_an_absurd_fence_is_refused(self):
        with self.assertRaises(ValueError):
            Place("home", HOME[0], HOME[1], radius_m=MAX_RADIUS_M + 1)

    def test_a_nameless_place_is_refused(self):
        for name in ("", "   "):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    Place(name, HOME[0], HOME[1])

    def test_coordinates_out_of_range_are_refused(self):
        with self.assertRaises(ValueError):
            Place("nowhere", 91.0, 0.0)
        with self.assertRaises(ValueError):
            Place("nowhere", 0.0, 181.0)


class LookupTests(unittest.TestCase):
    def test_the_smallest_containing_place_wins(self):
        """Fences overlap in life, and the tighter one is what a person means.

        A house sits inside a neighbourhood; an office suite inside a park.
        Returning whichever was defined first would label a trip by the
        coarsest fence that happens to contain it -- and "nearest centre",
        which this rule was at first, cannot separate concentric fences at
        all, because every distance from a shared centre is equal.
        """
        places = Places([
            Place("neighbourhood", HOME[0], HOME[1], radius_m=2000.0),
            Place("home", HOME[0], HOME[1], radius_m=120.0),
        ])
        self.assertEqual(places.label(*HOME), "home")

    def test_a_fix_outside_every_fence_has_no_label(self):
        places = Places([Place("home", HOME[0], HOME[1])])
        self.assertIsNone(places.label(HOME[0] + 1.0, HOME[1]))

    def test_a_missing_fix_is_not_a_place(self):
        places = Places([Place("home", HOME[0], HOME[1])])
        for lat, lon in ((None, None), (HOME[0], None), (None, HOME[1])):
            with self.subTest(lat=lat, lon=lon):
                self.assertIsNone(places.label(lat, lon))

    def test_adding_a_name_twice_edits_rather_than_duplicates(self):
        places = Places([Place("home", HOME[0], HOME[1], radius_m=120.0)])
        places.add(Place("home", HOME[0], HOME[1], radius_m=300.0))
        self.assertEqual(len(places), 1)
        self.assertEqual(next(iter(places)).radius_m, 300.0)

    def test_removing_reports_whether_it_removed(self):
        places = Places([Place("home", HOME[0], HOME[1])])
        self.assertTrue(places.remove("home"))
        self.assertFalse(places.remove("home"))


class PersistenceTests(unittest.TestCase):
    def test_a_round_trip_preserves_every_field(self):
        places = Places([Place("home", HOME[0], HOME[1], 150.0, "the driveway")])
        back = Places.from_json(places.to_json())
        self.assertEqual(len(back), 1)
        got = next(iter(back))
        self.assertEqual((got.name, got.radius_m, got.note),
                         ("home", 150.0, "the driveway"))

    def test_one_bad_row_does_not_cost_the_file(self):
        """A hand-edited list must not be all-or-nothing.

        Refusing the whole file for one typo means a mistyped fence silently
        unlabels every trip -- the failure is invisible and total.
        """
        text = json.dumps({"places": [
            {"name": "home", "lat": HOME[0], "lon": HOME[1]},
            {"name": "broken", "lat": "not a number", "lon": 0},
            {"lat": 1.0, "lon": 2.0},
            "not even an object",
            {"name": "work", "lat": 33.42, "lon": -111.93, "radius_m": 200},
        ]})
        places = Places.from_json(text)
        self.assertEqual(sorted(p.name for p in places), ["home", "work"])

    def test_rubbish_parses_to_an_empty_list_not_an_exception(self):
        for text in ("", "not json", "[]", "{}", "null", "123"):
            with self.subTest(text=text):
                self.assertEqual(len(Places.from_json(text)), 0)

    def test_a_missing_file_is_an_empty_list(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(len(Places.load(Path(tmp) / "nope.json")), 0)

    def test_saving_is_atomic(self):
        # A half-written file read by the next poll is an unlabelled map.
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "places.json"
            Places([Place("home", HOME[0], HOME[1])]).save(path)
            self.assertEqual(len(Places.load(path)), 1)
            # No temporary files left behind.
            self.assertEqual([p.name for p in path.parent.iterdir()],
                             ["places.json"])

    def test_saved_json_is_stable_for_unchanged_content(self):
        places = Places([Place("b", 1.0, 2.0), Place("a", 3.0, 4.0)])
        self.assertEqual(places.to_json(), places.to_json())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class StopTests(unittest.TestCase):
    """Where the vehicle stopped, which is not the same as where it was."""

    WORK = (33.42000, -111.93000)

    def fixes(self, at, count, start_min=0):
        return [{"gps_lat": at[0] + (i % 3 - 1) * 0.00009,
                 "gps_lon": at[1] + (i % 2 - 1) * 0.00009,
                 "gps_speed_mps": 0.0,
                 "utc": f"2026-09-10T0{start_min // 60}:{start_min % 60:02d}:{i % 60:02d}Z"}
                for i in range(count)]

    def long_stay(self, at, minutes, start_hour=1):
        return [{"gps_lat": at[0], "gps_lon": at[1], "gps_speed_mps": 0.0,
                 "utc": f"2026-09-10T{start_hour:02d}:{m:02d}:00Z"}
                for m in range(minutes)]

    def test_two_places_do_not_average_into_one(self):
        """The failure this exists to prevent.

        A median over a whole session that visited two places lands between
        them -- in a field neither stop is in. Measured on this vehicle: two
        parked clusters 5.2 km apart, and the global median sat between them.
        """
        from hummer_obd.places import stops
        found = stops(self.long_stay(HOME, 20) + self.long_stay(self.WORK, 20, 3))
        self.assertEqual(len(found), 2)
        centres = sorted(round(s["lat"], 3) for s in found)
        self.assertEqual(centres, sorted([round(HOME[0], 3),
                                          round(self.WORK[0], 3)]))

    def test_a_traffic_light_is_not_a_place(self):
        from hummer_obd.places import stops
        brief = [{"gps_lat": HOME[0], "gps_lon": HOME[1], "gps_speed_mps": 0.0,
                  "utc": f"2026-09-10T01:00:{s:02d}Z"} for s in range(0, 40, 10)]
        self.assertEqual(stops(brief), [])

    def test_moving_fixes_are_not_stops(self):
        from hummer_obd.places import stops
        driving = [{"gps_lat": HOME[0] + i * 0.001, "gps_lon": HOME[1],
                    "gps_speed_mps": 15.0,
                    "utc": f"2026-09-10T01:{i:02d}:00Z"} for i in range(30)]
        self.assertEqual(stops(driving), [])

    def test_jitter_inside_one_driveway_stays_one_stop(self):
        from hummer_obd.places import stops
        found = stops(self.long_stay(HOME, 20))
        self.assertEqual(len(found), 1)
        self.assertGreaterEqual(found[0]["fixes"], 20)

    def test_a_stop_reports_how_long_it_lasted(self):
        from hummer_obd.places import stops
        found = stops(self.long_stay(HOME, 30))
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0]["seconds"], 29 * 60, delta=61)

    def test_stops_carry_no_names_of_their_own(self):
        # Naming is the owner's job; this only says where they stopped.
        from hummer_obd.places import stops
        found = stops(self.long_stay(HOME, 20))
        self.assertNotIn("name", found[0])

    def test_rows_without_fixes_are_skipped(self):
        from hummer_obd.places import stops
        rows = self.long_stay(HOME, 20) + [{"gps_lat": None, "gps_lon": None}]
        self.assertEqual(len(stops(rows)), 1)

    def test_an_empty_session_has_no_stops(self):
        from hummer_obd.places import stops
        self.assertEqual(stops([]), [])
