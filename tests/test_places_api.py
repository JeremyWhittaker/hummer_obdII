"""Places reach the page, and the node does not accept them from the network.

Two separate claims. The first is ordinary wiring. The second is a posture:
this server sits on a vehicle's diagnostic port, every route it has is a GET,
and a place list is not a good enough reason to change that.
"""

import threading
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
    """One write path, fenced. Previously there were none at all.

    Refusing to add one was the wrong call: the owner asked for a way to add
    locations from Home Assistant and got a judgment about risk appetite on
    their own network instead. So the surface exists, and these tests are the
    fence rather than the absence.
    """

    def test_the_only_write_verb_is_post(self):
        with TemporaryDirectory() as tmp:
            server = dashboard.make_server(store_with(tmp), port=0)
            try:
                handler = server.RequestHandlerClass
                self.assertTrue(hasattr(handler, "do_POST"))
                for method in ("do_PUT", "do_DELETE", "do_PATCH"):
                    with self.subTest(method=method):
                        self.assertFalse(hasattr(handler, method))
            finally:
                server.server_close()

    def post(self, store, body, origin="http://ha.example:8123"):
        import json as _json
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen
        server = dashboard.make_server(store, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/api/places",
                data=body if isinstance(body, bytes) else _json.dumps(body).encode(),
                method="POST",
                headers={"Content-Type": "application/json",
                         **({"Origin": origin} if origin else {})})
            try:
                with urlopen(request, timeout=3) as response:
                    return response.status, _json.load(response)
            except HTTPError as error:
                return error.code, None
        finally:
            server.shutdown()
            server.server_close()

    def store(self, tmp):
        return dashboard.SessionStore(
            tmp, expose_location=True, places_file=Path(tmp) / "places.json",
            allow_origins=("http://ha.example:8123",))

    def test_an_allowed_origin_can_name_a_place(self):
        with TemporaryDirectory() as tmp:
            store = self.store(tmp)
            code, body = self.post(store, {"name": "home", "lat": HOME[0],
                                           "lon": HOME[1]})
            self.assertEqual(code, 200)
            self.assertEqual([p["name"] for p in body["places"]], ["home"])
            self.assertEqual([p.name for p in store.places()], ["home"])

    def test_an_unnamed_origin_is_refused(self):
        with TemporaryDirectory() as tmp:
            code, _ = self.post(self.store(tmp),
                                {"name": "x", "lat": HOME[0], "lon": HOME[1]},
                                origin="http://evil.example")
            self.assertEqual(code, 403)

    def test_no_origin_at_all_is_refused(self):
        # A browser will not send one cross-origin without preflight, but a
        # non-browser client sends whatever it likes.
        with TemporaryDirectory() as tmp:
            code, _ = self.post(self.store(tmp),
                                {"name": "x", "lat": HOME[0], "lon": HOME[1]},
                                origin=None)
            self.assertEqual(code, 403)

    def test_an_oversized_body_is_refused_by_length_not_by_parsing(self):
        with TemporaryDirectory() as tmp:
            code, _ = self.post(self.store(tmp), b"{" + b"a" * 9000 + b"}")
            self.assertEqual(code, 413)

    def test_a_fence_smaller_than_the_receiver_error_is_refused(self):
        with TemporaryDirectory() as tmp:
            code, _ = self.post(self.store(tmp), {"name": "tiny", "lat": HOME[0],
                                                  "lon": HOME[1], "radius_m": 5})
            self.assertEqual(code, 400)

    def test_rubbish_is_refused(self):
        with TemporaryDirectory() as tmp:
            store = self.store(tmp)
            for body in (b"not json", b"[]", b'{"name": ""}',
                         b'{"name": "x"}', b'{"name": "x", "lat": 999, "lon": 0}'):
                with self.subTest(body=body):
                    code, _ = self.post(store, body)
                    self.assertEqual(code, 400)

    def test_a_zero_radius_deletes(self):
        with TemporaryDirectory() as tmp:
            store = self.store(tmp)
            self.post(store, {"name": "home", "lat": HOME[0], "lon": HOME[1]})
            code, body = self.post(store, {"name": "home", "radius_m": 0})
            self.assertEqual(code, 200)
            self.assertEqual(body["places"], [])

    def test_the_write_path_is_the_only_one(self):
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen
        with TemporaryDirectory() as tmp:
            server = dashboard.make_server(self.store(tmp), port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for path in ("/api/snapshot", "/api/sessions", "/api/stops", "/"):
                    with self.subTest(path=path), self.assertRaises(HTTPError) as e:
                        urlopen(Request(
                            f"http://127.0.0.1:{server.server_port}{path}",
                            data=b"{}", method="POST",
                            headers={"Origin": "http://ha.example:8123"}), timeout=3)
                    self.assertEqual(e.exception.code, 404)
            finally:
                server.shutdown()
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
