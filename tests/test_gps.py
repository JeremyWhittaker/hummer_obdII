"""The gpsd reader.

The property that matters most: the recorder's job is the vehicle, and a GPS
that is unplugged, unfixed, wedged or lying must cost it nothing. Everything
here is about failing quietly and being honest about why.
"""

import json
import unittest

from hummer_obd import gps
from hummer_obd.gps import GpsReader, parse_sky, parse_tpv


class _FakeSocket:
    """A gpsd that says exactly what a test tells it to."""

    def __init__(self, lines, *, close_after=True):
        payload = b"".join(json.dumps(x).encode() + b"\n" for x in lines)
        self._chunks = [payload[i:i + 7] for i in range(0, len(payload), 7)]
        self.sent = b""
        self.closed = False
        self._close_after = close_after

    def sendall(self, data):
        self.sent += data

    def recv(self, _n):
        if self._chunks:
            return self._chunks.pop(0)
        return b"" if self._close_after else b""

    def close(self):
        self.closed = True


def _reader(lines, **kw):
    fake = _FakeSocket(lines)
    r = GpsReader(connect=lambda h, p, t: fake, **kw)
    r._session()
    return r, fake


class TestParsing(unittest.TestCase):

    def test_altitude_is_read_under_every_spelling_gpsd_has_used(self):
        # A reader that knows one spelling records no altitude against half
        # the installed base, and does it silently.
        for key in ("altMSL", "altHAE", "alt"):
            with self.subTest(key=key):
                self.assertEqual(
                    parse_tpv({"mode": 3, key: 335.2})["alt_m"], 335.2)

    def test_the_preferred_altitude_wins_when_several_are_present(self):
        got = parse_tpv({"mode": 3, "alt": 1.0, "altHAE": 2.0, "altMSL": 3.0})
        self.assertEqual(got["alt_m"], 3.0)

    def test_a_boolean_is_not_a_number(self):
        # bool subclasses int in Python, so an unguarded isinstance check
        # records a flag as the value 1.0.
        self.assertIsNone(gps._number(True))
        self.assertIsNone(gps._number(False))
        self.assertEqual(parse_tpv({"mode": 3, "lat": True})["lat"], None)

    def test_mode_survives_nonsense_without_raising(self):
        for bad in ({"mode": "3"}, {"mode": None}, {"mode": True}, {}):
            with self.subTest(report=bad):
                self.assertEqual(parse_tpv(bad)["mode"], 0)

    def test_satellites_counts_those_used_not_those_visible(self):
        # Visible climbs first and means little; a fix rests on used.
        sky = {"satellites": [{"used": True}, {"used": True}, {"used": False}]}
        self.assertEqual(parse_sky(sky), 2)

    def test_satellites_prefers_the_explicit_count_when_gpsd_gives_one(self):
        self.assertEqual(parse_sky({"uSat": 5, "satellites": [{"used": True}]}), 5)

    def test_track_falls_back_to_magnetic(self):
        self.assertEqual(parse_tpv({"mode": 3, "magtrack": 221.7})["track_deg"], 221.7)


class TestTheReaderIsHonestAboutWhyThereIsNoFix(unittest.TestCase):

    def test_a_good_fix_produces_every_column(self):
        r, _ = _reader([
            {"class": "DEVICES", "devices": [{"path": "/dev/ttyUSB0"}]},
            {"class": "SKY", "uSat": 5},
            {"class": "TPV", "mode": 3, "lat": 1.5, "lon": -2.5, "altMSL": 335.2,
             "speed": 0.174, "track": 221.719, "epx": 44.9,
             "time": "2026-09-08T21:02:01.000Z"},
        ])
        cols = r.columns()
        self.assertEqual(set(cols), set(gps.COLUMNS))
        self.assertEqual(cols["gps_mode"], 3)
        self.assertEqual(cols["gps_sats"], 5)
        self.assertEqual(cols["gps_alt_m"], 335.2)
        self.assertEqual(cols["gps_time"], "2026-09-08T21:02:01.000Z")
        self.assertIn("3D fix", r.describe())

    def test_every_column_is_present_even_with_no_fix_at_all(self):
        # A row that omits these when the GPS is quiet is indistinguishable
        # from a row recorded before GPS existed.
        r = GpsReader()
        self.assertEqual(set(r.columns()), set(gps.COLUMNS))
        self.assertTrue(all(v is None for v in r.columns().values()))

    def test_gpsd_serving_no_device_is_distinguished_from_no_sky_view(self):
        # gpsd answers on its port whether or not a receiver is attached, so
        # these look identical to a client that does not check DEVICES. The
        # sibling project could only tell them apart by hand.
        r, _ = _reader([{"class": "DEVICES", "devices": []}])
        self.assertFalse(r.has_device)
        self.assertIn("no device", r.describe())

    def test_a_cold_start_says_so_rather_than_looking_broken(self):
        r, _ = _reader([
            {"class": "DEVICES", "devices": [{"path": "/dev/ttyUSB0"}]},
            {"class": "TPV", "mode": 1},
        ])
        self.assertIsNone(r.fix())
        self.assertIn("cold start", r.describe())

    def test_a_2d_fix_is_usable_but_a_1d_one_is_not(self):
        for mode, usable in ((3, True), (2, True), (1, False), (0, False)):
            with self.subTest(mode=mode):
                r, _ = _reader([{"class": "TPV", "mode": mode, "lat": 1.0}])
                self.assertEqual(r.fix() is not None, usable)

    def test_a_stale_fix_is_withheld(self):
        now = [100.0]
        r, _ = _reader([{"class": "TPV", "mode": 3, "lat": 1.0}],
                       clock=lambda: now[0])
        self.assertIsNotNone(r.fix(), "the fix should be fresh at first")
        now[0] = 200.0
        self.assertIsNone(r.fix(), "a fix 100 s old must not be reported")

    def test_staleness_uses_the_monotonic_clock_not_the_wall_clock(self):
        # This Pi has no RTC. Wall time steps by days when NTP or a GPS fix
        # lands, which would make a fresh fix look ancient during exactly the
        # boot window that matters.
        import inspect
        src = inspect.getsource(GpsReader.__init__)
        self.assertIn("time.monotonic", src)
        self.assertNotIn("time.time", src)

    def test_malformed_json_is_counted_and_skipped_not_fatal(self):
        fake = _FakeSocket([{"class": "TPV", "mode": 3, "lat": 1.0}])
        fake._chunks.insert(0, b"{not json\n")
        r = GpsReader(connect=lambda h, p, t: fake)
        r._session()
        self.assertGreaterEqual(r.malformed, 1)
        self.assertIsNotNone(r.fix(), "one bad line must not lose the stream")

    def test_the_watch_command_is_sent_once_per_connection(self):
        _, fake = _reader([{"class": "TPV", "mode": 3, "lat": 1.0}])
        self.assertEqual(fake.sent, gps.WATCH)

    def test_the_socket_is_closed_even_when_the_stream_explodes(self):
        class _Angry(_FakeSocket):
            def recv(self, _n):
                raise OSError("cable yanked")

        fake = _Angry([])
        r = GpsReader(connect=lambda h, p, t: fake)
        with self.assertRaises(OSError):
            r._session()
        self.assertTrue(fake.closed, "a failed session must not leak the socket")

    def test_it_defaults_to_gpsds_real_port_not_a_workaround(self):
        # The sibling project documents a private instance on 2948 for when
        # /etc/default/gpsd cannot be edited. That does not survive a reboot,
        # and encoding somebody's stopgap as a default outlives the stopgap.
        self.assertEqual(gps.DEFAULT_PORT, 2947)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
