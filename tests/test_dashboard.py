"""A browser may read telemetry, never a device, arbitrary file, or location."""

import csv
import math
import re
import json
import os
import shutil
import statistics
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from hummer_obd import analyze, live
from hummer_obd.dashboard import (CACHE_ENTRIES, MAX_HISTORY, SessionStore,
                                  energy_budget, make_server, public_columns)

PAGE_PATH = Path(__file__).resolve().parents[1] / "src" / "hummer_obd" / "dashboard.html"
#: The session CSVs the page's absolute scales are measured from. Untracked --
#: they are the vehicle's own recordings, not source -- so tests that cite them
#: skip rather than fail when they are not on this machine.
CORPUS = Path(__file__).resolve().parents[1] / "evidence" / "sessions"
NODE = shutil.which("node") or shutil.which("nodejs")


def page_script() -> str:
    page = PAGE_PATH.read_text(encoding="utf-8")
    return re.findall(r"<script[^>]*>([\s\S]*?)</script>", page)[0]


def _balanced(source: str, start: int) -> str:
    """The text from *start* through the brace that closes its first block."""
    depth, opened = 0, source.index("{", start)
    for i in range(opened, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise AssertionError("unbalanced braces in dashboard.html")


#: The page functions that decide whether a reading may be drawn at all. Every
#: one of them encodes a rule this project has got wrong before, and a rule
#: that cannot be RUN is a rule nothing really tests -- greps pass against code
#: that reads the right name and then does the wrong thing with it.
READING_RULES = ("num", "hexBytes", "frameIndex", "carriedFrom",
                 "torqueCounts", "effortScale", "effortEmissive",
                 "cornerDeviation", "cornerEmissive", "cellSpreadEmissive",
                 "cellEnvelope", "moduleColour", "contrastFloor",
                 "swatchRgb", "swatchHex", "legendEntries")
#: The first name of each top-level `var` declaration those rules close over.
READING_CONSTS = ("TORQUE_ZERO", "EFFORT_NEUTRAL", "CORNER_DEADBAND_KPH",
                  "CORNER_AGREE", "CORNER_OVER_FAST",
                  "CELL_SPREAD_TYPICAL_MV", "CELL_SPREAD_OVER",
                  "CONTRAST_FLOOR", "SWATCH_AMBIENT", "TYRE_DIFFUSE")


def tyre_alpha() -> float:
    """The alpha the tyre is actually drawn at, read off the page.

    Read rather than written down: every contrast margin on the tyre is a
    claim about the OUTPUT pixel, and the output pixel is the emissive times
    this number. A test that hard-coded 0.38 would keep passing the day the
    tyre went more transparent and the distinction it guards went with it.
    """
    source = page_script()
    start = source.index('add({ id: "tyre-" + w[0]')
    found = re.search(r"alpha: ([\d.]+)", source[start:source.index("});", start)])
    assert found, "the tyre no longer declares an alpha in dashboard.html"
    return float(found.group(1))


def as_rendered(expression: str, part: dict) -> list:
    """*expression*'s emissive after draw()'s last pass, in the DEFAULT view.

    The default view is body-off (`layers.shell` is 0 at init and nothing
    turns it on), which is the view `contrastFloor` acts in -- so this is what
    the page puts on screen, as opposed to what an earlier line assigned.
    """
    return run_reading_rules("return contrastFloor(%s, %s, true);"
                             % (expression, json.dumps(part)))


#: The part flags the draw loop sees for a tyre and for a halfshaft, and the
#: alpha each is composited at. Taken from the `add({...})` calls in
#: dashboard.html: the tyre is translucent so the disc behind it reads
#: through, the shaft is opaque.
TYRE_PART = {"layer": "wheels", "cornerTint": True}
SHAFT_PART = {"layer": "drive", "effort": True}


def shade(diffuse: list, emissive: list, ambient: float) -> str:
    """The page's shading rule at its ambient floor, done independently here.

    Deliberately NOT `swatchHex` run in node: the page's own function is what
    is under test, so an assertion built out of it would agree with any
    arithmetic it happened to do. The INPUTS come from the page -- the part's
    diffuse array, the emissive its own draw rule returns for that state, and
    the ambient term the shader multiplies by -- and only the sum is redone.

    The rule is the fragment shader's first two lines with the view-dependent
    terms dropped, which is what a legend can honestly claim:

        lit = uColour * (0.20 + kd * 0.85 + fd) + spec;  lit += uEmissive;

    `kd`, `fd` and `spec` depend on the camera, so `uColour * 0.20 +
    uEmissive` is the part at its darkest in any frame from any angle.
    `math.floor(v + 0.5)` rather than `round`, because JavaScript's
    `Math.round` takes halves upward and Python's takes them to even.
    """
    return "#" + "".join(
        "%02x" % math.floor(min(1.0, max(0.0, d * ambient + e)) * 255 + 0.5)
        for d, e in zip(diffuse, emissive))


def peak_channel(swatch: str) -> int:
    """How bright a swatch's brightest channel is, 0-255.

    The ordering these entries are read in. NOT a weighted sum: the states
    differ in direction as much as in magnitude -- warm puts its light in red
    and cool puts it in blue -- and relative luminance weights blue at 0.0722,
    so it ranks a tyre at full cool tint BELOW the pale grey of a measured
    agreement although the cool tint is plainly the stronger mark on screen.
    A plain sum inverts the same pair. The peak channel is hue-blind, which is
    what ordering states that differ in hue needs.
    """
    return max(int(swatch[i:i + 2], 16) for i in (1, 3, 5))


def page_constants() -> dict:
    """The page's own numbers, read off its source as JSON-ish literals."""
    source, out = page_script(), {}
    for first in READING_CONSTS:
        found = re.search(r"^  var (%s\s*=[^;]+);" % first, source, re.M)
        assert found, f"{first} is no longer a top-level var in dashboard.html"
        for part in re.finditer(r"(\w+)\s*=\s*(\[[^\]]*\]|[-\d.]+)", found.group(1)):
            out[part.group(1)] = json.loads(part.group(2))
    return out


def run_reading_rules(body: str, state=None):
    """Run *body* in node against the page's own reading rules.

    Extracted rather than reimplemented: a copy of the rule in Python would
    pass while the page did something else, which is the exact failure this
    file exists to catch.
    """
    source = page_script()
    pieces = [re.search(r"^  var %s = [^;]+;" % name, source, re.M).group(0)
              for name in READING_CONSTS]
    pieces += [_balanced(source, source.index("function %s(" % name))
               for name in READING_RULES]
    pieces.append("var state = %s;" % json.dumps(state if state is not None else {}))
    pieces.append("process.stdout.write(JSON.stringify((function () {\n%s\n})()));"
                  % body)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rules.js"
        path.write_text("\n".join(pieces), encoding="utf-8")
        done = subprocess.run([NODE, str(path)], capture_output=True,
                              text=True, timeout=120)
    assert done.returncode == 0, f"the page's reading rules failed:\n{done.stderr[:800]}"
    return json.loads(done.stdout)


def corpus_spread_mv() -> list[float]:
    """Every `cell_spread_mv` reading in the recorded sessions on this machine."""
    out = []
    for path in sorted(CORPUS.glob("drive-*.csv")):
        with path.open(newline="") as handle:
            reader = csv.DictReader(handle)
            if "cell_spread_mv" not in (reader.fieldnames or []):
                continue
            for row in reader:
                try:
                    out.append(float(row["cell_spread_mv"]))
                except (TypeError, ValueError):
                    continue
    return out


def corpus_corner_deviations() -> list[float]:
    """Every per-corner deviation the page's own rule would tint, in km/h.

    The same population `cornerDeviation` produces: a row is counted only when
    all four corners answered and their mean is at or above the 5 km/h floor,
    and each row then contributes four readings.
    """
    corners = ("wheel_fl_kph", "wheel_fr_kph", "wheel_rl_kph", "wheel_rr_kph")
    out = []
    for path in sorted(CORPUS.glob("drive-*.csv")):
        with path.open(newline="") as handle:
            reader = csv.DictReader(handle)
            if not set(corners) <= set(reader.fieldnames or []):
                continue
            for row in reader:
                try:
                    wheels = [float(row[name]) for name in corners]
                except (TypeError, ValueError):
                    continue
                mean = sum(wheels) / 4
                if mean < 5:
                    continue
                out.extend(abs(value - mean) for value in wheels)
    return out


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile, which is what the page's comment cites."""
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def carried(history: list[dict], index: int, column: str):
    """A delta-encoded column resolved the way the page resolves it.

    A row that does not mention the column is identical to the nearest
    earlier row that does; a row carrying an explicit null has no reading.
    See DELTA_RAW_COLUMNS in dashboard.py.
    """
    for row in reversed(history[:index + 1]):
        if column in row:
            return row[column]
    raise AssertionError(f"no row at or before {index} states {column}")


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.store = SessionStore(self.directory)
        self.name = "drive-20260101T120000Z.csv"
        self.now = datetime.fromisoformat("2026-01-01T12:00:10+00:00").timestamp()

    def write(self, rows, name=None):
        path = self.directory / (name or self.name)
        fields = list(dict.fromkeys(key for row in rows for key in row))
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fields)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def rows(self):
        return [
            {"utc": "2026-01-01T12:00:00Z", "elapsed_s": 0, "pack_v": 390,
             "pack_a": 20, "speed_kph": 0, "soc_pct": 80, "energy_kwh": 152},
            {"utc": "2026-01-01T12:00:10Z", "elapsed_s": 10, "pack_v": 389,
             "pack_a": 40, "speed_kph": 50, "soc_pct": 79, "energy_kwh": 150.1},
        ]

    def sequence(self, arrays):
        """One row per entry, each carrying that array (None for no array)."""
        rows = []
        for index, array in enumerate(arrays):
            row = dict(self.rows()[0])
            row["utc"] = "2026-01-01T12:00:%02dZ" % index
            row["elapsed_s"] = index
            row["array_2af1"] = "" if array is None else array
            rows.append(row)
        return rows

    def test_empty_waiting_state(self):
        result = self.store.snapshot(now=self.now)
        self.assertEqual(result["session"]["status"], "empty")
        self.assertEqual(result["signals"], {})
        self.assertEqual(self.store.sessions(), {"sessions": [], "latest": None})

    def test_live_snapshot_has_provenance_and_coherent_power(self):
        self.write(self.rows())
        with patch("serial.Serial", side_effect=AssertionError("must not open serial")):
            result = self.store.snapshot(now=self.now)
        self.assertEqual(result["session"]["status"], "live")
        self.assertEqual(result["signals"]["pack_v"]["identifier"], "0x2885")
        self.assertEqual(result["signals"]["pack_v"]["status"], "fresh")
        self.assertAlmostEqual(result["derived"]["pack_kw"], 15.56)
        self.assertEqual(result["history"][-1]["pack_kw"], 15.56)

    def test_stopped_file_ages_even_when_cached_and_hides_current_tiles(self):
        self.write(self.rows())
        first = self.store.snapshot(now=self.now)
        later = self.store.snapshot(now=self.now + 300)
        self.assertEqual(first["signals"]["pack_v"]["age_s"], 0)
        self.assertEqual(later["session"]["status"], "stale")
        self.assertEqual(later["signals"]["pack_v"]["age_s"], 300)
        self.assertIsNone(later["derived"]["pack_kw"])
        self.assertEqual(later["signals"]["pack_v"]["value"], 389)

    def test_historical_selection_cannot_be_labelled_live(self):
        self.write(self.rows())
        result = self.store.snapshot(self.name, now=self.now + 3600)
        self.assertEqual(result["session"]["status"], "historical")
        self.assertAlmostEqual(result["derived"]["pack_kw"], 15.56)
        self.assertEqual(result["session"]["age_s"], 3600)
        self.assertEqual(result["signals"]["pack_v"]["age_s"], 0)
        self.assertEqual(result["signals"]["pack_v"]["status"], "fresh")

    def test_signal_age_includes_time_since_recorder_last_row(self):
        rows = self.rows()
        rows[-1]["pack_a"] = ""
        self.write(rows)
        result = self.store.snapshot(now=self.now + 40)
        self.assertEqual(result["session"]["status"], "live")
        self.assertEqual(result["signals"]["pack_a"]["age_s"], 50)
        self.assertEqual(result["signals"]["pack_a"]["status"], "stale")
        self.assertIsNone(result["derived"]["pack_kw"])

    def test_alternating_fresh_sensors_cannot_rejuvenate_an_old_pair(self):
        rows = self.rows()[:1] + [
            dict(utc="2026-01-01T12:01:30Z", elapsed_s=90, pack_v=395),
            dict(utc="2026-01-01T12:01:35Z", elapsed_s=95, pack_a=100),
            dict(utc="2026-01-01T12:01:40Z", elapsed_s=100, pack_v=394),
        ]
        self.write(rows)
        result = self.store.snapshot(now=self.now + 90)
        self.assertEqual(result["signals"]["pack_v"]["status"], "fresh")
        self.assertEqual(result["signals"]["pack_a"]["status"], "fresh")
        self.assertIsNone(result["derived"]["pack_kw"])

    def test_the_replay_carries_every_column_the_scene_paints_per_frame(self):
        # A column the 3D view draws from and the history does not carry is a
        # part frozen at the session's last sample while the scrub marker
        # moves through the trip. That is not a visible error -- the model
        # simply describes the wrong moment -- so it gets an explicit test
        # rather than being noticed by eye. The raw ones are hex strings and
        # reach the history through `raw`, not `reading`: `_finite` is a
        # numeric gate and dropped every one of them silently once already.
        rows = self.rows()
        for row in rows:
            row["array_2af1"] = "1A" * 24
            row["field_2429_raw"] = "5806"
            row["cell_min_v"] = 3.98
            row["cell_avg_v"] = 3.99
            row["cell_max_v"] = 4.01
        self.write(rows)
        history = self.store.snapshot(now=self.now)["history"]
        point = history[-1]
        # array_2af1 is delta-encoded, so "carried" is how the page reads it
        # -- and a value that reaches the last frame by being unchanged is
        # still a value that reaches the last frame.
        self.assertEqual(carried(history, len(history) - 1, "array_2af1"),
                         "1A" * 24)
        self.assertEqual(point["field_2429_raw"], "5806")
        self.assertEqual(point["cell_min_v"], 3.98)
        self.assertEqual(point["cell_avg_v"], 3.99)
        self.assertEqual(point["cell_max_v"], 4.01)

    def test_a_missing_or_malformed_raw_array_replays_as_absent(self):
        # Absent, not stale-but-present: the page draws no colour from a null
        # and leaves the blocks their base colour, which is the honest
        # picture. A row of non-hex rubbish must take the same path as a row
        # with nothing in it at all.
        rows = self.rows()
        rows[0]["array_2af1"] = "1A" * 24
        rows[1]["array_2af1"] = "not hex at all"
        self.write(rows)
        history = self.store.snapshot(now=self.now)["history"]
        self.assertEqual(history[0]["array_2af1"], "1A" * 24)
        # An explicit null, not a silent omission: the row CHANGED, from an
        # array to no array, and the delta encoding has to say so out loud or
        # the page would carry the previous row's colours across the gap.
        self.assertIn("array_2af1", history[1])
        self.assertIsNone(history[1]["array_2af1"])

    def test_the_module_array_is_sent_only_when_it_changes(self):
        # 24 bytes is 48 hex characters plus a key name -- about 63 bytes of
        # every row, rebuilt, walked by _clean and re-serialised on every 5 s
        # poll on a Pi Zero 2 W. Across evidence/sessions the array repeats
        # the previous row 89% of the time.
        self.write(self.sequence(["1A" * 24, "1A" * 24, "2B" * 24, "2B" * 24]))
        history = self.store.snapshot(now=self.now)["history"]
        stated = [index for index, row in enumerate(history)
                  if "array_2af1" in row]
        self.assertEqual(
            stated, [0, 2],
            "the module array is repeated on rows that did not change it",
        )

    def test_the_delta_encoding_loses_no_row(self):
        # The saving is only allowed if resolving it back gives the same
        # per-row answer the full payload gave. Every shape that matters is
        # in this one sequence: a first row, a repeat, a change, a drop to
        # nothing, a repeat of nothing, and a return.
        shapes = ["1A" * 24, "1A" * 24, "2B" * 24, None, None, "2B" * 24]
        self.write(self.sequence(shapes))
        history = self.store.snapshot(now=self.now)["history"]
        self.assertEqual(
            [carried(history, i, "array_2af1") for i in range(len(history))],
            shapes,
            "resolving the delta encoding does not reproduce the rows",
        )
        self.assertLess(
            sum(1 for row in history if "array_2af1" in row), len(shapes),
            "nothing was saved: every row still states the array",
        )

    def test_torque_is_withheld_once_its_only_source_goes_stale(self):
        # These two used to sit outside CURRENT_FIELDS, so the staleness loop
        # never reached them and the page was handed an hour-old effort
        # reading with nothing saying it was old. The halfshafts draw from
        # them now, so the shafts would have glowed for a truck standing
        # still.
        rows = self.rows()
        rows[0]["field_2429_raw"] = "5900"
        self.write(rows)
        fresh = self.store.snapshot(now=self.now)
        self.assertEqual(fresh["derived"]["torque_dir"], "drive")
        self.assertIsNotNone(fresh["derived"]["torque_counts"])
        later = self.store.snapshot(now=self.now + 300)
        self.assertEqual(later["signals"]["field_2429_raw"]["status"], "stale")
        self.assertIsNone(later["derived"]["torque_counts"])
        self.assertIsNone(later["derived"]["torque_dir"])

    def test_no_location_identity_unknown_fields_or_absolute_path_in_api(self):
        rows = self.rows()
        rows[-1].update(gps_lat=12.345678, gps_lon=23.456789,
                        gps_time="2026-01-01T12:00:10Z", gps_sats=9,
                        private_note="PRIVATE_SENTINEL", vin="PRIVATE_IDENTITY")
        self.write(rows)
        result = self.store.snapshot(now=self.now)
        serialized = json.dumps(result, allow_nan=False)
        for private in ("gps_lat", "gps_lon", "gps_time", "12.345678", "23.456789",
                        "PRIVATE_SENTINEL", "PRIVATE_IDENTITY", str(self.directory)):
            self.assertNotIn(private, serialized)
        self.assertEqual(result["signals"]["gps_sats"]["value"], 9)

    def test_invalid_reading_and_future_clock_do_not_look_live(self):
        rows = self.rows()
        rows[-1]["pack_v"] = 1.0
        self.write(rows)
        result = self.store.snapshot(now=self.now)
        self.assertEqual(result["signals"]["pack_v"]["status"], "invalid")
        self.assertIsNone(result["derived"]["pack_v"])
        self.assertIsNone(result["history"][-1]["pack_kw"])
        future = self.store.snapshot(now=self.now - 100)
        self.assertEqual(future["session"]["status"], "stale")
        self.assertTrue(any("clock" in w for w in future["warnings"]))

    def test_unproven_raw_fields_are_not_labelled_decoded(self):
        rows = self.rows()
        rows[-1]["cell_extra_raw"] = "01020304"
        self.write(rows)
        item = self.store.snapshot(now=self.now)["signals"]["cell_extra_raw"]
        self.assertEqual(item["value"], "01020304")
        self.assertIsNone(item["confidence"])
        self.assertEqual(item["confidence_label"], "raw / unscaled")

    def test_sleep_transition_cannot_look_like_perfectly_balanced_cells(self):
        rows = self.rows()
        rows[-1].update(cell_spread_mv=0, cell_avg_v=0, cell_min_v=0, cell_max_v=0)
        self.write(rows)
        result = self.store.snapshot(now=self.now)
        self.assertIsNone(result["derived"]["cell_spread_mv"])
        self.assertIsNone(result["history"][-1]["cell_spread_mv"])
        self.assertEqual(result["signals"]["cell_min_v"]["status"], "invalid")

    def test_latest_uses_file_activity_after_a_clock_correction(self):
        older_name = "drive-20250101T120000Z.csv"
        current = self.write(self.rows(), older_name)
        future_named = self.write(self.rows())
        os.utime(future_named, (100, 100))
        os.utime(current, (200, 200))
        self.assertEqual(self.store.sessions()["latest"], older_name)
        self.assertEqual(self.store.snapshot(now=self.now)["session"]["id"], older_name)

    def test_traversal_symlinks_and_unknown_session_fail_closed(self):
        for name in ("../secret.csv", "/etc/passwd", "drive-other.csv"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.snapshot(name)
        with tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / "secret"
            secret.write_text("private")
            (self.directory / self.name).symlink_to(secret)
            self.assertEqual(self.store.sessions()["sessions"], [])
            with self.assertRaises(FileNotFoundError):
                self.store.snapshot(self.name)

    def test_cache_refreshes_when_recorder_adds_a_row(self):
        self.write(self.rows()[:1])
        self.assertEqual(self.store.snapshot(now=self.now)["session"]["rows"], 1)
        self.write(self.rows())
        self.assertEqual(self.store.snapshot(now=self.now)["session"]["rows"], 2)

    def test_invalid_elapsed_row_preserves_a_break_in_energy_coverage(self):
        rows = [self.rows()[0], dict(self.rows()[0], elapsed_s="bad-time"), self.rows()[1]]
        self.write(rows)
        result = self.store.snapshot(now=self.now)
        self.assertIsNone(result["energy_budget"]["observed_s"])
        self.assertNotIn("drawn_kwh_from_pack_current", result["report"]["energy"])
        self.assertTrue(any("elapsed" in w for w in result["warnings"]))

    def test_history_size_is_bounded(self):
        rows = [dict(self.rows()[0], elapsed_s=i, pack_a=0) for i in range(MAX_HISTORY + 10)]
        self.write(rows)
        history = self.store.snapshot(now=self.now)["history"]
        self.assertEqual(len(history), MAX_HISTORY)
        self.assertEqual(history[0]["elapsed_s"], 10)

    def test_energy_budget_separates_parked_load_from_driving_and_returned_energy(self):
        def row(t, current, speed):
            return dict(elapsed_s=t, pack_v=400, pack_a=current, speed_kph=speed)
        result = energy_budget([row(0, 10, 0), row(10, 10, 0),
                                row(20, 100, 50), row(30, -100, 50)])
        self.assertAlmostEqual(result["stationary_drawn_kwh"], 4 * 10 / 3600, places=5)
        # First moving interval averages 22 kW; second crosses from +40 to
        # -40, returning and drawing 0.02778 kWh each, not 0.05556 each.
        self.assertAlmostEqual(result["moving_drawn_kwh"], (22 * 10 + 100) / 3600, places=5)
        self.assertAlmostEqual(result["moving_regen_kwh"], 100 / 3600, places=5)
        self.assertEqual(result["observed_s"], 30)

    def test_energy_budget_preserves_gaps_and_missing_is_not_zero(self):
        row = dict(pack_v=400, pack_a=100, speed_kph=50)
        result = energy_budget([dict(row, elapsed_s=0), {"elapsed_s": 10},
                                dict(row, elapsed_s=20), dict(row, elapsed_s=500)])
        self.assertIsNone(result["moving_drawn_kwh"])
        self.assertIsNone(result["observed_s"])
        self.assertEqual(result["skipped_intervals"], 3)

    def test_stationary_energy_in_is_not_reported_as_regen(self):
        rows = [dict(elapsed_s=t, pack_v=400, pack_a=-20, speed_kph=0) for t in (0, 10)]
        result = energy_budget(rows)
        self.assertEqual(result["moving_regen_kwh"], 0)
        self.assertAlmostEqual(result["stationary_energy_in_kwh"], 8 * 10 / 3600, places=5)

    def test_http_is_read_only_and_never_serves_arbitrary_files(self):
        self.write(self.rows())
        server = make_server(self.store, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urlopen(base + "/api/snapshot", timeout=3) as response:
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
                self.assertEqual(json.load(response)["schema"], 1)
            for url, expected in (("/../../etc/passwd", 404),
                                  ("/api/snapshot?session=..%2Fsecret.csv", 400),
                                  ("/api/snapshot?session=x&session=y", 400),
                                  ("/api/snapshot?command=04", 400)):
                with self.subTest(url=url), self.assertRaises(HTTPError) as error:
                    urlopen(base + url, timeout=3)
                self.assertEqual(error.exception.code, expected)
            # One write path exists, and only one. A POST anywhere else is a
            # 404, and a POST to the write path from an unnamed origin -- or
            # with no origin at all, as here -- is refused outright.
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(base + "/api/snapshot", data=b"04", method="POST"), timeout=3)
            self.assertEqual(error.exception.code, 404)
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(base + "/api/places", data=b"{}", method="POST"), timeout=3)
            self.assertEqual(error.exception.code, 403)
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(base + "/api/snapshot", headers={"Host": "untrusted.example"}), timeout=3)
            self.assertEqual(error.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


class CostTests(unittest.TestCase):
    """What a snapshot costs, not only what it answers.

    A cold snapshot of a 573-row session took 7.1 seconds on the Pi Zero 2 W
    the dashboard runs on, against a browser that gives up after 4.5 -- so the
    session picker showed "No position in this session" for a trip that had
    526 GPS fixes.  Nothing was broken in a way any existing test could see:
    the answers were all correct, just too late to reach the page.  These
    tests are about the shape of the work, because that is the part that was
    wrong and the part that will regress silently again.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.rows = [
            {"utc": f"2026-01-01T12:00:{i:02d}Z", "elapsed_s": i * 10,
             "pack_v": 390 - i * 0.1, "pack_a": 20, "speed_kph": i % 50,
             "soc_pct": 80 - i * 0.01, "energy_kwh": 152 - i * 0.01}
            for i in range(30)
        ]

    def write(self, name):
        path = self.directory / name
        fields = list(dict.fromkeys(k for row in self.rows for k in row))
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fields)
            writer.writeheader()
            writer.writerows(self.rows)
        return path

    def count_sane(self):
        """Count `sane` calls, with the report stubbed out to isolate ours."""
        calls = []
        real = analyze.sane

        def counted(row):
            calls.append(id(row))
            return real(row)

        store = SessionStore(self.directory)
        with patch.object(analyze, "sane", counted), \
             patch.object(analyze, "analyze", return_value={}):
            store.snapshot("latest")
        return len(calls)

    def test_sanity_is_asked_once_per_row_not_once_per_field(self):
        # `sane` describes a row, so asking it per field was 55 identical
        # answers per row -- 89% of a cold snapshot.  The count must track the
        # row count alone; if it ever tracks the column count again, the
        # picker times out in the browser and nothing else complains.
        self.write("drive-20260101T120000Z.csv")
        columns = len(SessionStore(self.directory).columns)
        self.assertGreater(columns, 20, "a wide contract is the point of this test")
        # The dashboard walks the rows three times -- to build the report's
        # input, the history trail, and the energy budget -- and each pass may
        # ask about a row once.  Before this was fixed all three asked once per
        # *field*, so the count rose with the contract's width instead: 55 per
        # row here, 73 on the vehicle.  A count that scales with `columns` is
        # the regression; a fourth pass is a decision worth updating this for.
        count = self.count_sane()
        self.assertLessEqual(
            count, 3 * len(self.rows),
            f"{count / len(self.rows):.0f} sanity checks per row over "
            f"{columns} columns -- a pass is asking per field again",
        )

    def test_a_second_session_does_not_evict_the_one_being_watched(self):
        # The page polls `latest` every few seconds while a past trip is
        # selected.  With a single-entry cache the two evicted each other turn
        # by turn, so *every* poll paid the full cold cost and the dashboard
        # served a selected trip in 18 seconds rather than 0.07.
        self.write("drive-20260101T120000Z.csv")
        older = self.write("drive-20260101T110000Z.csv")
        os.utime(older, (1_760_000_000, 1_760_000_000))
        store = SessionStore(self.directory)
        reads = []
        real = analyze.read_session

        def counted(path, *a, **kw):
            reads.append(Path(path).name)
            return real(path, *a, **kw)

        with patch.object(analyze, "read_session", counted):
            for _ in range(3):
                store.snapshot("latest")
                store.snapshot("drive-20260101T110000Z.csv")
        self.assertEqual(sorted(set(reads)), sorted(set(reads)))
        self.assertEqual(len(reads), 2, f"re-read sessions already cached: {reads}")

    def test_the_cache_is_bounded_so_a_long_session_list_cannot_exhaust_memory(self):
        # 415 MiB of RAM total on the node, and a browser can ask for every
        # session in the picker in a row.
        names = [f"drive-202601{day:02d}T120000Z.csv" for day in range(1, CACHE_ENTRIES + 4)]
        for name in names:
            self.write(name)
        store = SessionStore(self.directory)
        for name in names:
            store.snapshot(name)
        self.assertLessEqual(len(store._cache), CACHE_ENTRIES)
        # The one just asked for is the one still held.
        self.assertIn(names[-1], [Path(key[0]).name for key in store._cache])


class CrossOriginTests(unittest.TestCase):
    """Who, exactly, may read this vehicle's data from a browser.

    The interface moved to a Home Assistant page, so the node has to answer a
    cross-origin reader. That is a real widening: a browser will hand any page
    on an allowed origin whatever this API says, and what it says includes
    where the truck is. So the allowlist is exact, echoed rather than
    wildcarded, and `*` is refused outright rather than warned about.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        rows = [{"utc": "2026-01-01T12:00:00Z", "elapsed_s": 0, "pack_v": 390,
                 "pack_a": 20, "speed_kph": 0, "soc_pct": 80, "energy_kwh": 152},
                {"utc": "2026-01-01T12:00:10Z", "elapsed_s": 10, "pack_v": 389,
                 "pack_a": 40, "speed_kph": 50, "soc_pct": 79, "energy_kwh": 150.1}]
        path = self.directory / "drive-20260101T120000Z.csv"
        fields = list(dict.fromkeys(k for r in rows for k in r))
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fields)
            writer.writeheader()
            writer.writerows(rows)

    def serve(self, **kwargs):
        store = SessionStore(self.directory, **kwargs)
        server = make_server(store, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def get(self, base, path, origin=None):
        headers = {"Origin": origin} if origin else {}
        return urlopen(Request(base + path, headers=headers), timeout=3)

    def test_a_wildcard_origin_is_refused_at_construction(self):
        # Not a warning, not a log line. There is no version of "any page may
        # read where this vehicle is" that is worth supporting.
        with self.assertRaises(ValueError):
            SessionStore(self.directory, allow_origins=("*",))

    def test_an_origin_without_a_scheme_is_refused(self):
        # "homeassistant.local:8123" never matches a browser's Origin header,
        # so accepting it would look configured and silently fail closed.
        for bad in ("homeassistant.local:8123", "null", "", "ftp://ha"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                SessionStore(self.directory, allow_origins=(bad,))

    def test_nothing_is_readable_cross_origin_by_default(self):
        base = self.serve()
        with self.get(base, "/api/snapshot", "http://evil.example") as r:
            self.assertIsNone(r.headers.get("Access-Control-Allow-Origin"))

    def test_a_named_origin_is_echoed_never_wildcarded(self):
        allowed = "http://homeassistant.local:8123"
        base = self.serve(allow_origins=(allowed,))
        with self.get(base, "/api/snapshot", allowed) as r:
            self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), allowed)
            # Without Vary, a shared cache could hand this response to a page
            # on a different origin along with its permission header.
            self.assertEqual(r.headers.get("Vary"), "Origin")

    def test_an_origin_that_was_not_named_gets_no_permission(self):
        base = self.serve(allow_origins=("http://homeassistant.local:8123",))
        for other in ("http://evil.example",
                      "https://homeassistant.local:8123",   # scheme differs
                      "http://homeassistant.local",          # port differs
                      "http://homeassistant.local:8123.evil.example"):
            with self.subTest(other=other), self.get(base, "/api/snapshot", other) as r:
                self.assertIsNone(r.headers.get("Access-Control-Allow-Origin"))

    def test_preflight_answers_only_for_a_named_origin(self):
        allowed = "http://homeassistant.local:8123"
        base = self.serve(allow_origins=(allowed,))
        request = Request(base + "/api/snapshot", method="OPTIONS",
                          headers={"Origin": allowed})
        with urlopen(request, timeout=3) as r:
            self.assertEqual(r.status, 204)
            self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), allowed)
            self.assertIn("GET", r.headers.get("Access-Control-Allow-Methods", ""))
        request = Request(base + "/api/snapshot", method="OPTIONS",
                          headers={"Origin": "http://evil.example"})
        with urlopen(request, timeout=3) as r:
            self.assertIsNone(r.headers.get("Access-Control-Allow-Origin"))

    def test_api_only_serves_data_and_not_a_second_copy_of_the_page(self):
        base = self.serve(api_only=True)
        with self.get(base, "/") as r:
            self.assertIn("application/json", r.headers.get("Content-Type", ""))
            body = json.load(r)
            self.assertEqual(body["service"], "hummer-obd")
            self.assertIn("/api/snapshot", body["endpoints"])
        # The data itself is unaffected -- this changes what is served, not
        # what is measured.
        with self.get(base, "/api/snapshot") as r:
            self.assertEqual(json.load(r)["schema"], 1)

    def test_the_page_is_still_served_when_it_was_not_turned_off(self):
        base = self.serve()
        with self.get(base, "/") as r:
            self.assertIn("text/html", r.headers.get("Content-Type", ""))


