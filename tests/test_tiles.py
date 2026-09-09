"""Map tiles: a cache, a bound, and a promise about who gets told what.

This is the first module in the project that talks to a third party. Two
things therefore have to be true and stay true: it must never fetch a tile the
page did not ask to draw, and it must never be reachable when the operator has
not opted into serving location at all. Everything else here is about the
narrow gap between a URL path and the filesystem.

Nothing in this file touches the network. A test suite that depends on a
donation-funded server being up is a test suite that fails for reasons that
are not about this project.
"""

import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from hummer_obd import tiles
from hummer_obd.dashboard import SessionStore

PNG = b"\x89PNG\r\n\x1a\n" + b"fake tile body"


class Recorder:
    """A stand-in for the tile server that counts what it was asked for."""

    def __init__(self, body=PNG):
        self.calls = []
        self.body = body

    def __call__(self, z, x, y):
        self.calls.append((z, x, y))
        return self.body


class ValidityTests(unittest.TestCase):
    def test_a_real_tile_is_accepted(self):
        self.assertTrue(tiles.valid(13, 1489, 3324))
        self.assertTrue(tiles.valid(tiles.MIN_ZOOM, 0, 0))

    def test_zoom_outside_the_served_range_is_refused(self):
        self.assertFalse(tiles.valid(tiles.MIN_ZOOM - 1, 0, 0))
        self.assertFalse(tiles.valid(tiles.MAX_ZOOM + 1, 0, 0))

    def test_coordinates_outside_the_pyramid_are_refused(self):
        # At zoom 3 the world is 8x8 tiles, so 8 is one past the edge.
        self.assertTrue(tiles.valid(3, 7, 7))
        self.assertFalse(tiles.valid(3, 8, 0))
        self.assertFalse(tiles.valid(3, 0, 8))
        self.assertFalse(tiles.valid(3, -1, 0))

    def test_booleans_are_not_coordinates(self):
        # bool is a subclass of int, and True would otherwise index tile 1.
        self.assertFalse(tiles.valid(True, 1, 1))
        self.assertFalse(tiles.valid(3, True, 1))

    def test_non_integers_are_refused_rather_than_coerced(self):
        for bad in ("3", 3.0, None, [3]):
            with self.subTest(bad=bad):
                self.assertFalse(tiles.valid(bad, 1, 1))
                self.assertFalse(tiles.valid(3, bad, 1))


