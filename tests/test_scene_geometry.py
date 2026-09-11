"""Run the page's own buildScene() in node and check the geometry it produces.

Every other test in this suite tests Python. The vehicle model is 600 lines of
JavaScript that no Python test can reach, and it has now failed twice in ways
that produce NO error anywhere: once when a Python-style string concatenation
killed the whole script, and once when four parts were positioned from a `var`
used above its own declaration.

That second one is the reason this file computes rather than greps. `var`
hoists the name and not the value, so `bedRail - 0.05` evaluated to NaN, the
shader was handed a NaN model matrix, and the bed lamps and two cameras drew
nothing. No exception, no console message, no failing test -- the parts were
simply not there, and the only way to notice was to count them.

So this extracts VEH, the derived constants and buildScene from the page,
executes them, and asserts against the actual list of parts.
"""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path

PAGE = files("hummer_obd").joinpath("dashboard.html").read_text(encoding="utf-8")
NODE = shutil.which("node") or shutil.which("nodejs")


def _script() -> str:
    return re.findall(r"<script[^>]*>([\s\S]*?)</script>", PAGE)[0]


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


def build_parts() -> list[dict]:
    """The parts list the page would build, as data."""
    source = _script()
    veh = re.search(r"var VEH = \{[\s\S]*?\n  \};", source)
    assert veh, "VEH literal not found"
    consts = re.findall(r"^  var (?:HALF_TRACK|AXLE|HALF_W) = [^;]+;", source, re.M)
    assert len(consts) == 3, f"expected 3 derived constants, found {len(consts)}"
    scene = _balanced(source, source.index("function buildScene"))
    harness = (veh.group(0) + "\n" + "\n".join(consts) + "\n" + scene + """
const parts = buildScene();
process.stdout.write(JSON.stringify(parts.map(p => ({
  id: p.id, t: p.t, s: p.s, layer: p.layer, alpha: p.alpha === undefined ? null : p.alpha
}))));
""")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "scene.js"
        path.write_text(harness, encoding="utf-8")
        done = subprocess.run([NODE, str(path)], capture_output=True,
                              text=True, timeout=120)
    assert done.returncode == 0, f"buildScene failed:\n{done.stderr[:800]}"
    return json.loads(done.stdout)


@unittest.skipIf(NODE is None, "node is not installed on this machine")
class CutawayIsASection(unittest.TestCase):
    """The cutaway must draw nothing on the removed side of the plane.

    The first cutaway culled parts whose CENTRE lay on the driver's side and
    kept everything else whole. 154 of 379 parts sit on the centreline -- the
    pack, the drive units, the cabin, the roof, the bed -- so they survived at
    full width with their driver-side faces toward the eye, and the view was
    a side elevation with two wheels missing. The owner said it was not cut in
    half. He was right.

    This mirrors the page's clip rule over the same parts list and asserts the
    property the view exists for: after the cutaway, no drawn geometry extends
    onto the removed side, and the parts that straddled the plane are still
    drawn -- a pack cut at the centreline is still the pack.
    """

    PLANE = -0.02

    @staticmethod
    def _clip(p):
        t, s, r = list(p["t"]), list(p["s"]), p.get("rot")
        axis = 0 if r == "x" else 1 if r == "y" else 2
        hz = s[axis] / 2
        if t[2] < CutawayIsASection.PLANE:
            return None
        if t[2] - hz < 0 < t[2] + hz:
            top = t[2] + hz
            s[axis] = top
            t[2] = top / 2
        return t, s, axis

    def test_nothing_drawn_extends_onto_the_removed_side(self):
        drawn = 0
        worst = 0.0
        for p in build_parts_with_rot():
            got = self._clip(p)
            if got is None:
                continue
            drawn += 1
            t, s, axis = got
            low = t[2] - s[axis] / 2
            worst = min(worst, low)
        self.assertGreater(drawn, 200, "the cutaway drew almost nothing")
        self.assertGreaterEqual(worst, -1e-9,
                                f"drawn geometry reaches z={worst:.3f}, past the plane")

    def test_the_parts_on_the_centreline_are_clipped_not_deleted(self):
        ids = {p["id"] for p in build_parts_with_rot() if self._clip(p) is not None}
        # Concepts, not literal names: the ids are the page's business.
        for concept, pattern in (("pack", r"pack|module|cell"),
                                 ("cabin", r"cabin|seat|dash|cab|cluster"),
                                 ("roof/glass", r"roof|top|greenhouse|glass")):
            self.assertTrue(any(re.search(pattern, i) for i in ids),
                            f"no {concept} part survives the cutaway")