class HostGuardTests(unittest.TestCase):
    """Which hostnames this API may be reached under.

    do_GET refuses any Host it does not recognise, which is what stops a page
    on an attacker's domain from resolving that domain to this address and
    reading the answer -- DNS rebinding. CORS cannot do this job: it governs
    whether a browser hands the *response* to a page, and the guard has to
    refuse the request.

    The guard had no allowlist, and that made it refuse legitimate proxies
    too. `tailscale serve` forwards the original Host, so an API fronted at
    https://hummer.<tailnet>.ts.net arrived as that hostname and every request
    came back 403 -- with the proxy, the certificate and the CORS list all
    correct, and nothing in any log saying which of the four was wrong.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        rows = [{"utc": "2026-01-01T12:00:00Z", "elapsed_s": 0, "pack_v": 390,
                 "pack_a": 20, "speed_kph": 0, "soc_pct": 80, "energy_kwh": 152}]
        path = self.directory / "drive-20260101T120000Z.csv"
        fields = list(dict.fromkeys(k for r in rows for k in r))
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fields)
            writer.writeheader()
            writer.writerows(rows)

    def serve(self, **kwargs):
        store = SessionStore(self.directory, **kwargs)
        server = make_server(store, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def get(self, base, host=None):
        headers = {"Host": host} if host else {}
        return urlopen(Request(base + "/api/snapshot", headers=headers), timeout=3)

    def test_an_unnamed_host_is_still_refused(self):
        # The whole point of the guard. Adding an allowlist must not turn it
        # into a formality.
        base = self.serve()
        with self.assertRaises(HTTPError) as error:
            self.get(base, "attacker.example")
        self.assertEqual(error.exception.code, 403)

    def test_a_named_host_is_accepted(self):
        base = self.serve(allow_hosts=("hummer.example.ts.net",))
        with self.get(base, "hummer.example.ts.net") as response:
            self.assertEqual(json.load(response)["schema"], 1)

    def test_naming_one_host_does_not_admit_another(self):
        # An allowlist that leaks past its entries is not an allowlist.
        base = self.serve(allow_hosts=("hummer.example.ts.net",))
        for bad in ("attacker.example", "hummer.example.ts.net.evil.example",
                    "evil.hummer.example.ts.net"):
            with self.subTest(bad=bad), self.assertRaises(HTTPError) as error:
                self.get(base, bad)
            self.assertEqual(error.exception.code, 403)

    def test_the_listener_and_localhost_still_work_with_no_allowlist(self):
        # The default deployment must not need the new flag.
        base = self.serve()
        with self.get(base) as response:
            self.assertEqual(json.load(response)["schema"], 1)

    def test_a_wildcard_host_is_refused_at_construction(self):
        # Same reasoning as allow_origins: there is no safe version of "any
        # hostname may reach the API that answers with the vehicle's position".
        with self.assertRaises(ValueError):
            SessionStore(self.directory, allow_hosts=("*",))

    def test_a_host_that_could_never_match_is_refused_at_construction(self):
        # The Host header is compared with the port already stripped, so an
        # entry carrying one would never match anything and would look
        # configured while silently failing closed -- the same trap
        # allow_origins avoids by refusing a scheme-less origin.
        for bad in ("hummer.example.ts.net:443", "https://hummer.example.ts.net",
                    "hummer.example.ts.net/api", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                SessionStore(self.directory, allow_hosts=(bad,))

    def test_the_allowlist_does_not_widen_who_may_read_cross_origin(self):
        # Two separate gates. Reaching the API under a permitted hostname says
        # nothing about which pages may read the answer, and a reader that
        # confused them would hand the vehicle's position to any origin that
        # knew the proxy's name.
        base = self.serve(allow_hosts=("hummer.example.ts.net",))
        with self.get(base, "hummer.example.ts.net") as response:
            self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))


class SessionListTests(unittest.TestCase):
    """Which recordings are journeys, so the picker can say so.

    On the vehicle node 51 of 66 recorded sessions contain no movement: the
    truck wakes by itself every couple of hours and the recorder faithfully
    writes a few hundred rows of it sitting still. Correct behaviour, and
    useless in a menu that offered all 66 with nothing to distinguish them.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def write(self, name, rows):
        path = self.directory / name
        fields = list(dict.fromkeys(k for row in rows for k in row))
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fields)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def entry(self, name):
        store = SessionStore(self.directory)
        return [e for e in store.sessions()["sessions"] if e["id"] == name][0]

    def test_a_parked_session_is_marked_parked(self):
        self.write("drive-20260101T120000Z.csv",
                   [{"utc": f"2026-01-01T12:00:{i:02d}Z", "elapsed_s": i,
                     "speed_kph": "0", "odometer_km": "1000.0"} for i in range(20)])
        entry = self.entry("drive-20260101T120000Z.csv")
        self.assertFalse(entry["moved"])
        self.assertEqual(entry["km"], 0.0)

    def test_a_journey_reports_its_distance(self):
        self.write("drive-20260101T130000Z.csv",
                   [{"utc": f"2026-01-01T13:00:{i:02d}Z", "elapsed_s": i,
                     "speed_kph": str(i * 4), "odometer_km": str(1000 + i * 0.4)}
                    for i in range(20)])
        entry = self.entry("drive-20260101T130000Z.csv")
        self.assertTrue(entry["moved"])
        self.assertAlmostEqual(entry["km"], 7.6, places=1)

    def test_speed_alone_is_enough_to_be_a_journey(self):
        # Module 17 goes quiet often enough that a real trip can record speed
        # with the odometer never answering. Requiring both would file that
        # trip under "parked".
        self.write("drive-20260101T140000Z.csv",
                   [{"utc": f"2026-01-01T14:00:{i:02d}Z", "elapsed_s": i,
                     "speed_kph": "55"} for i in range(5)])
        entry = self.entry("drive-20260101T140000Z.csv")
        self.assertTrue(entry["moved"])
        self.assertIsNone(entry["km"])

    def test_odometer_alone_is_enough_too(self):
        # And the reverse: a crawl that never exceeds the speed threshold, or
        # a session whose speed column is empty, still moved if the odometer
        # says it did.
        self.write("drive-20260101T150000Z.csv",
                   [{"utc": f"2026-01-01T15:00:{i:02d}Z", "elapsed_s": i,
                     "odometer_km": str(1000 + i * 0.5)} for i in range(10)])
        self.assertTrue(self.entry("drive-20260101T150000Z.csv")["moved"])

    def test_the_verdict_is_not_recomputed_for_a_file_that_has_not_changed(self):
        # A finished session never changes, and the picker is refreshed on
        # every poll. Re-reading every CSV every five seconds on a Pi Zero
        # would cost more than everything else the page does.
        self.write("drive-20260101T160000Z.csv",
                   [{"utc": "2026-01-01T16:00:00Z", "elapsed_s": 0,
                     "speed_kph": "40", "odometer_km": "1000"}])
        store = SessionStore(self.directory)
        store.sessions()
        opens = []
        real = Path.open

        def counted(self, *a, **kw):
            opens.append(self.name)
            return real(self, *a, **kw)

        with patch.object(Path, "open", counted):
            store.sessions()
        self.assertEqual([o for o in opens if o.endswith(".csv")], [])

    def test_an_unreadable_session_is_not_silently_called_parked(self):
        # Failing to read a file says nothing about whether the vehicle moved,
        # and filing it under "parked" would hide a real trip.
        #
        # The first version of this test wrote a few bytes of binary and
        # expected a failure. It got a verdict instead: the csv module reads
        # almost anything as one strange row and raises nothing, so the file
        # was judged "parked" and the test passed for the wrong reason. The
        # failure has to be a real one.
        self.write("drive-20260101T170000Z.csv",
                   [{"utc": "2026-01-01T17:00:00Z", "elapsed_s": 0,
                     "speed_kph": "70", "odometer_km": "1000"}])
        store = SessionStore(self.directory)
        real = Path.open

        def refuse(self, *a, **kw):
            if self.suffix == ".csv":
                raise OSError("the card went away")
            return real(self, *a, **kw)

        with patch.object(Path, "open", refuse):
            entry = [e for e in store.sessions()["sessions"]
                     if e["id"] == "drive-20260101T170000Z.csv"][0]
        self.assertIsNone(entry["moved"],
                          "not knowing was reported as knowing it stood still")
        self.assertIsNone(entry["km"])

    def test_a_file_of_nonsense_is_not_mistaken_for_a_journey(self):
        # The other side of the same coin: the csv module will parse binary
        # rubbish without complaint, and nothing in it looks like speed or an
        # odometer, so the honest verdict is that it did not move.
        path = self.write("drive-20260101T180000Z.csv",
                          [{"utc": "2026-01-01T18:00:00Z", "elapsed_s": 0}])
        path.write_bytes(b"\xff\xfe not a csv at all")
        self.assertFalse(self.entry("drive-20260101T180000Z.csv")["moved"])