class ProjectionTests(unittest.TestCase):
    def test_a_known_coordinate_lands_on_its_known_tile(self):
        # Chandler, Arizona -- where this vehicle's sessions actually are.
        self.assertEqual(tiles.deg_to_tile(33.335082973, -111.785020622, 14),
                         (3104, 6581))

    def test_the_origin_corner_is_tile_zero(self):
        self.assertEqual(tiles.deg_to_tile(85.0, -180.0, 4), (0, 0))

    def test_latitudes_past_the_mercator_limit_are_clamped_not_wrapped(self):
        # asinh(tan(90deg)) is not a number, and a NaN here would become a
        # negative index and then a path outside the cache directory.
        for lat in (90.0, -90.0, 89.9, -89.9):
            with self.subTest(lat=lat):
                x, y = tiles.deg_to_tile(lat, 10.0, 6)
                self.assertTrue(tiles.valid(6, x, y))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.upstream = Recorder()
        self.store = tiles.TileStore(self.dir, session=self.upstream)

    def test_a_tile_is_fetched_once_and_then_served_from_disk(self):
        # The whole justification for proxying is that the tile server is
        # asked about a piece of ground exactly once, ever.
        for _ in range(5):
            self.assertEqual(self.store.get(13, 1489, 3324), PNG)
        self.assertEqual(self.upstream.calls, [(13, 1489, 3324)])

    def test_a_second_store_reuses_what_the_first_one_kept(self):
        self.store.get(13, 1489, 3324)
        fresh = Recorder()
        again = tiles.TileStore(self.dir, session=fresh)
        self.assertEqual(again.get(13, 1489, 3324), PNG)
        self.assertEqual(fresh.calls, [], "re-fetched a tile already on disk")

    def test_an_invalid_tile_never_reaches_the_network_or_the_disk(self):
        for bad in [(99, 0, 0), (3, 8, 0), (3, 0, -1)]:
            with self.subTest(bad=bad), self.assertRaises(tiles.TileError):
                self.store.get(*bad)
        self.assertEqual(self.upstream.calls, [])
        self.assertEqual(list(self.dir.rglob("*.png")), [])

    def test_a_body_that_is_not_a_png_is_refused_rather_than_cached(self):
        # Captive portals and error pages answer 200 with HTML. Caching one
        # would poison that tile for good, since a hit is never revalidated.
        store = tiles.TileStore(self.dir, session=lambda z, x, y: b"<html>nope")
        with self.assertRaises(tiles.TileError):
            store.get(13, 1489, 3324)
        self.assertEqual(list(self.dir.rglob("*.png")), [])

    def test_the_upstream_guard_rejects_a_body_that_is_not_a_png(self):
        # The check above uses an injected session; this one exercises the
        # real reader, which is where the guard has to live.
        class Response(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with patch("urllib.request.urlopen", return_value=Response(b"<html>nope")):
            with self.assertRaises(tiles.TileError):
                tiles.TileStore(self.dir)._fetch_upstream(13, 1489, 3324)

    def test_a_network_failure_is_reported_and_not_cached_as_an_empty_tile(self):
        def broken(z, x, y):
            raise OSError("no route to host")

        store = tiles.TileStore(self.dir, session=broken)
        with self.assertRaises(tiles.TileError):
            store.get(13, 1489, 3324)
        self.assertIsNone(store.cached(13, 1489, 3324))

    def test_the_bucket_stops_a_runaway_client_becoming_a_bulk_download(self):
        # The usage policy's one hard prohibition is pre-emptive fetching. A
        # client asking for thousands of distinct tiles is what that would
        # look like from the outside, whatever the intent, so it is bounded
        # mechanically rather than by good behaviour.
        store = tiles.TileStore(self.dir, session=self.upstream)
        refused = 0
        for x in range(tiles.BUCKET_CAPACITY + 40):
            try:
                store.get(14, 3000 + x, 6581)
            except tiles.TileError:
                refused += 1
        self.assertGreater(refused, 0, "an unbounded number of tiles was fetched")
        self.assertLessEqual(len(self.upstream.calls), tiles.BUCKET_CAPACITY + 1)

    def test_cache_hits_do_not_spend_tokens(self):
        # Otherwise panning back and forth over ground already drawn would
        # eventually be refused, which would be absurd -- nothing leaves the
        # node on a hit.
        store = tiles.TileStore(self.dir, session=self.upstream)
        store.get(13, 1489, 3324)
        for _ in range(tiles.BUCKET_CAPACITY * 2):
            store.get(13, 1489, 3324)
        self.assertEqual(len(self.upstream.calls), 1)

    def test_a_full_cache_refuses_rather_than_filling_the_card(self):
        store = tiles.TileStore(self.dir, session=self.upstream)
        with patch.object(tiles, "MAX_CACHE_FILES", 2):
            store.get(13, 1489, 3324)
            store.get(13, 1489, 3325)
            with self.assertRaises(tiles.TileError):
                store.get(13, 1489, 3326)

    def test_no_partial_file_is_left_where_a_tile_should_be(self):
        self.store.get(13, 1489, 3324)
        self.assertEqual([p.name for p in self.dir.rglob("*.part")], [])


class ReadOnlyDeploymentTests(unittest.TestCase):
    """The dashboard's unit forbids it from writing anything, on purpose.

    ProtectSystem=strict and ProtectHome=read-only are a good property for a
    read-only telemetry service and not one to quietly drill a hole through
    for the sake of a map. When the cache cannot be written the obligation
    not to ask the tile server twice for the same square still stands, so it
    has to be met some other way.
    """

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.upstream = Recorder()

    def read_only_store(self):
        store = tiles.TileStore(self.dir / "tiles", session=self.upstream)
        store.writable = False
        return store

    def test_an_unwritable_cache_is_detected_rather_than_assumed(self):
        # /proc is real, present, and not writable by anyone.
        self.assertFalse(tiles.TileStore("/proc/hummer-tiles").writable)
        self.assertTrue(tiles.TileStore(self.dir / "fresh").writable)

    def test_tiles_are_still_served_when_the_cache_cannot_be_written(self):
        store = self.read_only_store()
        self.assertEqual(store.get(13, 1489, 3324), PNG)
        self.assertEqual(list(self.dir.rglob("*.png")), [])

    def test_a_tile_is_still_only_fetched_once(self):
        # The whole point. A read-only disk is not a licence to hammer a
        # donation-funded server with the same request.
        store = self.read_only_store()
        for _ in range(20):
            store.get(13, 1489, 3324)
        self.assertEqual(self.upstream.calls, [(13, 1489, 3324)])

    def test_the_memory_cache_is_bounded(self):
        store = self.read_only_store()
        with patch.object(tiles, "MEMORY_TILES", 3):
            for x in range(10):
                store.get(13, 1489 + x, 3324)
            self.assertLessEqual(len(store._memory), 3)

    def test_a_disk_cache_written_by_another_run_is_still_read(self):
        # Reading is permitted under the hardened unit even though writing is
        # not, so a cache seeded by a maintenance run keeps paying off.
        seeded = tiles.TileStore(self.dir / "tiles", session=self.upstream)
        seeded.get(13, 1489, 3324)
        fresh = Recorder()
        after = tiles.TileStore(self.dir / "tiles", session=fresh)
        after.writable = False
        self.assertEqual(after.get(13, 1489, 3324), PNG)
        self.assertEqual(fresh.calls, [])


class ReachabilityTests(unittest.TestCase):
    """Tiles must not be reachable when location is not being served."""

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def test_no_tile_store_exists_without_expose_location(self):
        # The map shows where the vehicle has been. Serving it while refusing
        # to serve coordinates would be a distinction without a difference,
        # and would leak the thing the flag exists to withhold.
        store = SessionStore(self.dir, expose_location=False,
                             tile_cache=self.dir / "tiles")
        self.assertIsNone(store.tiles)

    def test_tiles_are_available_once_location_is(self):
        store = SessionStore(self.dir, expose_location=True,
                             tile_cache=self.dir / "tiles")
        self.assertIsNotNone(store.tiles)

    def test_no_tile_store_when_no_cache_directory_was_asked_for(self):
        store = SessionStore(self.dir, expose_location=True, tile_cache=None)
        self.assertIsNone(store.tiles)


class PolicyTests(unittest.TestCase):
    """The parts of the tile usage policy that are code, not prose."""

    def test_the_user_agent_identifies_this_application_and_a_contact(self):
        # The policy blocks generic library defaults by name, because the
        # operators cannot identify or contact whoever is responsible.
        agent = tiles.USER_AGENT
        self.assertIn("hummer-obd-node", agent)
        self.assertIn("+http", agent, "no contact URL in the User-Agent")
        for default in ("python-urllib", "python-requests", "curl/", "okhttp"):
            self.assertNotIn(default, agent)

    def test_the_served_zoom_range_stays_modest(self):
        # High zoom over a wide area is the shape the policy calls out as a
        # scan. A driving map has no use past the high teens.
        self.assertLessEqual(tiles.MAX_ZOOM, 18)
        self.assertGreaterEqual(tiles.MIN_ZOOM, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
