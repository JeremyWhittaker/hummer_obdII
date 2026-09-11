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


def build_parts_with_rot() -> list[dict]:
    """build_parts(), plus each part's rotation."""
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


@unittest.skipIf(NODE is None, "node is not installed on this machine")
class GreenhouseTests(unittest.TestCase):
    """The side glass, asserted as the proportions of a crew cab.

    The side glass was one pane the length of the cab with the B-pillar drawn
    across it beside the front seat cushion, so the pane behind the pillar
    was 1.62 m and the pane ahead of it 0.78 m. The owner said the rear
    windows were twice the length of the front. They were.
    """

    @classmethod
    def setUpClass(cls):
        cls.parts = {p["id"]: p for p in build_parts()}

    def _length(self, pid):
        return self.parts[pid]["s"][0]

    def test_the_front_door_glass_is_longer_than_the_rear(self):
        for side in "lr":
            with self.subTest(side=side):
                front = self._length(f"glass-door-f-{side}")
                rear = self._length(f"glass-door-r-{side}")
                self.assertGreater(front, rear,
                                   f"front {front:.2f} m, rear {rear:.2f} m")
                # Photographs put it near 1.3:1; well inside the band either way.
                self.assertLess(front / rear, 1.6)
                self.assertGreater(front / rear, 1.15)

    def test_the_b_pillar_stands_behind_the_front_seatback(self):
        pillar = self.parts["pillar-b-l"]
        seatback = self.parts["seat-back-driver"]
        self.assertLess(pillar["t"][0] + pillar["s"][0] / 2,
                        seatback["t"][0] - seatback["s"][0] / 2,
                        "the B-pillar is beside or ahead of the front seat")

    def test_the_glass_panes_do_not_overlap_the_pillar_between_them(self):
        front = self.parts["glass-door-f-l"]
        rear = self.parts["glass-door-r-l"]
        pillar = self.parts["pillar-b-l"]
        self.assertGreaterEqual(front["t"][0] - front["s"][0] / 2,
                                pillar["t"][0] + pillar["s"][0] / 2 - 1e-9)
        self.assertLessEqual(rear["t"][0] + rear["s"][0] / 2,
                             pillar["t"][0] - pillar["s"][0] / 2 + 1e-9)

    def test_each_door_handle_sits_on_its_own_door(self):
        front = self.parts["glass-door-f-l"]
        rear = self.parts["glass-door-r-l"]
        h0, h1 = self.parts["handle-l0"], self.parts["handle-l1"]
        self.assertTrue(front["t"][0] - front["s"][0] / 2 < h0["t"][0] < front["t"][0] + front["s"][0] / 2)
        self.assertTrue(rear["t"][0] - rear["s"][0] / 2 < h1["t"][0] < rear["t"][0] + rear["s"][0] / 2)


@unittest.skipIf(NODE is None, "node is not installed on this machine")
class DrivelineTests(unittest.TestCase):
    def test_the_rear_motors_share_one_casing(self):
        # GM: the two rear motors "are housed within the same casing".
        parts = {p["id"]: p for p in build_parts_with_rot()}
        self.assertIn("motor-rear", parts)
        self.assertNotIn("motor-rear-a", parts, "a second rear casing")
        self.assertNotIn("motor-rear-b", parts, "a second rear casing")
        casing = parts["motor-rear"]
        for rotor in ("motor-rear-a-rotor", "motor-rear-b-rotor"):
            r = parts[rotor]
            self.assertLess(abs(r["t"][2]) + r["s"][2] / 2, casing["s"][2] / 2,
                            f"{rotor} sticks out of the casing")


class LayerControlTests(unittest.TestCase):
    """What the Body button does, read off the page rather than assumed.

    The shell defaulted to 0.46 -- drawn, but below the 0.5 the button called
    "on" -- so Body read as unselected while most of a truck was still drawn
    over the hardware. Off has to mean gone.
    """

    def _layers(self):
        m = re.search(r"layers = \{([^}]*)\}", _script())
        self.assertIsNotNone(m)
        return {k.strip(): float(v) for k, v in
                (e.split(":") for e in m.group(1).split(",") if ":" in e)}

    def test_the_body_is_off_by_default_and_off_is_zero(self):
        self.assertEqual(self._layers()["shell"], 0.0)

    def test_every_other_layer_starts_fully_drawn(self):
        for name, value in self._layers().items():
            if name != "shell":
                with self.subTest(layer=name):
                    self.assertEqual(value, 1.0)

    def test_the_toggle_never_leaves_a_layer_half_drawn(self):
        # The old code special-cased the shell to 0.40 on "off".
        block = _script()
        block = block[block.index("function buildLayerControls"):]
        block = block[:block.index("el.layers.appendChild(b)")]
        self.assertIn("next = current > 0.5 ? 0 : 1", block)
        self.assertNotIn("0.40", block)

    def test_brakes_suspension_and_armour_are_their_own_layers(self):
        for layer in ("brakes", "suspension", "armour", "lights"):
            with self.subTest(layer=layer):
                self.assertIn(layer, self._layers())

    def test_there_are_two_views_and_no_section_or_underside(self):
        views = re.search(r"var VIEWS = \[([\s\S]*?)\n  \];", _script()).group(1)
        keys = re.findall(r'key: "(\w+)"', views)
        self.assertEqual(keys, ["exterior", "driver"])
        self.assertNotIn('state.view === "cutaway"', _script())