#: Columns whose part index entry names a part, where the page genuinely
#: conveys the reading without ever mentioning the column by name. Every entry
#: needs the reason written beside it.
#:
#: THIS DICT IS A RATCHET. It may shrink -- that is what wiring a signal to the
#: model looks like -- and it may never grow without a sentence here saying why
#: the reading reaches the viewer under some other name. A part index entry is
#: a promise that something on the truck moves; an entry added here without a
#: reason is that promise quietly withdrawn.
INDIRECT = {
    "dmc2_v": "one of three 12 V sense points on the single lv part, which does "
              "not distinguish it from its siblings",
    "mod17_v": "one of three 12 V sense points on the single lv part, which does "
               "not distinguish it from its siblings",
    "pack_v_1d": "a real reading the single HV run part does not distinguish "
                 "from its siblings",
    "gps_mode": "consumed by the map through the history array, not by column name",
    "gps_time": "consumed by the map through the history array, not by column name",
    "odometer_km": "reaches the scene as distance through the anchoring pass, "
                   "not by name",
}


#: Parts where two or more columns genuinely claim the same effect, because
#: the part really does carry all of them and the page really does read them
#: all. Each needs the reason written beside it.
#:
#: THIS DICT IS A RATCHET, the same way INDIRECT is. Two columns claiming one
#: effect on one part is normally a false index entry: only one of them can be
#: the reading that part draws. An entry added here without a reason is that
#: check switched off.
SHARED_EFFECT = {
    ("motor-*", "all three drive units together"):
        "hv_power_kw and power_kw are the same bus power by two routes, and "
        "the motors glow from whichever one answered",
    ("lv", "12 v rail"):
        "four sense points on one rail, all drawn on the single lv part -- "
        "they are there to disagree with each other, which needs all four",
    ("shell", "warning lamp"):
        "mil_on and dtc_count are module 17's two views of one lamp, and the "
        "lamp lights on either",
}