def build_parts_with_rot() -> list[dict]:
    """build_parts(), plus the rotation the clip rule depends on."""
    source = _script()
    veh = re.search(r"var VEH = \{[\s\S]*?\n  \};", source)
    consts = re.findall(r"^  var (?:HALF_TRACK|AXLE|HALF_W) = [^;]+;", source, re.M)
    scene = _balanced(source, source.index("function buildScene"))
    harness = (veh.group(0) + "\n" + "\n".join(consts) + "\n" + scene + """
const parts = buildScene();
process.stdout.write(JSON.stringify(parts.map(p => ({
  id: p.id, t: p.t, s: p.s, rot: p.rot === undefined ? null : p.rot
}))));
""")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "scene.js"
        path.write_text(harness, encoding="utf-8")
        done = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, f"buildScene failed:\n{done.stderr[:800]}"
    return json.loads(done.stdout)


@unittest.skipIf(NODE is None, "node is not installed on this machine")
class ScenePartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parts = build_parts()
        cls.ids = {p["id"] for p in cls.parts}

    def test_the_scene_builds(self):
        self.assertGreater(len(self.parts), 150)

    def test_no_part_has_a_non_finite_position_or_size(self):
        """The bug this file exists for.

        A NaN in a model matrix draws nothing and says nothing. Four parts --
        bedlamp-l, bedlamp-r, cam-bed, cam-tailgate -- were built this way for
        an entire evening because `bedRail` was used ninety lines above its
        own `var`.
        """
        bad = [p["id"] for p in self.parts
               if not all(isinstance(v, (int, float)) and v == v and abs(v) != float("inf")
                          for v in list(p["t"]) + list(p["s"]))]
        self.assertEqual(bad, [], f"parts positioned with NaN or infinity: {bad}")

    def test_no_part_has_zero_extent(self):
        """A zero-scale box is as invisible as a NaN one, and just as quiet.

        Magnitude, not sign: the left-hand rims carry a deliberate negative Z
        scale to mirror the wheel, which is a legitimate way to reuse one mesh
        for both sides. The first version of this test rejected them and was
        wrong to.
        """
        flat = [p["id"] for p in self.parts if any(abs(v) <= 0 for v in p["s"])]
        self.assertEqual(flat, [], f"parts with no extent: {flat}")

    def test_every_part_has_a_unique_id(self):
        ids = [p["id"] for p in self.parts]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        self.assertEqual(dupes, [], f"duplicate part ids: {dupes}")

    def test_the_lamps_gmc_publishes_are_all_present(self):
        # GMC's brochure names four lighting features; all four must render.
        for lamp in ("headlamp-l", "headlamp-r", "taillamp-l", "taillamp-r",
                     "bedlamp-l", "bedlamp-r", "portlamp"):
            with self.subTest(lamp=lamp):
                self.assertIn(lamp, self.ids)

    def test_all_eight_camera_positions_are_present(self):
        for camera in ("cam-front", "cam-rear", "cam-mirror-l", "cam-mirror-r",
                       "cam-under-f", "cam-under-r", "cam-bed", "cam-tailgate"):
            with self.subTest(camera=camera):
                self.assertIn(camera, self.ids)

    def test_the_cab_has_a_rear_window(self):
        # There was none. Which made "the bed rests below the rear window" a
        # claim about a part that did not exist.
        self.assertIn("glass-back", self.ids)

    def test_the_bed_sits_entirely_below_the_glass_line(self):
        """The complaint, asserted as geometry rather than trusted to a diff.

        It was not satisfied after the first attempt: the pre-rework bed walls
        were never deleted, and two of them stood 0.18 m and 0.24 m above the
        bottom of the glass while the new box sat correctly below it.
        """
        glass = [p for p in self.parts if p["id"].startswith("glass")]
        self.assertTrue(glass, "no glass to compare against")
        glass_bottom = min(p["t"][1] - p["s"][1] / 2 for p in glass)
        offenders = [
            (p["id"], round(p["t"][1] + p["s"][1] / 2, 3))
            for p in self.parts
            if p["id"].startswith("bed") and p["t"][1] + p["s"][1] / 2 > glass_bottom + 1e-6
        ]
        self.assertEqual(offenders, [],
                         f"bed parts above the glass bottom ({glass_bottom:.3f}): "
                         f"{offenders}")

    def test_the_light_bar_spells_six_letters(self):
        letters = sorted(i for i in self.ids if i.startswith("lightbar-letter-"))
        self.assertEqual(len(letters), 6, f"HUMMER is six letters: {letters}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


@unittest.skipIf(NODE is None, "node is not installed on this machine")
class IntersectionTests(unittest.TestCase):
    """Parts must not occupy the same space as other parts.

    A twelve-agent audit found the model riddled with these and every one of
    them survived every screenshot, because each piece looks correct alone and
    only the pair is wrong: coolant lines cutting an 0.857 m chord through both
    front tyres, halfshafts skewering all four air springs, the HV cable
    running through the left rear wheel, the charge cord plugged into a wheel
    arch. Nothing in a render says "these two overlap".
    """

    @classmethod
    def setUpClass(cls):
        cls.parts = [p for p in build_parts() if not p["id"].startswith("evse")]

    @staticmethod
    def _box(p):
        return [(p["t"][i] - abs(p["s"][i]) / 2, p["t"][i] + abs(p["s"][i]) / 2)
                for i in range(3)]

    @classmethod
    def _overlap(cls, a, b, slack=1e-6):
        ba, bb = cls._box(a), cls._box(b)
        return all(ba[i][0] < bb[i][1] - slack and bb[i][0] < ba[i][1] - slack
                   for i in range(3))

    #: A halfshaft enters the wheel it drives. That is what a halfshaft is.
    ALLOWED_IN_WHEEL = {"shaft-f", "shaft-r"}

    def test_nothing_runs_through_a_wheel(self):
        wheels = [p for p in self.parts if p["id"].startswith(("tyre-", "rim-"))]
        self.assertTrue(wheels, "no wheels to test against")
        offenders = set()
        for wheel in wheels:
            for part in self.parts:
                if part["id"].startswith(("tyre-", "rim-", "brake-", "flare-",
                                          "wheel-", "hub")):
                    continue
                if part["layer"] in ("shell", "cabin"):
                    continue
                if part["id"] in self.ALLOWED_IN_WHEEL:
                    continue
                if self._overlap(wheel, part):
                    offenders.add(part["id"])
        self.assertEqual(sorted(offenders), [],
                         f"parts inside a wheel: {sorted(offenders)}")

    def test_nothing_on_the_vehicle_is_wider_than_the_vehicle(self):
        """GMC publishes 2.202 m across the flares and 2.380 across mirrors.

        The rock rails were the widest thing on the model, and the flares --
        which DEFINE the 2.202 -- were hung outboard of it.
        """
        limit = 1.101 + 1e-3
        mirrors = {"mirror-l", "mirror-r", "cam-mirror-l", "cam-mirror-r"}
        wide = [(p["id"], round(max(abs(v) for v in self._box(p)[2]), 3))
                for p in self.parts
                if p["id"] not in mirrors
                and max(abs(v) for v in self._box(p)[2]) > limit]
        self.assertEqual(wide, [], f"wider than the published half-width: {wide}")

    def test_nothing_hangs_below_the_published_ground_clearance(self):
        # The skid plates -- the vehicle's own armour -- used to be the
        # violation, bottoming out 0.072 m below the figure in the same table.
        clearance = 0.2565 - 1e-3
        low = [(p["id"], round(self._box(p)[1][0], 3)) for p in self.parts
               if not p["id"].startswith(("tyre-", "rim-", "brake-"))
               and self._box(p)[1][0] < clearance]
        self.assertEqual(low, [], f"below the ground clearance: {low}")


class HonestyTests(unittest.TestCase):
    """What the model draws confidently and cannot source, it must say so.

    The README called the thermal plumbing unverified while the page drew 28
    thermal parts with no caveat anywhere a reader would see it. Two stories
    about the same geometry, and the one nobody reads was the honest one.
    """

    def test_the_unsourced_thermal_plumbing_says_so_on_the_model(self):
        self.assertIn("The plumbing itself is UNSOURCED", PAGE)

    def test_the_measured_temperature_is_distinguished_from_the_guessed_shapes(self):
        # The tint is a real reading; the pipes are placeholders. A caveat that
        # tarred both would be as misleading as none.
        self.assertIn("the temperature above is measured", PAGE)

    @unittest.skipIf(NODE is None, "node is not installed on this machine")
    def test_the_caveat_covers_however_many_parts_are_actually_drawn(self):
        # If the thermal layer is ever emptied the caveat should go with it,
        # and if it grows the caveat still applies -- this asserts the pairing
        # rather than a count that would rot.
        thermal = [p for p in build_parts() if p["layer"] == "thermal"]
        if thermal:
            self.assertIn("UNSOURCED", PAGE)
