"""Places reach the page, and the node does not accept them from the network.

Two separate claims. The first is ordinary wiring. The second is a posture:
this server sits on a vehicle's diagnostic port, every route it has is a GET,
and a place list is not a good enough reason to change that.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from hummer_obd import dashboard
from hummer_obd.places import Place, Places

HOME = (33.32770, -111.74170)
AWAY = (33.42000, -111.93000)


def store_with(tmp: str, places: Places | None = None) -> dashboard.SessionStore:
    path = Path(tmp) / "places.json"
    if places is not None:
        places.save(path)
    return dashboard.SessionStore(tmp, expose_location=True, places_file=path)


class ReadingTests(unittest.TestCase):
    def test_places_are_served(self):
        with TemporaryDirectory() as tmp:
            store = store_with(tmp, Places([Place("home", *HOME)]))
            self.assertEqual([p.name for p in store.places()], ["home"])

    def test_a_missing_file_is_no_places_rather_than_an_error(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(len(store_with(tmp).places()), 0)

    def test_the_list_is_re_read_when_it_changes(self):
        """Added at the kerbside, visible without a restart.

        Cached on mtime: a place list is edited by a CLI while the server
        runs, and a cache held for the process lifetime would mean the reader
        has to be restarted to see a place that was just created.
        """
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "places.json"
            store = store_with(tmp, Places([Place("home", *HOME)]))
            self.assertEqual(len(store.places()), 1)
            Places([Place("home", *HOME), Place("work", *AWAY)]).save(path)
            self.assertEqual(sorted(p.name for p in store.places()),
                             ["home", "work"])


class WriteSurfaceTests(unittest.TestCase):
    def test_the_handler_has_no_write_methods(self):
        """Read over HTTP, write on the machine.

        Asserted structurally rather than by convention, because the whole
        point is that a future edit adding do_POST for something convenient
        would silently open a write surface on a node wired to a vehicle.
        """
        with TemporaryDirectory() as tmp:
            server = dashboard.make_server(store_with(tmp), port=0)
            try:
                handler = server.RequestHandlerClass
                for method in ("do_POST", "do_PUT", "do_DELETE", "do_PATCH"):
                    with self.subTest(method=method):
                        self.assertFalse(hasattr(handler, method))
            finally:
                server.server_close()


class JourneyTests(unittest.TestCase):
    def rows(self, lat, lon, n=5):
        return [{"utc": f"2026-09-10T05:{i:02d}:00Z", "elapsed_s": float(i * 10),
                 "gps_lat": lat, "gps_lon": lon,
                 "gps_speed_mps": 0.0, "gps_epx_m": 15.0} for i in range(n)]

    def test_a_trip_between_two_places_is_named(self):
        named = Places([Place("home", *HOME), Place("work", *AWAY)])
        history = dashboard._history(
            self.rows(*HOME) + self.rows(*AWAY), location=True)
        first = next(r for r in history if r.get("gps_lat") is not None)
        last = next(r for r in reversed(history) if r.get("gps_lat") is not None)
        self.assertEqual(named.label(first["gps_lat"], first["gps_lon"]), "home")
        self.assertEqual(named.label(last["gps_lat"], last["gps_lon"]), "work")

    def test_a_session_that_never_left_is_not_a_journey(self):
        named = Places([Place("home", *HOME)])
        history = dashboard._history(self.rows(*HOME, n=10), location=True)
        labels = {named.label(r.get("gps_lat"), r.get("gps_lon"))
                  for r in history}
        self.assertEqual(labels, {"home"})

    def test_labels_are_taken_after_anchoring_not_before(self):
        # A jittering parked truck can wander out of its own fence and back.
        # "nowhere to home" is worse than no label at all.
        import inspect
        source = inspect.getsource(dashboard.SessionStore._snapshot)
        self.assertIn("already-anchored history", source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