class PartIndexTests(unittest.TestCase):
    """The page claims where every signal is shown. That claim must stay true.

    This is the fourth hand-kept inventory in this project. The README's column
    count, the drive unit's identifier list and the enhanced-identifier registry
    all drifted from the code before a test was put on them. The part index is
    the same shape of promise -- one entry per recorded column -- so it gets the
    same treatment before rather than after it goes stale.
    """

    PAGE = Path(__file__).resolve().parents[1] / "src" / "hummer_obd" / "dashboard.html"

    def entries(self):
        page = self.PAGE.read_text(encoding="utf-8")
        start = page.index("var SIGNAL_PART = {")
        block = page[start:page.index("\n  };", start)]
        return re.findall(r"^\s{4}(\w+):\s*\[", block, re.M)

    def test_every_recorded_column_says_where_it_is_shown(self):
        mapped = self.entries()
        columns = set(public_columns(True))
        missing = sorted(columns - set(mapped))
        self.assertEqual(
            missing, [],
            "columns the page would render with no entry in the part index -- "
            "the 'Shown on' cell falls back to 'unmapped', which is a question "
            f"the page should answer rather than ask: {missing}",
        )

    def test_the_index_does_not_name_columns_that_do_not_exist(self):
        stray = sorted(set(self.entries()) - set(public_columns(True)))
        self.assertEqual(
            stray, [],
            f"part index entries for columns nothing records any more: {stray}",
        )

    def test_a_column_that_is_not_drawn_sits_with_the_ones_that_are_not(self):
        # The table is read by section headings, and an entry filed under the
        # wrong one is a heading that lies. `dist_since_chg_mi` resolved to
        # null -- recorded, deliberately not drawn -- while sitting in the
        # "traction pack" block beside the columns that ARE drawn on the pack,
        # after `regen_field_raw` and `field_4127_raw` were moved out of it
        # for exactly that reason. Nothing caught it, because every other test
        # here reads the entries and none read where they sit.
        page = self.PAGE.read_text(encoding="utf-8")
        start = page.index("var SIGNAL_PART = {")
        block = page[start:page.index("\n  };", start)]
        marker = block.index("recorded, deliberately not drawn")
        stray = re.findall(r"^\s{4}(\w+):\s*\[\s*null", block[:marker], re.M)
        self.assertEqual(
            stray, [],
            "these columns are indexed as not drawn while filed under a "
            f"section that says where things are drawn: {stray}",
        )

    def test_every_layer_a_part_names_has_an_opacity(self):
        # A layer missing from the renderer's opacity map resolves to
        # undefined, which becomes a NaN alpha, and the part simply does not
        # draw -- no error, no warning, just absent geometry. This is the same
        # shape of failure as an unguarded reading reaching the shader.
        page = self.PAGE.read_text(encoding="utf-8")
        declared = {
            entry.split(":")[0].strip()
            for entry in re.search(r"layers = \{([^}]*)\}", page).group(1).split(",")
            if ":" in entry
        }
        used = set(re.findall(r'layer:\s*"(\w+)"', page))
        self.assertEqual(
            sorted(used - declared), [],
            "parts name a layer the renderer has no opacity for, so they "
            "would silently fail to draw",
        )

    def test_every_layer_can_be_toggled(self):
        # A layer with no button is a part of the vehicle a viewer cannot get
        # out of the way, which is the entire purpose of the cutaway.
        page = self.PAGE.read_text(encoding="utf-8")
        block = page[page.index("var LAYER_LABELS = ["):]
        labelled = set(re.findall(r'\["(\w+)",\s*"[^"]+"\]',
                                  block[:block.index("];")]))
        used = set(re.findall(r'layer:\s*"(\w+)"', page))
        self.assertEqual(sorted(used - labelled), [],
                         "layers with no toggle button")

    def test_each_column_is_claimed_once(self):
        mapped = self.entries()
        duplicates = sorted({name for name in mapped if mapped.count(name) > 1})
        self.assertEqual(duplicates, [], f"two answers for one signal: {duplicates}")

    def test_every_replayable_column_is_read_through_the_frame(self):
        # The defect this guards is not a crash and not a wrong colour: the
        # part simply shows the session's LAST sample while the scrub marker
        # walks the trip, and it looks entirely plausible. The module map did
        # it for as long as the map existed, because its read was `sig()` only
        # AND sat above `var f = state.frame` -- where `f` is hoisted but
        # undefined, so even adding atFrameRaw in place would have returned
        # the fallback every time and kept the freeze.
        page = self.PAGE.read_text(encoding="utf-8")
        frame = page.index("var f = state.frame;")
        for column in ("array_2af1", "field_2429_raw", "cell_min_v",
                       "cell_avg_v", "cell_max_v", "cell_spread_mv",
                       "wheel_fl_kph", "wheel_fr_kph", "wheel_rl_kph",
                       "wheel_rr_kph"):
            with self.subTest(column=column):
                # `carriedRaw` is the third frame accessor: same rule, plus
                # the carry-forward the delta-encoded columns need.
                reads = [m.start() for m in re.finditer(
                    r'(?:atFrame|atFrameRaw|carriedRaw)\("%s"' % column, page)]
                self.assertTrue(
                    reads,
                    f"{column} travels in the history so the scene can follow "
                    "a replay, and the page reads it off the live snapshot "
                    "only -- so it freezes at the session's last sample while "
                    "the marker moves",
                )
                self.assertGreater(
                    min(reads), frame,
                    f"{column} is read above `var f = state.frame`, where f is "
                    "hoisted but undefined -- atFrame would silently return "
                    "its fallback forever",
                )

    def named(self):
        """Entries that name a part, as (column, part), and the rest of the page.

        The same slice boundaries `entries` uses, so the two cannot disagree
        about where the table ends.
        """
        page = self.PAGE.read_text(encoding="utf-8")
        start = page.index("var SIGNAL_PART = {")
        end = page.index("\n  };", start)
        pairs = re.findall(r'^\s{4}(\w+):\s*\[\s*"([^"]*)"',
                           page[start:end], re.M)
        return pairs, page[:start] + page[end:]

    def test_a_claim_of_being_drawn_must_name_a_column_the_page_reads(self):
        # PartIndexTests has always checked that the table is COMPLETE. It
        # never checked that the table is TRUE: an entry naming a part is a
        # statement that the page reads that column, and twelve of them said
        # so while no line outside this table mentioned the column at all.
        # The three raw thermal fields drifted exactly this way once already
        # (see the note at dashboard.html's coolant1 line).
        #
        # WHAT THIS CHECKS AND WHAT IT DOES NOT. It checks that the COLUMN is
        # read somewhere outside the index. It does not check that the PART
        # named is the thing that reads it, because nothing looks a part up
        # by that string at runtime and the link cannot be followed from the
        # source. So it is necessary and not sufficient, and it missed a real
        # one: `range_mi` was indexed to the pack case, whose fill reads
        # energy_kwh, while range_mi is only ever drawn on the driver's
        # cluster -- the column WAS read, just not there. That class is
        # covered by test_two_columns_may_not_claim_the_same_effect below,
        # which catches the shape it takes in practice: two columns claiming
        # to be the same thing on the same part, where only one can be.
        pairs, elsewhere = self.named()
        silent = sorted(column for column, _ in pairs
                        if column not in elsewhere and column not in INDIRECT)
        self.assertEqual(
            silent, [],
            "the part index says these columns are drawn on the truck, and no "
            "line outside the index reads them -- wire them to the model, or "
            "move them to the 'recorded, deliberately not drawn' section, or "
            "add each one to INDIRECT in this file WITH THE REASON. That dict "
            f"is a ratchet: it may shrink, never grow unexplained: {silent}",
        )
        # The ratchet only ratchets if slack is given back. An excuse for a
        # column the index no longer claims, or for one the page now reads by
        # name, has to come out -- otherwise the dict quietly becomes the
        # permanent exemption list it exists to prevent.
        named = {column for column, _ in pairs}
        stale = sorted(column for column in INDIRECT
                       if column not in named or column in elsewhere)
        self.assertEqual(
            stale, [],
            "INDIRECT still excuses columns that no longer need excusing -- "
            "either the index stopped claiming them or the page now reads "
            f"them by name. Delete these entries: {stale}",
        )

    def test_two_columns_may_not_claim_the_same_effect(self):
        # The false index entry has a shape, and this is it: a second column
        # claiming the effect a first column already claims on the same part.
        # Only one of them can be the thing the page reads, so the other is
        # the index promising something the truck does not do. `range_mi` and
        # `dist_since_chg_mi` were both indexed to "pack-case, pack charge
        # level" beside `energy_kwh`, which is what the pack case actually
        # reads.
        #
        # Scene geometry only. A DOM target like "#map" is one panel showing
        # many things at once, and several columns landing on it with the
        # same short description is what a map legitimately looks like.
        pairs, _ = self.named()
        page = self.PAGE.read_text(encoding="utf-8")
        start = page.index("var SIGNAL_PART = {")
        table = page[start:page.index("\n  };", start)]
        described = re.findall(
            r'^\s{4}(\w+):\s*\[\s*"([^"]*)"\s*,\s*((?:"[^"]*"\s*\+?\s*)+)\]',
            table, re.M | re.S)
        self.assertEqual(len(described), len(pairs),
                         "an entry's description could not be read")
        claims = {}
        for column, part, description in described:
            if part.startswith("#") or part.startswith("."):
                continue
            text = "".join(re.findall(r'"([^"]*)"', description))
            # The effect is the first clause: what the part is said to do.
            # Everything after the first dash, comma, bracket or full stop is
            # the caveat on it, and two entries may qualify one claim
            # differently while still making the same claim.
            effect = re.split(r"[—(,.;]", text)[0].strip().lower()
            claims.setdefault((part, effect), []).append(column)
        clashes = sorted((key, sorted(columns))
                         for key, columns in claims.items()
                         if len(columns) > 1 and key not in SHARED_EFFECT)
        self.assertEqual(
            clashes, [],
            "these columns claim the same effect on the same part, and only "
            "one of them can be the reading the page draws there -- point the "
            "others at the part that really draws them, null them with the "
            "reason, or add the pair to SHARED_EFFECT in this file WITH THE "
            f"REASON: {clashes}",
        )
        # The same ratchet as INDIRECT: an exemption that no longer excuses
        # anything has to come out, or the dict becomes a permanent licence.
        stale = sorted(key for key in SHARED_EFFECT
                       if len(claims.get(key, [])) < 2)
        self.assertEqual(
            stale, [],
            f"SHARED_EFFECT still excuses pairs that no longer clash: {stale}",
        )

    def test_a_named_part_must_exist(self):
        # An id that no part carries fails the way a bad layer does: silently.
        # Nothing looks a part up by this string at runtime, so the index can
        # name geometry that was renamed or never existed and the page still
        # renders -- it just answers "where is this on the truck" with a place
        # that is not there.
        pairs, _ = self.named()
        page = self.PAGE.read_text(encoding="utf-8")
        # Part ids are literals, but several families are built by
        # concatenation -- `id: "tyre-" + w[0]` -- so a literal ending in a
        # dash stands for every id that extends it.
        ids = set(re.findall(r'id:\s*"([^"]*)"', page))
        prefixes = {i for i in ids if i.endswith("-")}
        # A whole layer is a legitimate answer for a signal that moves the
        # whole model rather than one part.
        layers = {entry.split(":")[0].strip()
                  for entry in re.search(r"layers = \{([^}]*)\}", page).group(1).split(",")
                  if ":" in entry}

        def exists(part):
            if part.startswith("#") or part.startswith("."):
                return True          # a DOM selector, not scene geometry
            if "-*" in part:
                head = part.split("-*")[0]
                return any(i.startswith(head) for i in ids)
            return (part in ids or part in layers
                    or any(part.startswith(p) for p in prefixes))

        missing = sorted((column, part) for column, part in pairs if not exists(part))
        self.assertEqual(
            missing, [],
            "the part index sends a viewer to geometry the scene does not "
            f"build: {missing}",
        )


@unittest.skipIf(NODE is None, "node is not installed on this machine")
class ReadingRuleTests(unittest.TestCase):
    """A visual may not assert a value the page cannot stand behind.

    Four defects in one review had the same shape: the datum was missing,
    stale, out of range or unscaled, and the page drew a confident number
    anyway. None of them raised anything, none of them looked wrong, and each
    one needed a specific arrangement of readings to show itself. So the rules
    that decide "draw or withhold" are run here against exactly those
    arrangements, out of the page's own source.
    """

    def signals(self, **corners):
        """A snapshot `signals` map, from corner -> (value, status)."""
        return {"wheel_%s_kph" % name: {"value": value, "status": status}
                for name, (value, status) in corners.items()}

    # -- corner tint ------------------------------------------------------
    def test_a_stale_corner_tints_no_tyre_at_all(self):
        # `signals[c].value` is nulled when the node judges a reading INVALID
        # and NOT when it judges it merely STALE, so a corner that stopped
        # answering keeps its last number for as long as the page is open.
        # The null guard cannot catch that -- the value is a number, just an
        # old one -- and the result is not a missing tint but an invented
        # fault: three healthy corners pulled above a mean a dead corner
        # dragged down, so the dead one reads "dragging" and the three good
        # ones read "spinning". Nothing on screen says which corner is silent.
        deviation = run_reading_rules(
            "return cornerDeviation(null, %s);" % json.dumps(self.signals(
                fl=(60.0, "fresh"), fr=(60.0, "fresh"),
                rr=(60.0, "fresh"), rl=(20.0, "stale"))))
        self.assertIsNone(
            deviation,
            "a corner that stopped reporting still carries its last number, "
            "and averaging it in tints three healthy tyres as spinning and "
            "the dead one as dragging",
        )

    def test_four_fresh_corners_do_tint(self):
        # The guard above must not have simply turned the tint off.
        deviation = run_reading_rules(
            "return cornerDeviation(null, %s);" % json.dumps(self.signals(
                fl=(60.0, "fresh"), fr=(60.0, "fresh"),
                rl=(56.0, "fresh"), rr=(60.0, "fresh"))))
        self.assertEqual(sorted(deviation), ["fl", "fr", "rl", "rr"])
        self.assertAlmostEqual(deviation["rl"], -3.0)
        self.assertAlmostEqual(deviation["fl"], 1.0)

    def test_a_replayed_frame_needs_no_freshness_test(self):
        # A history row IS the reading taken at that instant; its age is where
        # the scrub is sitting and the page says so elsewhere. Applying a
        # freshness rule to it would blank the tint for every replay.
        deviation = run_reading_rules(
            'return cornerDeviation({"wheel_fl_kph": 60, "wheel_fr_kph": 60,'
            ' "wheel_rl_kph": 56, "wheel_rr_kph": 60}, {});')
        self.assertAlmostEqual(deviation["rl"], -3.0)

    def test_corners_below_a_walking_pace_tint_nothing(self):
        self.assertIsNone(run_reading_rules(
            'return cornerDeviation({"wheel_fl_kph": 4, "wheel_fr_kph": 4,'
            ' "wheel_rl_kph": 2, "wheel_rr_kph": 4}, {});'))

    # -- cell envelope ----------------------------------------------------
    def test_the_envelope_is_hidden_rather_than_drawn_from_nothing(self):
        # The three bounds are no longer drawn -- see
        # CellEnvelopeRemovalTests -- but they are still PRINTED in the
        # caption, and printing them is as much a claim as drawing them was.
        # So the same rule holds: all three or none.
        for name, args in (("no minimum", "null, 3.99, 4.01"),
                           ("no average", "3.98, null, 4.01"),
                           ("no maximum", "3.98, 3.99, null"),
                           ("nothing at all", "null, null, null")):
            with self.subTest(name):
                self.assertIsNone(run_reading_rules(
                    "return cellEnvelope(%s);" % args))

    def test_an_impossible_envelope_is_not_printed(self):
        # No pack has its lowest cell above its average. Three numbers in that
        # order are a torn read or a decode fault, and printing them states a
        # pack condition that cannot exist with nothing saying so.
        self.assertIsNone(run_reading_rules("return cellEnvelope(4.05, 3.99, 4.01);"))
        self.assertIsNone(run_reading_rules("return cellEnvelope(3.98, 4.05, 4.01);"))
        self.assertIsNotNone(run_reading_rules("return cellEnvelope(3.98, 3.99, 4.01);"))

    def test_a_cell_far_off_the_old_axis_is_still_reported(self):
        # The three marks stood on a fixed 3.0-4.2 V axis, so a 2.4 V cell had
        # to be pinned to the bottom of it and flagged, or it drew in exactly
        # the place a healthy 3.0 V one did. With the marks gone and the
        # numbers printed instead there is no axis to run off: 2.400 V reads
        # as 2.400 V, which is what digits are for.
        low = run_reading_rules("return cellEnvelope(2.4, 3.90, 4.00);")
        self.assertEqual([low["min"], low["avg"], low["max"]], [2.4, 3.90, 4.00])
        high = run_reading_rules("return cellEnvelope(3.98, 4.10, 4.35);")
        self.assertEqual(high["max"], 4.35)

    # -- effort scale -----------------------------------------------------
    def peaking_at(self, counts):
        """A snapshot whose history peaks at *counts* above the measured zero."""
        return json.dumps({"history": [
            {"field_2429_raw": "%04X" % (live.TORQUE_ZERO + value)}
            for value in (0, counts // 2, counts)]})

    # Both of the next two run their whole sequence inside ONE node process,
    # because the defect they guard is state surviving between calls -- run a
    # call per process and the process boundary does the resetting the page
    # was supposed to do, and the test passes against the broken code.
    def test_the_effort_scale_does_not_carry_across_sessions(self):
        # It was a running maximum on `state`, which is a page-lifetime fact
        # wearing a session's clothes: after viewing a drive that peaked at
        # 8631 counts, a gentle drive peaking at 400 rendered every halfshaft
        # at 4.6% brightness with nothing on screen saying why. Recomputed
        # from the snapshot, the second session cannot see the first's peak --
        # which is what `rawFraction` has always done and what the comment on
        # this scale used to claim without doing.
        hard, gentle = run_reading_rules(
            "state.snapshot = " + self.peaking_at(8631) + ";\n"
            "var hard = effortScale(8631);\n"
            # Exactly what the session-select handler does to `state`.
            "state.snapshot = null;\n"
            "state.snapshot = " + self.peaking_at(400) + ";\n"
            "return [hard, effortScale(400)];",
            {"snapshot": None})
        self.assertEqual(hard, 8631)
        self.assertEqual(
            gentle, 400,
            "the halfshaft scale carried a previous session's peak into this "
            "one, so a gentle drive renders every shaft dim for a reason "
            "nothing on the page states",
        )

    def test_the_effort_scale_does_not_depend_on_where_the_scrub_has_been(self):
        # Same session, same frame, two scrub histories: one frame must render
        # one way. Accumulated, the answer depended on whether the hard part
        # of the drive had already been played.
        before, after = run_reading_rules(
            "state.snapshot = " + self.peaking_at(8631) + ";\n"
            "var before = effortScale(400);\n"
            "effortScale(8631);\n"
            "return [before, effortScale(400)];",
            {"snapshot": None})
        self.assertEqual(before, after)
        self.assertEqual(before, 8631)

    def test_a_flat_drive_keeps_a_floor_under_its_scale(self):
        # Otherwise a drive that never left the neutral band gets its own
        # quantisation noise stretched across the whole brightness range.
        self.assertEqual(
            run_reading_rules("return effortScale(4);",
                              {"snapshot": json.loads(self.peaking_at(4))}), 200)

    def test_the_effort_scale_is_not_kept_on_state(self):
        # The grep is the cheap half of the two tests above: a scale that is
        # written back to `state` is by construction a page-lifetime fact.
        # Asserted as a boolean rather than with assertNotIn, whose failure
        # message would print the entire page script.
        self.assertFalse(
            "state.effortPeak" in page_script(),
            "a scale accumulated on `state` outlives the session it describes",
        )

    # -- what the default view actually renders ---------------------------
    def test_the_default_view_is_the_one_the_contrast_floor_acts_in(self):
        # Every "as rendered" assertion below passes `true` for the floor's
        # `shellHidden` argument, and that is only honest if body-off is the
        # ORDINARY frame rather than a corner of the view menu. It is:
        # `layers.shell` is 0 at init and nothing turns it on.
        source = page_script()
        found = re.search(r"layers = \{ shell: (\d)", source)
        self.assertTrue(found, "the layer defaults are no longer readable")
        self.assertEqual(found.group(1), "0",
                         "the body is no longer off by default, so the "
                         "contrast floor is no longer the default view's "
                         "last word and these tests are asserting a view "
                         "nobody sees")

    def test_the_contrast_floor_lifts_decoration_and_not_readings(self):
        # The floor is the pass that has the last word over three earlier
        # ones, so the exemption list IS the guarantee that a reading part's
        # unlit state survives to the screen. Both halves are checked here:
        # that the floor still does its job, and that it keeps its hands off
        # the parts whose unlit state is a measurement.
        floor = page_constants()["CONTRAST_FLOOR"]
        self.assertEqual(as_rendered("[0, 0, 0]", {"layer": "drive"}), floor,
                         "an ordinary internal part with nothing to report "
                         "is lost against the void")
        # Already lit by a measurement: left alone, because that is
        # information and this is only contrast.
        self.assertEqual(as_rendered("[0.4, 0.1, 0]", {"layer": "drive"}),
                         [0.4, 0.1, 0])
        # Body on: there is no void to be lost against.
        self.assertEqual(
            run_reading_rules('return contrastFloor([0, 0, 0],'
                              ' {layer: "drive"}, false);'), [0, 0, 0])
        # The body and the cabin are what is being seen through.
        for layer in ("shell", "cabin"):
            with self.subTest(layer):
                self.assertEqual(as_rendered("[0, 0, 0]", {"layer": layer}),
                                 [0, 0, 0])
        # And the reading parts, which is the point of the whole function.
        for name, part in (("tyre", TYRE_PART), ("halfshaft", SHAFT_PART),
                           ("spread band", {"layer": "pack",
                                            "spreadBand": True})):
            with self.subTest(name):
                self.assertEqual(
                    as_rendered("[0, 0, 0]", part), [0, 0, 0],
                    "the floor overwrites a reading part's unlit state, so "
                    "'nothing was measured' renders as a dim grey a shade "
                    "off the measured neutral",
                )

    # -- the halfshaft colour ---------------------------------------------
    def test_no_effort_reading_does_not_look_like_a_measured_neutral(self):
        # THE DEFECT: `live.effort === null` left the shafts at [0, 0, 0] and
        # so did `dir === "neutral"`, so the two were the same picture, and
        # both then picked up the same contrast floor. On a halfshaft that
        # picture reads as coasting -- a statement about the truck made out
        # of a gap in the log. And it is the common case, not a corner: the
        # field answered in 2,756 of 10,682 recorded rows, in 20 of 63
        # sessions, with runs of up to 198 rows carrying nothing, which is
        # about 18 s of playback in the middle of an acceleration.
        #
        # AND IT IS ASSERTED AS RENDERED. An earlier version of this test
        # compared the two values `effortEmissive` returns and stopped there,
        # which is not what the viewer sees: draw() runs a contrast floor over
        # the emissive afterwards, in the DEFAULT view, and that floor used to
        # replace an absent shaft's [0, 0, 0] with its own grey. The two
        # states went back to being two shades of one grey the moment they
        # were drawn, and this test passed green through all of it. So both
        # values go through `contrastFloor` before anything is claimed.
        measured = 'effortEmissive({counts: 4, dir: "neutral", scale: 8631})'
        absent = as_rendered("effortEmissive(null)", SHAFT_PART)
        neutral = as_rendered(measured, SHAFT_PART)
        self.assertNotEqual(
            absent, neutral,
            "a shaft with no reading and a shaft measured inside the neutral "
            "band render identically, so the page states 'coasting' where it "
            "measured nothing",
        )
        self.assertEqual(
            absent, [0, 0, 0],
            "an absent effort reading does not render unlit -- the page, the "
            "legend swatch and the caption all say unlit, and the floor says "
            "otherwise last",
        )
        # The measured neutral is the colour the rule chose, not a floored
        # version of it: EFFORT_NEUTRAL clears the floor on its own.
        self.assertEqual(neutral, page_constants()["EFFORT_NEUTRAL"])
        # THE MARGIN. The shaft is opaque, so the emissive difference is the
        # difference in the output pixel. It has to be at least the lift the
        # page itself calls the minimum that makes a part legible against the
        # void -- CONTRAST_FLOOR. Anything less and "unlit" and "measured
        # neutral" are a distinction only a colour picker can make.
        lift = page_constants()["CONTRAST_FLOOR"][0]
        self.assertGreaterEqual(
            max(n - a for n, a in zip(neutral, absent)), lift,
            "absent and measured-neutral are closer together on screen than "
            "the page's own floor for 'visible at all'",
        )

    def test_a_faint_measured_effort_is_not_mistaken_for_no_reading(self):
        # The same defect at the other end of the scale: 31 counts against a
        # session peaking at 8631 is 0.4% of the ramp, which is unlit. A
        # direction is itself a reading, so any direction has to be legible.
        for direction in ("drive", "regen"):
            with self.subTest(direction):
                faint = run_reading_rules(
                    'return effortEmissive({counts: 31, dir: "%s",'
                    ' scale: 8631});' % direction)
                self.assertGreater(sum(faint), 0.06)
                self.assertNotEqual(faint, run_reading_rules(
                    "return effortEmissive(null);"))
        # Without turning the ramp off: hard effort still reads harder.
        soft = run_reading_rules(
            'return effortEmissive({counts: 500, dir: "drive", scale: 8631});')
        hard = run_reading_rules(
            'return effortEmissive({counts: 8631, dir: "drive", scale: 8631});')
        self.assertGreater(sum(hard), sum(soft))

    # -- the cell spread tint ---------------------------------------------
    def test_a_spread_past_the_top_of_the_ramp_is_flagged_not_pinned(self):
        # A reading above the top anchor clamped to full amber would say
        # "7.6 mV" over a 12 mV pack -- the same defect as clamping an
        # off-axis cell to the end of the voltage axis. 1.0% of the recorded
        # readings land here, so this is a state the page really enters.
        page = page_constants()
        top = run_reading_rules("return cellSpreadEmissive(%r);"
                                % page["CELL_SPREAD_HIGH_MV"])
        over = run_reading_rules("return cellSpreadEmissive(%r);"
                                 % (page["CELL_SPREAD_HIGH_MV"] + 0.1))
        self.assertNotEqual(
            top, over,
            "a spread past the top of the ramp renders exactly as a spread "
            "at the top of it, so the tint asserts a value it does not have",
        )
        self.assertEqual(over, page["CELL_SPREAD_OVER"])
        # Absent has no colour at all, which is stronger than having its own:
        # draw() returns before the band is drawn when there is no reading, so
        # there is no "no spread" branch left in `cellSpreadEmissive` to
        # assert against. This test used to keep one alive -- a resting colour
        # nothing could reach, one edit from being rendered again. The
        # withholding itself is the assertion now.
        self.assertIn(
            'if (p.spreadBand && typeof live.spreadMv !== "number") return;',
            page_script(),
            "the band is drawn with no reading behind it, so it needs a "
            "resting colour again -- and a resting colour is a reading "
            "nobody took",
        )

    def test_the_spread_tint_moves_across_its_measured_range(self):
        # The ramp has to be traversed by real readings, which
        # test_the_cell_spread_ramp_can_actually_be_traversed checks against
        # the corpus. This checks the other half: that the page's own
        # arithmetic actually moves between the two anchors.
        page = page_constants()
        low, high = page["CELL_SPREAD_TYPICAL_MV"], page["CELL_SPREAD_HIGH_MV"]
        seen = [run_reading_rules("return cellSpreadEmissive(%r);" % mv)
                for mv in (low, (low + high) / 2, high)]
        self.assertEqual(len(set(tuple(c) for c in seen)), 3)
        self.assertLess(seen[0][0], seen[1][0])
        self.assertLess(seen[1][0], seen[2][0])

    # -- the torque field -------------------------------------------------
    def test_only_exactly_two_bytes_are_read_as_torque(self):
        # live.py's `_hex` parses the WHOLE hex string it is handed. Taking
        # the first two bytes of something wider would put the page and the
        # node on different numbers for the same instant with neither one
        # complaining.
        self.assertEqual(run_reading_rules('return torqueCounts("5806");'), 0)
        self.assertEqual(run_reading_rules('return torqueCounts("5996");'), 400)
        for odd in ("58", "580600", "", "zz06"):
            with self.subTest(odd):
                self.assertIsNone(run_reading_rules(
                    'return torqueCounts(%s);' % json.dumps(odd)))

    # -- the corner tint --------------------------------------------------
    def test_no_corner_reading_does_not_look_like_a_measured_agreement(self):
        # THE DEFECT: `cornerDeviation` returns null when a corner is missing
        # from the row, when any corner is not fresh, or when the mean is
        # under 5 km/h, and the draw loop then left the tyre at [0, 0, 0] --
        # pixel-identical to a corner measured squarely inside the 1 km/h
        # deadband. So "nobody read this corner" and "this corner agrees with
        # the other three" were the same tyre.
        #
        # It is the COMMON case, not an edge: 9,257 of the 10,682 rows in
        # evidence/sessions (86.7%) produce no four-corner deviation at all.
        # The freshness gate made it worse, because one stale corner untints
        # all four, and four untinted tyres read as the affirmative claim
        # "the corners agree".
        #
        # AND IT IS ASSERTED AS RENDERED, for the reason spelled out in
        # test_no_effort_reading_does_not_look_like_a_measured_neutral: the
        # contrast floor gets the last word over what `cornerEmissive`
        # returns, in the DEFAULT view, and it used to overwrite an absent
        # tyre's [0, 0, 0] with its own grey. Comparing the rule's two return
        # values proves nothing about the picture.
        absent = as_rendered("cornerEmissive(null)", TYRE_PART)
        agree = as_rendered("cornerEmissive(0.25)", TYRE_PART)
        self.assertNotEqual(
            absent, agree,
            "a tyre with no reading and a tyre measured inside the sensor's "
            "own step render identically, so the page states 'the corners "
            "agree' where it measured nothing",
        )
        self.assertEqual(
            absent, [0, 0, 0],
            "an absent corner reading does not render unlit -- the page, the "
            "legend swatch and the caption all say unlit, and the floor says "
            "otherwise last",
        )
        # Agreement is neither the warm nor the cool end: it may not read as
        # a faint version of either finding, and it clears the floor on its
        # own rather than being lifted onto it.
        page = page_constants()
        self.assertEqual(agree, page["CORNER_AGREE"])
        # THE MARGIN, AND IT IS MEASURED THROUGH THE ALPHA. The tyre is
        # composited at 0.38 so the disc behind it reads through, which cuts
        # every emissive difference on it to a third before it reaches the
        # pixel. The surviving difference has to be at least the lift the page
        # itself calls the minimum for a part to be visible against the void
        # -- CONTRAST_FLOOR. With the floor overwriting absent it was
        # (0.32 - 0.12) * 0.38 = 0.076, under that bar; unlit it is
        # 0.32 * 0.38 = 0.12, over it.
        lift, alpha = page["CONTRAST_FLOOR"][0], tyre_alpha()
        self.assertGreaterEqual(
            max(g - a for g, a in zip(agree, absent)) * alpha, lift,
            "through the tyre's own alpha, absent and measured-agreement are "
            "closer together on screen than the page's own floor for "
            "'visible at all' -- and no frame ever shows one of each, "
            "because the corner deviation is all four tyres or none",
        )

    def test_a_faint_measured_corner_is_not_mistaken_for_no_reading(self):
        # The same defect at the other end of the ramp. A corner measured
        # 1.05 km/h out sits 1.7% along it, which was emissive [0.009, 0.005,
        # 0] -- a sum of 0.014, under the 0.06 contrast floor at draw()'s
        # `layers.shell <= 0` branch, so the faintest real finding was
        # OVERWRITTEN with the untinted colour. Direction is itself a
        # reading, so any direction has to be legible.
        for deviation in (1.05, -1.05):
            with self.subTest(deviation):
                faint = run_reading_rules("return cornerEmissive(%r);" % deviation)
                self.assertGreater(
                    sum(faint), 0.06,
                    "a measured corner deviation renders below the contrast "
                    "floor, so draw() replaces it with the untinted colour "
                    "and the finding disappears",
                )
                self.assertNotEqual(faint, run_reading_rules(
                    "return cornerEmissive(null);"))
        # Without turning the ramp off: further out still reads further out.
        near = run_reading_rules("return cornerEmissive(1.05);")
        far = run_reading_rules("return cornerEmissive(1.7);")
        self.assertGreater(sum(far), sum(near))

    def test_a_corner_past_the_top_of_the_ramp_is_flagged_not_pinned(self):
        # 2.0% of the readings this ramp colours run past its top, so this is
        # a state the page really enters. Pinned at full tint it would say
        # "1.75 km/h" over a corner 2.75 km/h out -- the same defect as the
        # spread tint pinned at full amber.
        page = page_constants()
        top = run_reading_rules("return cornerEmissive(%r);"
                                % page["CORNER_HIGH_KPH"])
        over = run_reading_rules("return cornerEmissive(%r);"
                                 % (page["CORNER_HIGH_KPH"] + 0.01))
        self.assertNotEqual(
            top, over,
            "a corner past the top of the ramp renders exactly as one at the "
            "top of it, so the tint asserts a value it does not have",
        )
        self.assertEqual(over, page["CORNER_OVER_FAST"])
        self.assertEqual(run_reading_rules(
            "return cornerEmissive(%r);" % (-page["CORNER_HIGH_KPH"] - 0.01)),
            page["CORNER_OVER_SLOW"])
        # Direction survives the flag: an over-range corner still says which
        # way it is out, and neither flag is on the ramp it left.
        self.assertNotEqual(page["CORNER_OVER_FAST"], page["CORNER_OVER_SLOW"])

    def test_the_corner_ramp_moves_across_its_measured_range(self):
        page = page_constants()
        low, high = page["CORNER_DEADBAND_KPH"], page["CORNER_HIGH_KPH"]
        seen = [run_reading_rules("return cornerEmissive(%r);" % kph)
                for kph in (low, (low + high) / 2, high)]
        self.assertEqual(len(set(tuple(c) for c in seen)), 3)
        self.assertLess(seen[0][0], seen[1][0])
        self.assertLess(seen[1][0], seen[2][0])


@unittest.skipIf(NODE is None, "node is not installed on this machine")
class FrameResolutionTests(unittest.TestCase):
    """A replayed frame has to be findable in the history it came from.

    Four readings in renderVehicle walk BACKWARDS from the scrubbed frame:
    the delta-encoded module array, the nearest pack sample inside a plug-in,
    the charge-status indicator and the thermal accumulator's advance. Every
    one of them located the frame with `hist.indexOf(f)`, which is object
    identity -- and during a replay the two sides come from different objects
    by construction. `state.frame` is handed out of `state.map.rows`, which
    renderMap deliberately does NOT rebuild while the track is unchanged, and
    `state.snapshot` is replaced wholesale every 5 s.
    """

    #: Three history rows, delta-encoded exactly as dashboard.py encodes them:
    #: the first states the array, the two after it are silent because it did
    #: not change. Across the corpus 89% of rows are silent this way.
    HISTORY = [
        {"utc": "2026-01-01T12:00:00Z", "elapsed_s": 0, "array_2af1": "AB" * 24},
        {"utc": "2026-01-01T12:00:05Z", "elapsed_s": 5},
        {"utc": "2026-01-01T12:00:10Z", "elapsed_s": 10},
    ]

    def resolve(self, frame_index=2):
        """What the page gets for a frame out of a SUPERSEDED snapshot."""
        return run_reading_rules(
            "var served = %s;\n"
            # The snapshot the map was built from, and the one the next poll
            # replaced it with. Equal rows; different objects -- which is
            # exactly what the browser has five seconds into any replay.
            "var stale = JSON.parse(JSON.stringify(served));\n"
            "var fresh = JSON.parse(JSON.stringify(served));\n"
            "var f = stale[%d];\n"
            "var at = frameIndex(f, fresh);\n"
            "return [at, carriedFrom(fresh, at - 1, \"array_2af1\")];"
            % (json.dumps(self.HISTORY), frame_index))

    def test_a_frame_from_a_superseded_snapshot_still_resolves_the_array(self):
        at, array = self.resolve()
        self.assertEqual(
            at, 2,
            "the frame could not be found in the history it describes, so "
            "every backward walk in renderVehicle starts from -1",
        )
        self.assertEqual(
            array, "AB" * 24,
            "a replayed frame that inherits the module array resolved to no "
            "array at all -- five seconds into any replay the 24 module "
            "blocks drop to their base colour and stay there, the page "
            "asserting that the pack stopped answering about a pack that "
            "was steady",
        )

    def test_a_row_that_states_its_own_absence_is_not_walked_past(self):
        # The encoding turns on whether the key is PRESENT. An explicit null
        # is a row with NO array, and carrying the previous row's value over
        # it would paint the blocks steady through a run where the pack had
        # gone quiet.
        history = [
            {"utc": "2026-01-01T12:00:00Z", "elapsed_s": 0, "array_2af1": "AB" * 24},
            {"utc": "2026-01-01T12:00:05Z", "elapsed_s": 5, "array_2af1": None},
            {"utc": "2026-01-01T12:00:10Z", "elapsed_s": 10},
        ]
        self.assertIsNone(run_reading_rules(
            "var hist = %s;\n"
            "return carriedFrom(hist, 2, \"array_2af1\");" % json.dumps(history)))

    def test_a_frame_with_no_clock_falls_back_to_its_elapsed_time(self):
        # `utc` is written verbatim from the recorder and can be missing on a
        # row it wrote without one.
        history = [dict(row, utc=None) for row in self.HISTORY]
        at, array = run_reading_rules(
            "var served = %s;\n"
            "var stale = JSON.parse(JSON.stringify(served));\n"
            "var fresh = JSON.parse(JSON.stringify(served));\n"
            "var at = frameIndex(stale[2], fresh);\n"
            "return [at, carriedFrom(fresh, at - 1, \"array_2af1\")];"
            % json.dumps(history))
        self.assertEqual(at, 2)
        self.assertEqual(array, "AB" * 24)

    def test_a_frame_that_is_not_in_this_history_resolves_to_nothing(self):
        # A session change swaps the history under a frame that belongs to
        # another drive. There is no right answer, and inventing one would
        # paint one session's pack with another's.
        self.assertEqual(run_reading_rules(
            "return frameIndex({utc: \"2030-01-01T00:00:00Z\", elapsed_s: 9999},"
            " %s);" % json.dumps(self.HISTORY)), -1)
        self.assertEqual(run_reading_rules(
            "return frameIndex(null, %s);" % json.dumps(self.HISTORY)), -1)

    @unittest.skipIf(not CORPUS.is_dir(), "the recorded sessions are not on this machine")
    def test_the_key_the_lookup_stands_on_is_unique_per_session(self):
        # `utc` identifies a frame only if no two rows of a session share it.
        # The recorder writes it at microsecond resolution, which makes that
        # true rather than hoped for -- and this is where it stops being an
        # assumption.
        rows = 0
        for path in sorted(CORPUS.glob("drive-*.csv")):
            with path.open(newline="") as handle:
                stamps = [row.get("utc") for row in csv.DictReader(handle)]
            rows += len(stamps)
            with self.subTest(path.name):
                self.assertEqual(
                    len(set(stamps)), len(stamps),
                    "two rows of one session carry the same clock, so a "
                    "frame cannot be located by it",
                )
        if rows < 1000:
            self.skipTest("too few recorded rows on this machine to cite")

    def test_no_consumer_locates_a_frame_by_object_identity(self):
        # The cheap half of the tests above, and the one that covers the
        # three consumers they do not run: the charge-window walk, the
        # charge-status indicator and the thermal advance. All four had the
        # same lookup, and fixing one of them would have left the page with
        # two rules for the same question.
        #
        # Asserted as a boolean rather than with assertNotIn, whose failure
        # message would print the entire page script.
        source = page_script()
        self.assertFalse(
            "indexOf(f)" in source,
            "a frame is still located in the history by object identity, "
            "which cannot succeed for a replayed frame",
        )
        self.assertEqual(
            source.count("frameIndex(f, hist)"), 4,
            "all four consumers -- the module array, the charge window, the "
            "charge-status indicator and the thermal advance -- should "
            "resolve the frame the same way",
        )


class PageLiteralTests(unittest.TestCase):
    """Numbers the page states outright, and what they are answerable to."""

    def test_the_pages_torque_zero_and_band_match_the_python_ones(self):
        # The page needs its own copy: a replay has no node answer for the row
        # being scrubbed and has to reach the verdict the node would have
        # reached. Two copies of a number is somewhere they drift apart, and
        # drifting here means the live view and the replay disagreeing about
        # whether the same instant was drive, regen or neutral.
        page = page_constants()
        self.assertEqual(page["TORQUE_ZERO"], live.TORQUE_ZERO)
        self.assertEqual(page["TORQUE_BAND"], live.TORQUE_BAND)

    def test_the_cell_spread_ramp_is_measured_and_not_invented(self):
        # It used to run 10 mV to 70 mV and present that as calibrated. It was
        # not: 70 was a divisor picked to make a ramp. On this truck the
        # spread has never exceeded 19.7 mV, so the visual sat permanently in
        # the bottom sixth of its range and could not move enough to be read.
        # An absolute scale must cite where its numbers came from -- see the
        # note beside identifier 2429 in drive.py, which is this project's
        # whole point.
        page, source = page_constants(), page_script()
        # Boolean rather than assertNotIn, whose failure message would print
        # the entire page script.
        self.assertFalse("live.spreadMv - 10) / 60" in source,
                         "the uncited 10-70 mV ramp is still in the page")
        low, high = page["CELL_SPREAD_TYPICAL_MV"], page["CELL_SPREAD_HIGH_MV"]
        self.assertLess(low, high)
        # The top of the ramp is NOT the maximum on record, and the comment
        # has to say so: a once-ever maximum is an unreachable anchor, which
        # is the same defect as an invented one wearing a citation.
        self.assertLess(high, page["CELL_SPREAD_MAX_SEEN_MV"])
        # The comment beside them has to say where they came from, by name.
        where = source[source.index("THE SPREAD RAMP"):
                       source.index("var CELL_SPREAD_TYPICAL_MV")]
        for cited in ("10,349", "evidence/sessions", "median",
                      "99th percentile", "maximum"):
            with self.subTest(cited):
                self.assertIn(cited, where,
                              "an absolute scale that does not say what it was "
                              "measured from is an invented one")

    @unittest.skipIf(not CORPUS.is_dir(), "the recorded sessions are not on this machine")
    def test_the_cell_spread_ramp_still_matches_the_sessions_on_disk(self):
        # The point of citing a corpus is that the citation can be checked.
        # When the corpus grows past the ramp, this says so rather than the
        # page quietly going out of date -- which is how 10/70 survived.
        readings = corpus_spread_mv()
        if len(readings) < 1000:
            self.skipTest("too few recorded cell samples on this machine to cite")
        page = page_constants()
        self.assertAlmostEqual(page["CELL_SPREAD_TYPICAL_MV"],
                               statistics.median(readings), places=1,
                               msg="the green end of the ramp is no longer this "
                                   "pack's ordinary spread")
        self.assertAlmostEqual(page["CELL_SPREAD_HIGH_MV"],
                               percentile(readings, 0.99), places=1,
                               msg="the amber end of the ramp is no longer the "
                                   "99th percentile of the record -- rescale "
                                   "it, and restate the corpus in the comment")
        self.assertAlmostEqual(page["CELL_SPREAD_MAX_SEEN_MV"], max(readings),
                               places=1,
                               msg="the caption still quotes an old widest-ever "
                                   "reading")

    @unittest.skipIf(not CORPUS.is_dir(), "the recorded sessions are not on this machine")
    def test_the_cell_spread_ramp_can_actually_be_traversed(self):
        # THE REAL BAR, and the one the previous version of this test could
        # not clear. It used to assert that more than a tenth of readings
        # exceeded CELL_SPREAD_TYPICAL_MV -- while another assertion in the
        # same test fixed that constant AT THE MEDIAN. By definition about
        # half of the readings exceed the median, so the bar was unfailable
        # for any ramp built this way: it passed identically with the top at
        # 19.7 mV, and would have passed with the top at 197.
        #
        # What matters is not how many readings leave the floor but HOW FAR
        # ALONG THE RAMP they get, so that is what is measured: where each
        # recorded reading lands between the two anchors. Against the
        # once-ever 19.7 mV maximum the answer is 3.9% mean position and
        # 0.17% ever reaching halfway; against the 99th percentile it is
        # 14.6% and 4.5%. The thresholds below sit between those two, so
        # reinstating the unreachable anchor fails this test.
        readings = corpus_spread_mv()
        if len(readings) < 1000:
            self.skipTest("too few recorded cell samples on this machine to cite")
        page = page_constants()
        low, high = page["CELL_SPREAD_TYPICAL_MV"], page["CELL_SPREAD_HIGH_MV"]
        where = [min(1.0, max(0.0, (v - low) / (high - low))) for v in readings]
        self.assertGreaterEqual(
            sum(where) / len(where), 0.10,
            "the average recorded reading sits in the bottom tenth of this "
            "ramp, so the tint spends the drive at its floor and says nothing",
        )
        self.assertGreaterEqual(
            sum(1 for x in where if x >= 0.5) / len(where), 0.02,
            "fewer than one reading in fifty reaches the middle of this ramp "
            "-- the amber end is decorative, which is the defect the 10-70 mV "
            "ramp was replaced for",
        )
        # And the other end: a ramp nothing ever runs past does not need a
        # flag, and one that everything runs past is not a ramp. Both are
        # signs the anchor has drifted.
        over = sum(1 for v in readings if v > high) / len(readings)
        self.assertGreater(over, 0.001,
                           "nothing on record exceeds the top of the ramp, so "
                           "the over-range flag can never appear")
        self.assertLess(over, 0.05,
                        "more than one reading in twenty runs past the top of "
                        "the ramp, so the flag is the normal state")

    def test_the_corner_ramp_is_measured_and_not_invented(self):
        # It ran `Math.min(1, (dm - 1.0) / 3.0)`, which puts full tint at
        # 4.0 km/h of deviation from the four-corner mean. That 3.0 was
        # uncited and unreachable: the largest deviation this truck has ever
        # recorded is 2.75 km/h, which sits 58% of the way up, so the whole
        # top 42% of the ramp was dead. Exactly the standard this change set
        # for the cell-spread ramp.
        page, source = page_constants(), page_script()
        # Boolean rather than assertNotIn, whose failure message would print
        # the entire page script.
        self.assertFalse("(dm - 1.0) / 3.0" in source,
                         "the uncited 4.0 km/h corner ramp is still in the page")
        self.assertLess(page["CORNER_DEADBAND_KPH"], page["CORNER_HIGH_KPH"])
        # The top is a PERCENTILE of the record and not its maximum, for the
        # same reason the spread ramp's is: a once-ever anchor is an
        # unreachable one, which is an invented scale wearing a citation.
        self.assertLess(page["CORNER_HIGH_KPH"], page["CORNER_MAX_SEEN_KPH"])
        where = source[source.index("THE CORNER RAMP"):
                       source.index("var CORNER_DEADBAND_KPH")]
        for cited in ("10,682", "evidence/sessions", "5,700", "median",
                      "99th percentile", "1,425"):
            with self.subTest(cited):
                self.assertIn(cited, where,
                              "a scale that does not say what it was measured "
                              "from is an invented one")

    @unittest.skipIf(not CORPUS.is_dir(), "the recorded sessions are not on this machine")
    def test_the_corner_ramp_still_matches_the_sessions_on_disk(self):
        # The point of citing a corpus is that the citation can be checked.
        readings = corpus_corner_deviations()
        if len(readings) < 1000:
            self.skipTest("too few recorded corner samples on this machine to cite")
        page = page_constants()
        self.assertAlmostEqual(
            page["CORNER_HIGH_KPH"], percentile(readings, 0.999), places=2,
            msg="the top of the corner ramp is no longer the 99.9th "
                "percentile of the record -- rescale it, and restate the "
                "corpus in the comment")
        self.assertAlmostEqual(
            page["CORNER_MAX_SEEN_KPH"], max(readings), places=2,
            msg="the caption still quotes an old widest-ever deviation")

    @unittest.skipIf(not CORPUS.is_dir(), "the recorded sessions are not on this machine")
    def test_the_corner_ramp_can_actually_be_traversed(self):
        # The same bar test_the_cell_spread_ramp_can_actually_be_traversed
        # sets, applied to the tyres: not how many readings leave the floor
        # but HOW FAR ALONG THE RAMP they get.
        #
        # The population is the readings the ramp actually colours -- those
        # that clear the 1 km/h deadband, which is one whole sensor step and
        # not part of the ramp. There are 253 of them in the corpus. Against
        # the old 4.0 km/h top their mean position is 7.3% and 0.4% reach
        # halfway; against 1.75 it is 27.7% and 26.9%. The thresholds below
        # sit between those two, so reinstating the 3.0 divisor fails this.
        readings = corpus_corner_deviations()
        if len(readings) < 1000:
            self.skipTest("too few recorded corner samples on this machine to cite")
        page = page_constants()
        low, high = page["CORNER_DEADBAND_KPH"], page["CORNER_HIGH_KPH"]
        tinted = [v for v in readings if v >= low]
        self.assertGreater(len(tinted), 50,
                           "too few readings clear the deadband to say "
                           "anything about the ramp above it")
        where = [min(1.0, (v - low) / (high - low)) for v in tinted]
        self.assertGreaterEqual(
            sum(where) / len(where), 0.15,
            "the average tinted corner sits in the bottom sixth of this "
            "ramp, so the tint spends every drive at its floor and says "
            "nothing",
        )
        self.assertGreaterEqual(
            sum(1 for x in where if x >= 0.5) / len(where), 0.05,
            "fewer than one tinted corner in twenty reaches the middle of "
            "this ramp -- the far end is decorative, which is the defect the "
            "4.0 km/h divisor was replaced for",
        )
        # And the other end: a ramp nothing runs past needs no flag, and one
        # everything runs past is not a ramp.
        over = sum(1 for v in tinted if v > high) / len(tinted)
        self.assertGreater(over, 0.005,
                           "nothing on record exceeds the top of the ramp, so "
                           "the over-range flag can never appear")
        self.assertLess(over, 0.10,
                        "more than one tinted corner in ten runs past the top "
                        "of the ramp, so the flag is the normal state")


class CellEnvelopeRemovalTests(unittest.TestCase):
    """The three cell marks are gone, and nothing may still claim them.

    They stood on the pack case's own height as a fixed 3.0-4.2 V axis. The
    decode was sound; the geometry could not carry it. That axis maps 1.2 V
    onto 0.320 model units, so 1 mV is 0.000267 units while each mark was
    0.020 units thick: the widest imbalance this truck has ever recorded,
    19.7 mV, separated the lowest mark from the highest by 26% of one mark's
    own thickness, and the median reading, 3.4 mV, by 4.5% of it -- about a
    tenth of a pixel. On every reading in the corpus the three marks were one
    bar, under a caption saying they showed lowest, average and highest.
    """

    def test_the_arithmetic_that_condemned_the_marks(self):
        # Stated as a computation rather than as prose, so a later attempt to
        # put them back has to answer it.
        units_per_volt = 0.320 / (4.2 - 3.0)
        mark_thickness = 0.020
        for name, spread_mv in (("widest ever recorded", 19.7),
                                ("this pack's median", 3.4)):
            with self.subTest(name):
                separation = spread_mv / 1000 * units_per_volt
                self.assertLess(
                    separation, mark_thickness * 0.30,
                    "the lowest and highest marks are separated by less than "
                    "a third of one mark's thickness, so three marks are one "
                    "bar under a caption calling them three",
                )

    def test_the_marks_and_their_axis_are_gone_from_the_page(self):
        page = PAGE_PATH.read_text(encoding="utf-8")
        for gone in ("cell-env", "CELL_AXIS", "cellEnv:"):
            with self.subTest(gone):
                # Boolean rather than assertNotIn, whose failure message
                # would print the entire page.
                self.assertFalse(
                    gone in page,
                    f"{gone} is still in the page -- the marks, their axis or "
                    "their positioning code survived",
                )

    def test_the_numbers_are_printed_instead(self):
        # Removing a visual that cannot express its claim is only half of it.
        # The reading is real and established -- 0x2AF5's first six bytes are
        # u16/10000 V at level 4, cross-validated three ways -- so it goes
        # where a millivolt CAN be expressed, which is digits.
        source = page_script()
        note = source[source.index("cellNote = cellsHere"):
                      source.index("el[\"cell-caveat\"].textContent = cellNote")]
        for bound in ("cellsHere.min.toFixed(3)", "cellsHere.avg.toFixed(3)",
                      "cellsHere.max.toFixed(3)"):
            with self.subTest(bound):
                self.assertIn(bound, note,
                              "the cell voltages are neither drawn nor "
                              "printed, so the reading left the page entirely")
        self.assertIn("spreadHere.toFixed(1)", note,
                      "the spread is not printed as a number beside its tint")

    def test_the_part_index_no_longer_sends_anyone_to_the_marks(self):
        page = PAGE_PATH.read_text(encoding="utf-8")
        start = page.index("var SIGNAL_PART = {")
        table = page[start:page.index("\n  };", start)]
        for column in ("cell_min_v", "cell_avg_v", "cell_max_v"):
            with self.subTest(column):
                entry = re.search(r'^\s{4}%s:\s*\[\s*"([^"]*)"' % column,
                                  table, re.M)
                self.assertTrue(entry, f"{column} left the part index entirely")
                self.assertEqual(
                    entry.group(1), "#cell-caveat",
                    "the index still names geometry the scene does not build",
                )
        spread = re.search(r'^\s{4}cell_spread_mv:\s*\[\s*"([^"]*)"',
                           table, re.M)
        self.assertEqual(spread.group(1), "cell-spread-band",
                         "the spread tint has to say where it actually is")


class VehicleCaptionTests(unittest.TestCase):
    """A visual that can withhold has to be able to say that it withheld."""

    def legend(self) -> str:
        """The legend's source: its swatch table and the code that draws it."""
        source = page_script()
        start = source.index("function legendEntries()")
        return source[start:source.index("\n  }",
                                         source.index("function renderLegend()"))]

    def legend_swatches(self) -> list:
        """The legend's own table, RUN rather than read off the source.

        Every swatch in it is computed now -- `swatchHex` over the part's
        diffuse and over the emissive the page's own draw rule returns -- so
        there is no hex in the source to grep for, which is the point: a
        hand-written hex beside a label is a second source of truth for a
        colour nobody can check by eye against the model, and every one of
        them here had drifted from what it claimed.
        """
        return run_reading_rules("return legendEntries();")

    def swatch_for(self, prefix: str) -> str:
        """The one legend entry whose label starts with *prefix*."""
        hits = [colour for colour, label in self.legend_swatches()
                if label.startswith(prefix)]
        self.assertEqual(len(hits), 1,
                         "no single legend entry for %r" % prefix)
        return hits[0]

    def swatch(self, rgb) -> str:
        return "#%02x%02x%02x" % tuple(int(round(c * 255)) for c in rgb)

    def test_the_halfshaft_colours_are_in_the_legend(self):
        # Four states on one part, and nothing on the page said which was
        # which. The last two are the ones that matter: a measured neutral
        # and no reading at all were the same shaft, so the legend has to
        # distinguish them in words as well as the model distinguishing them
        # in colour.
        legend = self.legend()
        for phrase in ("halfshaft: drive effort", "halfshaft: regen effort",
                       "halfshaft: measured, inside the neutral band",
                       "halfshaft: unlit"):
            with self.subTest(phrase):
                self.assertIn(phrase, legend,
                              "the shaft colours are not explained anywhere")
        # The four swatches themselves are checked in
        # test_every_legend_swatch_is_its_state_under_the_shading_rule, which
        # needs node to run the page's own rules.

    #: Every state in the two reading sets, as the page's own name for the
    #: part's diffuse and the page's own draw rule for that state's emissive.
    #: Both are RUN, not transcribed; only the sum is redone here.
    SHADED_STATES = (
        ("tyre: turning faster", "TYRE_DIFFUSE",
         "cornerEmissive(CORNER_HIGH_KPH)"),
        ("tyre: turning slower", "TYRE_DIFFUSE",
         "cornerEmissive(-CORNER_HIGH_KPH)"),
        ("tyre: measured,", "TYRE_DIFFUSE", "cornerEmissive(0)"),
        ("tyre: unlit", "TYRE_DIFFUSE", "cornerEmissive(null)"),
        ("tyre: faster, past", "TYRE_DIFFUSE",
         "cornerEmissive(CORNER_MAX_SEEN_KPH)"),
        ("tyre: slower, past", "TYRE_DIFFUSE",
         "cornerEmissive(-CORNER_MAX_SEEN_KPH)"),
        ("halfshaft: drive effort", "SHAFT_DIFFUSE",
         'effortEmissive({dir: "drive", counts: 1, scale: 1})'),
        ("halfshaft: regen effort", "SHAFT_DIFFUSE",
         'effortEmissive({dir: "regen", counts: 1, scale: 1})'),
        ("halfshaft: measured,", "SHAFT_DIFFUSE",
         'effortEmissive({dir: "neutral"})'),
        ("halfshaft: unlit", "SHAFT_DIFFUSE", "effortEmissive(null)"),
    )

    @unittest.skipIf(NODE is None, "node is not installed on this machine")
    def test_every_legend_swatch_is_its_state_under_the_shading_rule(self):
        # THE DEFECT, twice, in opposite directions.
        #
        # First the two "unlit" entries quoted the part's raw DIFFUSE --
        # #0e0f11 on the tyre, #525a5e on the shaft -- while every other entry
        # in their sets quoted a raw EMISSIVE. Two quantities in one set, so
        # the entries were not comparable: #525a5e is brighter than #3d424c,
        # the measured-neutral swatch beside it, while the model draws the
        # measured neutral as the brighter of the two.
        #
        # The repair set both to #000000, on the premise that the diffuse "is
        # a value the draw loop never puts on screen". The fragment shader
        # says otherwise:
        #
        #     vec3 lit = uColour * (0.20 + kd * 0.85 + fd) + vec3(spec);
        #     lit += uEmissive;
        #
        # `uColour` IS the diffuse and it is multiplied into the output for
        # every non-textured part, every frame. An unlit halfshaft renders its
        # own grey under the lighting term, not black, so #000000 understated
        # it -- the same inversion pointing the other way.
        #
        # THE FIX is that every swatch in a set is the same computed
        # quantity: the part's diffuse under the one lighting term that is a
        # property of the STATE rather than of the camera -- the shader's
        # 0.20 ambient floor -- plus that state's own emissive. See `shade`.
        ambient = page_constants()["SWATCH_AMBIENT"]
        for prefix, diffuse, emissive in self.SHADED_STATES:
            with self.subTest(prefix):
                lit = run_reading_rules("return [%s, %s];" % (diffuse, emissive))
                self.assertEqual(
                    self.swatch_for(prefix), shade(lit[0], lit[1], ambient),
                    "this swatch is not the pixel the model draws for this "
                    "state: it is neither the raw diffuse nor a raw emissive "
                    "that belongs beside it, it is the sum the shader writes",
                )

    @unittest.skipIf(NODE is None, "node is not installed on this machine")
    def test_every_module_swatch_is_its_cell_under_the_shading_rule(self):
        # The module blocks state their reading in DIFFUSE and carry no
        # emissive of their own, so what lights them in the default body-off
        # view is draw()'s contrast floor. These swatches were hand-written
        # hexes off the CSS palette and every one of them had drifted from
        # the colour `moduleColour` actually returns -- #4e9c68 for a cell
        # drawn #4d9c69, #ffb454 for #ffb554, #7fd3e8 for #80d4e8, #262b2e for
        # #292e30. Invisible drift is still two sources of truth, and it is
        # the same defect as the one above in its earliest stage.
        ambient = page_constants()["SWATCH_AMBIENT"]
        floor = run_reading_rules(
            'return contrastFloor([0, 0, 0], {layer: "pack"}, true);')
        for z, label in ((0, "typical"), (2, "above the pack"),
                         (3, "well above"), (-2, "below the pack"),
                         (-3, "well below"), (None, "no reading")):
            with self.subTest(label):
                cell = run_reading_rules(
                    "return moduleColour(%s, 0, 1);"
                    % ("null" if z is None else z))
                self.assertEqual(self.swatch_for(label),
                                 shade(cell, floor, ambient))

    @unittest.skipIf(NODE is None, "node is not installed on this machine")
    def test_the_legend_names_every_colour_the_module_ramp_draws(self):
        # `moduleColour` cuts the cool side at -1.1 AND at -2.2, exactly as it
        # cuts the warm side at +1.1 and +2.2, so it draws six colours. The
        # legend named five: the far cool band -- a module a long way BELOW
        # its siblings, which is the shape of a cell group that has dropped --
        # had no entry at all, while the far warm band had one.
        drawn = run_reading_rules(
            "var out = [], seen = {}, z;"
            "for (z = -6; z <= 6; z += 0.05) {"
            "  var c = JSON.stringify(moduleColour(z, 0, 1));"
            "  if (!seen[c]) { seen[c] = 1; out.push(JSON.parse(c)); } }"
            "out.push(moduleColour(null, 0, 1));"
            "return out;")
        ambient = page_constants()["SWATCH_AMBIENT"]
        floor = run_reading_rules(
            'return contrastFloor([0, 0, 0], {layer: "pack"}, true);')
        want = {shade(c, floor, ambient) for c in drawn}
        have = {colour for colour, _ in self.legend_swatches()}
        self.assertEqual(
            want - have, set(),
            "the module ramp draws a colour the legend does not name, so a "
            "block the page has painted is left unexplained",
        )

    @unittest.skipIf(NODE is None, "node is not installed on this machine")
    def test_the_legend_swatches_rank_the_way_the_model_draws_them(self):
        # A set of entries side by side is a claim about which states are the
        # brighter ones, and that claim has to match the screen whatever any
        # single swatch is set to. Read off the legend's own table rather than
        # off the model, by peak channel -- see `peak_channel` for why not a
        # weighted sum.
        for darker, brighter in (
                ("tyre: unlit", "tyre: measured,"),
                ("tyre: measured,", "tyre: turning faster"),
                ("tyre: measured,", "tyre: turning slower"),
                ("tyre: measured,", "tyre: faster, past"),
                ("tyre: measured,", "tyre: slower, past"),
                ("halfshaft: unlit", "halfshaft: measured,"),
                ("halfshaft: measured,", "halfshaft: drive effort"),
                ("halfshaft: measured,", "halfshaft: regen effort")):
            with self.subTest("%s < %s" % (darker, brighter)):
                self.assertLess(
                    peak_channel(self.swatch_for(darker)),
                    peak_channel(self.swatch_for(brighter)),
                    "the legend ranks these two states the opposite way round "
                    "from the model, so it inverts the relationship it exists "
                    "to teach",
                )

    @unittest.skipIf(NODE is None, "node is not installed on this machine")
    def test_no_two_legend_swatches_are_the_same_square(self):
        # Two entries drawn the same colour teach nothing: the reader is told
        # there are two states and shown one. #000000 on both "unlit" entries
        # did exactly that -- a tyre and a halfshaft with no reading are the
        # same black square, though the model draws them three units apart in
        # every channel and they are the two commonest states on the page.
        swatches = self.legend_swatches()
        seen = {}
        for colour, label in swatches:
            with self.subTest(label[:40]):
                self.assertNotIn(
                    colour, seen,
                    "this swatch is the same square as %r" % seen.get(colour))
            seen[colour] = label
        # And visible against the panel they sit on -- which for the darkest
        # of them is what the 1px border on `.legend i` is for.
        page = PAGE_PATH.read_text(encoding="utf-8")
        panel = re.search(r"^  --panel:(#[0-9a-f]{6});", page, re.M).group(1)
        border = re.search(r"^  --line:(#[0-9a-f]{6});", page, re.M).group(1)
        self.assertIn("border:1px solid var(--line)",
                      re.search(r"^\.legend i\{([^}]*)\}", page, re.M).group(1),
                      "the near-black swatches have no border, so on the "
                      "panel they are entries a reader cannot see are there")
        for colour, label in swatches:
            with self.subTest(label[:40]):
                self.assertTrue(
                    max(abs(int(colour[i:i + 2], 16)
                            - int(panel[i:i + 2], 16)) for i in (1, 3, 5)) >= 8
                    or max(abs(int(colour[i:i + 2], 16)
                               - int(border[i:i + 2], 16))
                           for i in (1, 3, 5)) >= 8,
                    "this swatch is indistinguishable from the panel it is "
                    "drawn on and from its own border",
                )

    def test_the_off_ramp_cell_colour_is_in_the_legend(self):
        legend = self.legend()
        self.assertIn(self.swatch(page_constants()["CELL_SPREAD_OVER"]), legend)
        self.assertIn("past 7.6 mV", legend)

    def test_the_legend_no_longer_claims_a_cell_voltage_axis(self):
        # The marks are gone; a legend entry for "off the 3.0-4.2 V axis"
        # would send a viewer looking for geometry that is not there.
        legend = self.legend()
        for phrase in ("cell mark", "3.0–4.2 V axis"):
            with self.subTest(phrase):
                self.assertNotIn(phrase, legend)

    def test_the_corner_colours_are_in_the_legend(self):
        # Five states on one part. The third and fourth are the ones that
        # matter: a corner measured inside the sensor's own step and a corner
        # nobody read were the same black tyre, so the legend has to
        # distinguish them in words as well as the model distinguishing them
        # in colour.
        legend = self.legend()
        for phrase in ("tyre: turning faster", "tyre: turning slower",
                       "tyre: measured, within one sensor step",
                       "tyre: unlit"):
            with self.subTest(phrase):
                self.assertIn(phrase, legend,
                              "the tyre colours are not explained anywhere")
        # The five swatches themselves are checked in
        # test_every_legend_swatch_is_its_state_under_the_shading_rule: each
        # one is the page's own draw rule for that state, shaded, so there is
        # no hex in the source to match against a constant here.
        self.assertIn("past 1.75 km/h", legend,
                      "the over-range flag does not say what it is past")

    def test_the_corner_caption_names_the_absent_case(self):
        # The one sentence that stops four unlit tyres being read as "the
        # corners agree". It has to state the frequency too: "no reading" is
        # only credible if the page says how often that happens -- and here
        # it is the ordinary case, not an edge.
        source = page_script()
        start = source.index("var cornerNote =")
        note = source[start:source.index("corner-caveat\"].textContent =", start)]
        for cited in ("no corner deviation for this moment", "9,257", "10,682",
                      "86.7%", "3,202", "6,055"):
            with self.subTest(cited):
                self.assertIn(cited, note,
                              "four unlit tyres with nothing beside them are "
                              "an absent measurement read as agreement")

    def test_the_effort_caption_names_the_absent_case(self):
        # The one sentence that stops an unlit shaft being read as coasting.
        # It has to state the sparseness too: "no reading" is only credible
        # if the page says how often that happens.
        source = page_script()
        start = source.index("var effortNote =")
        note = source[start:source.index("effort-caveat\"].textContent =", start)]
        for cited in ("no effort reading for this moment", "2,756", "10,682",
                      "25.8%", "198"):
            with self.subTest(cited):
                self.assertIn(cited, note,
                              "an unlit halfshaft with nothing beside it is a "
                              "gap in the log read as a measurement")

    def test_the_captions_are_written_only_when_they_change(self):
        # All three of these strings are hundreds of characters and
        # renderVehicle
        # runs about eleven times a second during playback and again on every
        # scrub input. Assigning textContent rebuilds the node's text, with
        # the style, layout and paint behind it, for a sentence that is
        # almost always identical to the one already there -- which is not
        # free on the Pi Zero 2 W this page is read on.
        source = page_script()
        for name, value in (("cell-caveat", "cellNote"),
                            ("corner-caveat", "cornerNote"),
                            ("effort-caveat", "effortNote")):
            with self.subTest(name):
                self.assertIn(
                    'if (el["%s"].textContent !== %s) {' % (name, value),
                    source,
                    "the caption is re-assigned on every frame",
                )


class EnvelopeGatingTests(unittest.TestCase):
    """The three cell bounds must be read from a source that goes stale.

    They are printed now rather than drawn -- see CellEnvelopeRemovalTests --
    and printing an hour-old voltage to three decimals is the same assertion
    the marks used to make with their height, so the gate is unchanged.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.store = SessionStore(self.directory)
        self.now = datetime.fromisoformat("2026-01-01T12:00:10+00:00").timestamp()

    def write(self, rows):
        path = self.directory / "drive-20260101T120000Z.csv"
        fields = list(dict.fromkeys(key for row in rows for key in row))
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fields)
            writer.writeheader()
            writer.writerows(rows)

    def rows(self):
        return [
            {"utc": "2026-01-01T12:00:00Z", "elapsed_s": 0, "pack_v": 390,
             "pack_a": 20, "speed_kph": 0, "soc_pct": 80, "energy_kwh": 152,
             "cell_min_v": 3.98, "cell_avg_v": 3.99, "cell_max_v": 4.01,
             "cell_spread_mv": 3.0},
            {"utc": "2026-01-01T12:00:10Z", "elapsed_s": 10, "pack_v": 389,
             "pack_a": 40, "speed_kph": 50, "soc_pct": 79, "energy_kwh": 150.1,
             "cell_min_v": 3.97, "cell_avg_v": 3.99, "cell_max_v": 4.02,
             "cell_spread_mv": 5.0},
        ]

    def test_the_three_bounds_are_withheld_once_they_go_stale(self):
        # The defect: `cell_spread_mv` was already gated and `cell_min_v` and
        # friends were not, so on a session gone quiet for an hour the page
        # stated three hour-old voltages while the spread tint, reading the
        # gated column, correctly abstained -- the same columns at the same
        # instant, one asserting and one declining to. The page reads
        # `derived` now, so these three have to empty the way the spread
        # does.
        self.write(self.rows())
        fresh = self.store.snapshot(now=self.now)["derived"]
        self.assertEqual(fresh["cell_min_v"], 3.97)
        self.assertEqual(fresh["cell_avg_v"], 3.99)
        self.assertEqual(fresh["cell_max_v"], 4.02)
        later = self.store.snapshot(now=self.now + 3600)
        self.assertIsNone(later["derived"]["cell_spread_mv"])
        for field in ("cell_min_v", "cell_avg_v", "cell_max_v"):
            with self.subTest(field):
                self.assertIsNone(
                    later["derived"][field],
                    "the caption would print an hour-old voltage to three "
                    "decimals while the tint beside it abstained",
                )
        # And the ungated route the page used to read is still ungated, which
        # is precisely why it may not be read for this.
        self.assertEqual(later["signals"]["cell_min_v"]["status"], "stale")
        self.assertEqual(later["signals"]["cell_min_v"]["value"], 3.97)

    def test_the_page_reads_the_bounds_from_the_gated_source(self):
        # Runs against the source because the fault was WHICH accessor the
        # live branch used, and both spell the column the same way.
        source = page_script()
        for column in ("cell_min_v", "cell_avg_v", "cell_max_v"):
            with self.subTest(column):
                # Boolean rather than assertNotIn/assertIn, whose failure
                # messages would print the entire page script.
                self.assertFalse(
                    'sigValue("%s")' % column in source,
                    "the caption prints these to three decimals, and "
                    "`signals[c].value` survives going stale -- read the "
                    "gated `derived` value instead",
                )
                self.assertTrue('derived("%s")' % column in source,
                                f"{column} is not read from the gated source")


if __name__ == "__main__":
    unittest.main()
