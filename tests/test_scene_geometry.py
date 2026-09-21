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
import math
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path

import reference_skin

PAGE = files("hummer_obd").joinpath("dashboard.html").read_text(encoding="utf-8")
NODE = shutil.which("node") or shutil.which("nodejs")


_SKIN: dict = {}


def skin() -> dict:
    """The body the page draws: its triangles and 5 cm extent maps, built once."""
    if not _SKIN:
        tris = reference_skin.triangles(PAGE)
        _SKIN["tris"] = tris
        _SKIN["x"], _SKIN["y"], _SKIN["z"] = (reference_skin.Extents(tris, a) for a in range(3))
    return _SKIN


def _glass_behind_the_cab() -> list:
    return [p for name, *abc in skin()["tris"] if name == "glass" for p in abc if p[0] < -1.10]


def _windshield() -> list:
    return [p for name, *abc in skin()["tris"] if name == "glass" for p in abc
            if abs(p[2]) < 0.05 and p[0] > 0.5]


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


#: The top-level declarations `buildScene` closes over. TYRE_DIFFUSE's line
#: also declares SHAFT_DIFFUSE: both are the diffuse colours of parts whose
#: legend swatch has to be shaded from the same array the scene is built with,
#: so they live beside `swatchHex` rather than inside `buildScene`.
SCENE_CONSTS = r"^  var (?:HALF_TRACK|AXLE|HALF_W|TYRE_DIFFUSE) = [^;]+;"


def build_parts() -> list[dict]:
    """The parts list the page would build, as data."""
    source = _script()
    veh = re.search(r"var VEH = \{[\s\S]*?\n  \};", source)
    assert veh, "VEH literal not found"
    consts = re.findall(SCENE_CONSTS, source, re.M)
    assert len(consts) == 4, f"expected 4 derived constants, found {len(consts)}"
    scene = _balanced(source, source.index("function buildScene"))
    harness = (veh.group(0) + "\n" + "\n".join(consts) + "\n" + scene + """
const parts = buildScene();
// A part turned about Y reports the world box it occupies as s, so every box
// test here stays true of it; its own size and angle come as size and yaw.
process.stdout.write(JSON.stringify(parts.map(p => {
  const c = Math.abs(Math.cos(p.yaw || 0)), n = Math.abs(Math.sin(p.yaw || 0));
  return {
    id: p.id, t: p.t, layer: p.layer, alpha: p.alpha === undefined ? null : p.alpha,
    s: p.yaw ? [p.s[0] * c + p.s[2] * n, p.s[1], p.s[0] * n + p.s[2] * c] : p.s,
    size: p.s, yaw: p.yaw === undefined ? null : p.yaw
  };
})));
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
    consts = re.findall(SCENE_CONSTS, source, re.M)
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
        # claim about a part that did not exist. The body's glass carries one:
        # a pane across the cab back, behind the rear doors.
        back = _glass_behind_the_cab()
        self.assertTrue(back, "no glass behind the rear doors")
        span = max(p[2] for p in back) - min(p[2] for p in back)
        self.assertGreater(span, 1.0, f"the rear window spans only {span:.2f} m")

    def test_the_bed_sits_entirely_below_the_rear_window(self):
        """The complaint, asserted as geometry rather than trusted to a diff.

        "Clearly the bed rests below the rear window in its entirety." The box
        bed failed it twice -- the second time because old walls were left
        standing in front of the new box. The body now draws both, so this
        measures the drawn skin: the top of the bedsides all along the bed
        against the lowest edge of the rear window. The sail forward of
        x -1.55 is the cab's buttress, not the bed.
        """
        top = skin()["y"]
        glass_bottom = min(p[1] for p in _glass_behind_the_cab())
        rails = [top.at(x / 100, z / 100) for x in range(-270, -155, 5)
                 for z in (-90, -85, -80, 80, 85, 90)]
        rails = [r for r in rails if r is not None]
        self.assertTrue(rails, "no bedside found")
        self.assertLess(max(rails), glass_bottom,
                        f"bedside at {max(rails):.3f}, rear window from {glass_bottom:.3f}")
        for lamp in ("bedlamp-l", "bedlamp-r"):
            with self.subTest(lamp=lamp):
                p = next(q for q in self.parts if q["id"] == lamp)
                self.assertLess(p["t"][1] + p["s"][1] / 2, max(rails), "above the rail")

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
        """Through the tyre's rubber, or through the wheel's face.

        The tyre's box is the wrong test: the knuckle, the brake hat and the
        ball joints belong inside the wheel, within the rim, and a box test
        called every one of them a collision. So a part runs through a wheel
        if, across the tyre's width, some of it lies between the bead and the
        tread -- or if it crosses the wheel's mounting face inside the bead.
        The drawn spoke disc is not that face: it sits proud of the tyre's
        outer sidewall. A 9.5Jx22 ET33 wheel mounts 33 mm outboard of the
        tyre's centre plane.
        """
        offenders = set()
        for tyre in [p for p in self.parts if p["id"].startswith("tyre-")]:
            cx, cy = tyre["t"][0], tyre["t"][1]
            radius = abs(tyre["s"][0]) / 2
            bead = 0.2794                               # 22 in rim
            (tz0, tz1) = self._box(tyre)[2]
            face = tyre["t"][2] + math.copysign(0.033, tyre["t"][2])
            for part in self.parts:
                if part["id"].startswith(("tyre-", "rim-", "brake-", "flare-", "wheel-", "hub")):
                    continue
                if part["layer"] in ("shell", "cabin") or part["id"] in self.ALLOWED_IN_WHEEL:
                    continue
                (x0, x1), (y0, y1), (z0, z1) = self._box(part)
                dx, dy = max(x0 - cx, 0, cx - x1), max(y0 - cy, 0, cy - y1)
                near = (dx * dx + dy * dy) ** 0.5
                far = max(((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 for x in (x0, x1) for y in (y0, y1))
                in_rubber = z0 < tz1 and tz0 < z1 and near < radius and far > bead
                through_face = z0 < face < z1 and near < bead
                if in_rubber or through_face:
                    offenders.add(part["id"])
        self.assertEqual(sorted(offenders), [], f"parts through a wheel: {sorted(offenders)}")

    def test_nothing_on_the_vehicle_is_wider_than_the_vehicle(self):
        """GMC publishes 2.202 m across the flares and 2.380 across mirrors.

        The rock rails were the widest thing on the model, and the flares --
        which DEFINE the 2.202 -- were hung outboard of it.
        """
        limit = 1.101 + 1e-3
        mirrors = {"cam-mirror-l", "cam-mirror-r", "body-mirror-l-paint", "body-mirror-l-trim",
                   "body-mirror-r-paint", "body-mirror-r-trim"}
        wide = [(p["id"], round(max(abs(v) for v in self._box(p)[2]), 3))
                for p in self.parts
                if p["id"] not in mirrors
                and max(abs(v) for v in self._box(p)[2]) > limit]
        self.assertEqual(wide, [], f"wider than the published half-width: {wide}")
        # And the mirrors to GMC's 2.380 m across them, inside the body
        # model's own 0.6 % in width.
        reach = max(max(abs(v) for v in self._box(p)[2]) for p in self.parts if p["id"] in mirrors)
        self.assertLess(reach, 1.190 + 0.010, f"mirrors reach {reach:.3f}")

    def test_nothing_stands_above_the_published_overall_height(self):
        # GMC's 79.1 in. The roof rails and the old roof markers both did.
        tall = [(p["id"], round(self._box(p)[1][1], 3)) for p in self.parts
                if self._box(p)[1][1] > 2.009 + 1e-3]
        self.assertEqual(tall, [], f"above the overall height: {tall}")

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
    """The greenhouse, now that the body draws it.

    The box greenhouse was pinned here from GM's rescue sheet: the door glass
    ratio, the B-pillar beside the seatback, the handles on their doors. The
    body model carries those as drawn surfaces, so what is asserted now is
    how this project's own parts sit against it: the seat beside the pillar,
    the instrument panel against the glass, the windshield raked.
    """

    @classmethod
    def setUpClass(cls):
        cls.parts = {p["id"]: p for p in build_parts()}

    def test_the_windshield_is_raked_not_vertical(self):
        pane = _windshield()
        self.assertTrue(pane, "no windshield on the centreline")
        base, top = min(pane, key=lambda p: p[1]), max(pane, key=lambda p: p[1])
        self.assertLess(top[0], base[0] - 0.10, "the top of the windshield is not behind its base")

    def test_the_instrument_panel_is_against_the_windshield_not_behind_it(self):
        # The box dash stood behind a windshield at the rescue sheet's cowl,
        # 0.21 m aft of where the body's glass meets it; drawn behind the
        # body's glass it would have floated there in plain sight.
        dash = self.parts["dash-pad"]
        front = dash["t"][0] + dash["s"][0] / 2
        base = min(_windshield(), key=lambda p: p[1])
        self.assertGreater(base[0] - front, 0.0, "the dash goes through the glass")
        self.assertLess(base[0] - front, 0.06, "the dash stops short of the glass")

    def test_the_front_seatback_is_beside_the_b_pillar(self):
        """The owner's complaint was a B-pillar beside the seat cushion.

        The pillar is where, at shoulder height between the doors, the body
        stands outboard of the side glass. The seatback must be beside it:
        within a hand's width of the seatback, fore or aft.
        """
        glass = reference_skin.Extents([x for x in skin()["tris"] if x[0] == "glass"], 2)
        body = reference_skin.Extents([x for x in skin()["tris"] if x[0] != "glass"], 2)
        pillar = [x / 100 for x in range(-60, 61, 5)
                  if body.at(x / 100, 1.60, "lo") is not None and glass.at(x / 100, 1.60, "lo") is not None
                  and body.at(x / 100, 1.60, "lo") < glass.at(x / 100, 1.60, "lo") - 0.005]
        self.assertTrue(pillar, "no B-pillar found between the doors")
        centre = sum(pillar) / len(pillar)
        back = self.parts["seat-back-driver"]
        self.assertGreater(centre, back["t"][0] - back["s"][0] / 2 - 0.15, "the pillar is well behind the seatback")
        self.assertLess(centre, back["t"][0] + back["s"][0] / 2 + 0.15, "the pillar is beside the cushion, not the seatback")

    def test_the_pack_is_where_the_rescue_sheet_draws_it(self):
        case = self.parts["pack-case"]
        # 2.09 m long, bottom at 0.43 m. The sheet puts its centre 0.13 m ahead
        # of the wheelbase midpoint; it is drawn at 0.03, inside the sheet's
        # 0.10 m error, because at 0.13 its case sat in the front wheel wells.
        self.assertAlmostEqual(case["t"][0], 0.03, places=2)
        self.assertLess(case["s"][0], 2.30)
        self.assertGreater(case["t"][1] - case["s"][1] / 2, 0.38)

    def test_every_corner_has_a_spring_and_a_damper(self):
        for corner in ("fl", "fr", "rl", "rr"):
            with self.subTest(corner=corner):
                self.assertIn(f"airspring-{corner}", self.parts)
                self.assertIn(f"damper-{corner}", self.parts)


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


@unittest.skipIf(NODE is None, "node is not installed on this machine")
class UnderbodyStaysUnderTheBody(unittest.TestCase):
    """What sits under the truck must not stand up through it.

    The rear air springs were 0.42 m tall on a 0.66 m centre, topping out at
    0.87 m, with the bed floor at 0.75 m: the bellows stood up through the
    bed. The owner asked whether they really do that. They do not.
    """

    @classmethod
    def setUpClass(cls):
        cls.parts = build_parts()
        cls.by_id = {p["id"]: p for p in cls.parts}

    def test_no_suspension_part_rises_through_the_bed(self):
        """The bed is the body's now, and a strut may not stand up through it.

        Over the bed the highest skin is the floor, or the wheelhouse where
        the body rises over the rear wheels, so every top corner of a
        suspension part under the bed must be below the skin above it.
        """
        top = skin()["y"]
        tall = []
        for p in self.parts:
            if p["layer"] != "suspension":
                continue
            (x0, x1), _, (z0, z1) = [(p["t"][i] - abs(p["s"][i]) / 2, p["t"][i] + abs(p["s"][i]) / 2)
                                     for i in range(3)]
            if x1 < -2.76 or x0 > -1.234:
                continue
            y1 = p["t"][1] + abs(p["s"][1]) / 2
            for x in (x0, x1):
                for z in (z0, z1):
                    above = top.at(x, z)
                    if above is not None and y1 > above + 1e-6:
                        tall.append((p["id"], round(y1, 3), round(above, 3)))
        self.assertEqual(tall, [], f"through the bed: {tall}")

    def test_thermal_parts_are_only_where_something_sourced_puts_them(self):
        """Inside the pack, or at the nose. Nothing in between.

        Two 3.3 m coolant lines used to run from the pack to nowhere at wheel
        height, past the front tyres. The model called them unsourced and
        drew them anyway; the owner saw tubes that do not run under his
        tyres. What is sourced is coolant through every module and a
        radiator and chiller at the front, so that is where thermal parts
        may be.
        """
        case = self.by_id["pack-case"]
        px0 = case["t"][0] - case["s"][0] / 2 - 0.05
        px1 = case["t"][0] + case["s"][0] / 2 + 0.05
        pz = case["s"][2] / 2 + 0.05
        stray = []
        for p in self.parts:
            if p["layer"] != "thermal":
                continue
            x, y, z = p["t"]
            in_pack = px0 <= x <= px1 and abs(z) <= pz
            at_nose = x > 1.75            # ahead of the front axle
            if not (in_pack or at_nose):
                stray.append((p["id"], round(x, 2), round(z, 2)))
        self.assertEqual(stray, [], f"thermal parts drawn where nothing sourced puts them: {stray}")


@unittest.skipIf(NODE is None, "node is not installed on this machine")
class ChargeStatusIndicatorTests(unittest.TestCase):
    """The headlight CSI, as the owner's manual describes it.

    "The headlight CSI bar is located on the headlamps. As charging occurs,
    the blue light bars on the headlamps fill towards the center of the
    vehicle." So: bars inside each headlamp housing, indexed from the outer
    edge, and the fill code lights index 0 first.
    """

    @classmethod
    def setUpClass(cls):
        cls.parts = {p["id"]: p for p in build_parts()}

    def test_each_headlamp_carries_a_row_of_bars_inside_its_housing(self):
        for side in "lr":
            with self.subTest(side=side):
                strips = [p for i, p in self.parts.items()
                          if i == f"headlamp-{side}" or i.startswith(f"headlamp-{side}-seg-")]
                bars = [p for i, p in self.parts.items() if i.startswith(f"headlamp-{side}-bar-")]
                self.assertGreaterEqual(len(bars), 4)
                z0 = min(p["t"][2] - abs(p["s"][2]) / 2 for p in strips)
                z1 = max(p["t"][2] + abs(p["s"][2]) / 2 for p in strips)
                for b in bars:
                    self.assertTrue(z0 <= b["t"][2] - b["s"][2] / 2 and b["t"][2] + b["s"][2] / 2 <= z1,
                                    f"{b['id']} is outside its housing")
                    # On the housing, not floating ahead of it: within the lens's
                    # own depth, which is up to 5 cm where a bar sits.
                    bz0, bz1 = b["t"][2] - b["s"][2] / 2, b["t"][2] + b["s"][2] / 2
                    under = [p["t"][0] + p["s"][0] / 2 for p in strips
                             if p["t"][2] - abs(p["s"][2]) / 2 < bz1 and bz0 < p["t"][2] + abs(p["s"][2]) / 2]
                    gap = (b["t"][0] - b["s"][0] / 2) - max(under)
                    self.assertTrue(0 <= gap <= 0.05, f"{b['id']} is {gap:.3f} m off its housing")

    def test_bar_zero_is_the_outermost_so_the_fill_runs_toward_the_centre(self):
        for side in "lr":
            with self.subTest(side=side):
                bars = sorted((p for i, p in self.parts.items()
                               if i.startswith(f"headlamp-{side}-bar-")),
                              key=lambda p: int(p["id"].rsplit("-", 1)[1]))
                outer = abs(bars[0]["t"][2])
                inner = abs(bars[-1]["t"][2])
                self.assertGreater(outer, inner, "bar 0 is not the outermost")

    def test_the_letters_no_longer_carry_a_charge_fill(self):
        block = _script()
        block = block[block.index("if (p.letter !== undefined) {"):]
        block = block[:block.index("if (p.csi !== undefined) {")]
        self.assertNotIn("live.soc", block, "the letters still fill with state of charge")


@unittest.skipIf(NODE is None, "node is not installed on this machine")
class FuseBlockTests(unittest.TestCase):
    """The three 12 V fuse blocks, where the owner's manual says they are."""

    @classmethod
    def setUpClass(cls):
        cls.parts = {p["id"]: p for p in build_parts()}

    def test_all_three_are_drawn_on_the_12v_layer(self):
        for pid in ("fusebox-underhood", "fusebox-ip-left", "fusebox-ip-right"):
            with self.subTest(block=pid):
                self.assertIn(pid, self.parts)
                self.assertEqual(self.parts[pid]["layer"], "aux")

    def test_the_underhood_block_is_under_the_hood_on_the_driver_side(self):
        # "Underhood Compartment Fuse Block"; the battery is on the passenger
        # side and the block's access cover is on the left.
        fb = self.parts["fusebox-underhood"]
        self.assertLess(fb["t"][2], -0.4, "not on the driver's side")
        top = fb["t"][1] + fb["s"][1] / 2
        for dx in (-1, 1):
            for dz in (-1, 1):
                hood = skin()["y"].at(fb["t"][0] + dx * fb["s"][0] / 2, fb["t"][2] + dz * fb["s"][2] / 2)
                self.assertGreater(hood, top, "stands up through the hood")
        self.assertGreater(fb["t"][0] - fb["s"][0] / 2, 1.165, "not ahead of the windshield")

    def test_the_two_panel_blocks_flank_the_instrument_panel(self):
        # Left: "driver side of the instrument panel, between the steering
        # wheel and the door". Right: "behind the glove box".
        left, right = self.parts["fusebox-ip-left"], self.parts["fusebox-ip-right"]
        wheel = self.parts["wheel-rim"]
        self.assertLess(left["t"][2], wheel["t"][2], "left block is inboard of the wheel")
        self.assertGreater(right["t"][2], 0.3, "right block is not on the passenger side")
        dash = self.parts["dash-pad"]
        for fb in (left, right):
            self.assertLess(fb["t"][1] + fb["s"][1] / 2, dash["t"][1] + dash["s"][1] / 2,
                            "a fuse block stands above the dash top")


@unittest.skipIf(NODE is None, "node is not installed on this machine")
class HighVoltageRunTests(unittest.TestCase):
    """The HV cables the rescue sheet draws: pack wall to drive-unit housing."""

    @classmethod
    def setUpClass(cls):
        cls.parts = {p["id"]: p for p in build_parts()}

    def _x_span(self, pid):
        p = self.parts[pid]
        return p["t"][0] - p["s"][0] / 2, p["t"][0] + p["s"][0] / 2

    def test_each_run_joins_the_pack_to_its_drive_unit(self):
        pack = self._x_span("pack-case")
        front = self._x_span("motor-front")
        rear = self._x_span("motor-rear")
        f0, f1 = self._x_span("hv-run-front")
        r0, r1 = self._x_span("hv-run-rear")
        # Front run: starts at (or inside) the pack's front wall, ends at the
        # front housing. Rear run: the mirror.
        self.assertLessEqual(f0, pack[1] + 0.07)
        self.assertGreaterEqual(f1, front[0] - 0.01)
        self.assertGreaterEqual(r1, pack[0] - 0.07)
        self.assertLessEqual(r0, rear[1] + 0.01)

    def test_an_inverter_sits_on_each_drive_unit(self):
        for tpim, unit in (("tpim-1", "motor-front"), ("tpim-2", "motor-rear"), ("tpim-3", "motor-rear")):
            with self.subTest(tpim=tpim):
                m, u = self.parts[tpim], self.parts[unit]
                self.assertGreater(m["t"][1] - m["s"][1] / 2, u["t"][1], "inverter is not above the housing")
                self.assertLess(abs(m["t"][0] - u["t"][0]), 0.2, "inverter is not on its drive unit")


@unittest.skipIf(NODE is None, "node is not installed on this machine")
class CabinSitsOnAFloor(unittest.TestCase):
    """There is a floor, it is above the pack, and the seats are on it.

    The seats stood 0.015 m inside the battery case with no floor drawn at
    all. "Clearly this cannot be accurate because there would be nowhere to
    put your feet." Correct.
    """

    @classmethod
    def setUpClass(cls):
        cls.parts = {p["id"]: p for p in build_parts()}

    def _top(self, pid): return self.parts[pid]["t"][1] + self.parts[pid]["s"][1] / 2
    def _bottom(self, pid): return self.parts[pid]["t"][1] - self.parts[pid]["s"][1] / 2

    def test_the_floor_lies_on_the_pack_not_in_it(self):
        self.assertGreaterEqual(self._bottom("cabin-floor"), self._top("pack-case") - 1e-9)

    def test_every_seat_cushion_stands_clear_of_the_floor(self):
        floor = self._top("cabin-floor")
        for pid in ("seat-base-driver", "seat-base-passenger", "seat-rear-base"):
            with self.subTest(seat=pid):
                self.assertGreater(self._bottom(pid), floor + 0.03, "no room for feet")

    def test_no_cabin_part_enters_the_pack(self):
        pack_top = self._top("pack-case")
        low = [(i, round(self._bottom(i), 3)) for i, p in self.parts.items()
               if p["layer"] == "cabin" and self._bottom(i) < pack_top - 1e-9]
        self.assertEqual(low, [], f"cabin parts inside the pack: {low}")

    def test_headrests_clear_the_headliner(self):
        # The body has no headliner, only its roof skin, so the allowance is
        # 0.15 m under the skin above every corner of every headrest -- tight
        # enough that the box seats' 1.86 m front headrests fail it, with room
        # for a rear bench that GMC's rear head room puts higher than the front.
        top = skin()["y"]
        for i, p in self.parts.items():
            if i.startswith("headrest"):
                with self.subTest(part=i):
                    for dx in (-1, 1):
                        for dz in (-1, 1):
                            roof = top.at(p["t"][0] + dx * p["s"][0] / 2, p["t"][2] + dz * p["s"][2] / 2)
                            self.assertLess(self._top(i), roof - 0.15)

    def test_the_pedals_are_on_the_toe_board_ahead_of_the_driver(self):
        toe = self.parts["cabin-toeboard"]
        for pid in ("pedal-accel", "pedal-brake"):
            with self.subTest(pedal=pid):
                p = self.parts[pid]
                self.assertLess(abs(p["t"][0] - (toe["t"][0] - toe["s"][0] / 2)), 0.06)
                self.assertLess(p["t"][2], 0, "pedals are not on the driver's side")


@unittest.skipIf(NODE is None, "node is not installed on this machine")
class ReferenceBodyTests(unittest.TestCase):
    """The body is a third-party model. These hold it to what it was checked against.

    "Hummer EV - Low Poly" by Ajay Gawde, CC-BY-4.0, rebuilt into the page by
    scripts/build_reference_body.py. It was adopted because in plan it agrees
    with GMC's published dimensions to 0.6 % (the front overhang to 1.1 %), and
    its heights were fitted to two published figures; if a rebuild ever breaks
    that, or anyone edits the embedded data by hand, these fail.
    """

    @classmethod
    def setUpClass(cls):
        cls.parts = {p["id"]: p for p in build_parts()}
        cls.tris = skin()["tris"]

    def test_the_embedded_body_is_what_the_build_script_wrote(self):
        # Every other test here reads REF_BODY through REF_PLACEMENTS, so a hand
        # edit to either moves the whole body and every check moves with it.
        # The digest the build script writes beside them is what cannot move.
        self.assertIsNotNone(reference_skin.digest_parts(PAGE), "no REF_DIGEST beside REF_BODY")
        self.assertTrue(reference_skin.digest_ok(PAGE),
                        "REF_BODY or REF_PLACEMENTS differ from what the build script wrote")

    @unittest.skipUnless((Path(__file__).resolve().parents[1] / "3d" / "hummer_ev_-_low_poly.glb").is_file(),
                         "the downloaded model is not in 3d/ on this machine")
    def test_the_build_script_still_reproduces_the_page(self):
        try:
            import numpy  # noqa: F401
            import trimesh  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError:
            self.skipTest("numpy/trimesh/Pillow are not installed; see the script's docstring")
        repo = Path(__file__).resolve().parents[1]
        done = subprocess.run([sys.executable, str(repo / "scripts" / "build_reference_body.py"),
                               str(repo / "3d" / "hummer_ev_-_low_poly.glb"), "--check"],
                              capture_output=True, text=True, timeout=600)
        self.assertEqual(done.returncode, 0, done.stderr[-800:])

    def test_the_body_is_lit_with_its_own_surface_normals(self):
        """The shader draws mat3(uModel) * aNormal, the stored normal times the
        group's box size. That must face the way the drawn triangles do.

        The first build divided the normals by the size squared; the digest
        test cannot see a mistake like that once the page is rebuilt from it,
        and the windshield was lit about 30 degrees off. Measured here against
        the triangles' own face normals, weighted by area.
        """
        import base64
        import math
        import struct
        placed = reference_skin.placements(PAGE)
        for group in reference_skin.ref_groups(PAGE):
            name = group["name"]
            with self.subTest(group=name):
                t, s = placed[name]
                raw = {k: base64.b64decode(group[k]) for k in "vni"}
                v = struct.unpack("<%dh" % (len(raw["v"]) // 2), raw["v"])
                n = struct.unpack("<%db" % len(raw["n"]), raw["n"])
                idx = struct.unpack("<%dH" % (len(raw["i"]) // 2), raw["i"])
                pts = [[t[a] + v[k + a] / 65534 * s[a] for a in range(3)] for k in range(0, len(v), 3)]
                drawn = [[n[k + a] * s[a] for a in range(3)] for k in range(0, len(n), 3)]
                total = weight = 0.0
                for k in range(0, len(idx), 3):
                    a, b, c = (pts[i] for i in idx[k:k + 3])
                    e1 = [b[i] - a[i] for i in range(3)]
                    e2 = [c[i] - a[i] for i in range(3)]
                    face = [e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2],
                            e1[0] * e2[1] - e1[1] * e2[0]]
                    area = math.hypot(*face)
                    if area < 1e-12:
                        continue
                    for vi in idx[k:k + 3]:
                        m = drawn[vi]
                        cos = abs(sum(face[i] * m[i] for i in range(3))) / (area * (math.hypot(*m) or 1))
                        total += area * math.degrees(math.acos(min(1.0, cos)))
                        weight += area
                self.assertLess(total / weight, 10.0, f"{name} lit {total / weight:.1f} degrees off its surface")

    def test_every_group_is_drawn_on_its_own_bounding_box(self):
        placed = reference_skin.placements(PAGE)
        for group in reference_skin.ref_groups(PAGE):
            name = group["name"]
            with self.subTest(group=name):
                pts = [p for g, *abc in self.tris if g == name for p in abc]
                t, s = placed[name]
                for a in range(3):
                    self.assertAlmostEqual(min(p[a] for p in pts), t[a] - s[a] / 2, delta=2e-4)
                    self.assertAlmostEqual(max(p[a] for p in pts), t[a] + s[a] / 2, delta=2e-4)
                part = [p for p in self.parts.values() if p["id"].endswith(name.replace("body-", ""))
                        and p["layer"] == "shell" and p["t"] == t]
                self.assertEqual(len(part), 1, "not placed as exactly one part")
                self.assertEqual(part[0]["s"], s)

    def test_the_body_is_credited_where_it_is_shown_and_where_it_is_licensed(self):
        credit = re.search(r'<p class="scene-credit">([\s\S]*?)</p>', PAGE)
        self.assertIsNotNone(credit, "no credit under the 3D view")
        for needed in ("Hummer EV - Low Poly", "Ajay Gawde", "CC BY 4.0",
                       "https://creativecommons.org/licenses/by/4.0/",
                       "https://sketchfab.com/3d-models/hummer-ev-low-poly-12622086af0449eda09f9d2ce5596090"):
            with self.subTest(needed=needed):
                self.assertIn(needed, credit.group(1))
        # CC BY 4.0 s.3(a)(1)(B): say that it was modified.
        self.assertIn("removed", credit.group(1))
        rule = re.search(r"\.scene-credit\{([^}]*)\}", PAGE)
        self.assertIsNotNone(rule, "no style for the credit")
        for hidden in ("display:none", "visibility:hidden", "opacity:0"):
            self.assertNotIn(hidden, rule.group(1).replace(" ", ""))
        licence = (Path(__file__).resolve().parents[1] / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("THIRD-PARTY MATERIAL", licence)
        self.assertIn("Ajay Gawde", licence)
        self.assertIn("Changes made", licence)

    def test_the_body_agrees_with_the_published_dimensions(self):
        pts = [p for g, *abc in self.tris for p in abc]
        body = [p for g, *abc in self.tris if not g.startswith("mirror") for p in abc]
        mirrors = [p for g, *abc in self.tris if g.startswith("mirror") for p in abc]
        length = max(p[0] for p in pts) - min(p[0] for p in pts)
        self.assertLess(abs(length - 5.507) / 5.507, 0.005, f"length {length:.3f}")
        self.assertLess(abs(max(p[0] for p in pts) - 2.603), 0.02, "nose")
        self.assertLess(abs(min(p[0] for p in pts) + 2.903), 0.02, "tail")
        self.assertAlmostEqual(max(p[1] for p in pts), 2.009, delta=0.003)
        half = max(abs(p[2]) for p in body)
        self.assertLessEqual(half, 1.101 + 1e-3, f"half-width {half:.3f}")
        self.assertGreater(half, 1.101 * 0.99, f"half-width {half:.3f}")
        self.assertLess(abs(max(abs(p[2]) for p in mirrors) - 1.190) / 1.190, 0.01)
        top = skin()["y"]
        depth = top.at(-2.2, 0.82) - top.at(-2.2, 0.0)
        self.assertLess(abs(depth - 0.5512), 0.01, f"bed depth {depth:.3f}")

    def test_the_body_carries_no_interior_or_wheel_hardware(self):
        # The model came with seats, a wheel, discs, calipers and coil-overs.
        # This page draws its own of all of those; two of each is a lie.
        cabin = [p for g, *abc in self.tris for p in abc
                 if -0.9 < p[0] < 0.6 and 0.9 < p[1] < 1.35 and abs(p[2]) < 0.7]
        self.assertEqual(cabin, [], "body geometry inside the cabin")
        hubs = [p for g, *abc in self.tris for p in abc for axle in (AXLE_X, -AXLE_X)
                if (p[0] - axle) ** 2 + (p[1] - 0.447) ** 2 < 0.18 ** 2 and 0.5 < abs(p[2]) < 0.99]
        self.assertEqual(hubs, [], "body geometry inside a wheel")


AXLE_X = 3.444 / 2


@unittest.skipIf(NODE is None, "node is not installed on this machine")
class PartsAgainstTheBody(unittest.TestCase):
    """What sits in the body is in it, and what sits on it is on it.

    Before the body was a measured model, a dashboard 0.18 m wide of the door
    glass, a battery in a wheel arch and a light bar 0.24 m above the lamps it
    belongs between all passed every test here, because the only thing they
    were checked against was other boxes. These check them against the drawn
    skin.
    """

    TOL = 0.02
    INSIDE_LAYERS = {"cabin", "radar"}
    INSIDE_IDS = {"etrunk", "lv", "lv-terminal-0", "lv-terminal-1", "fusebox-underhood",
                  "fusebox-ip-left", "fusebox-ip-right", "radiator", "condenser", "chiller",
                  "tpim-1", "tpim-2", "tpim-3", "hv-rear-module",
                  "headlamp-l", "headlamp-r", "shell-lightbar"}

    #: The windshield's lower edge on the centreline; below it the cabin ends at it.
    WIND_BASE = (1.165, 1.382)

    @classmethod
    def setUpClass(cls):
        cls.parts = {p["id"]: p for p in build_parts()}
        cls.x, cls.y, cls.z = skin()["x"], skin()["y"], skin()["z"]
        cls.glass_x = reference_skin.Extents([x for x in skin()["tris"] if x[0] == "glass"], 0)

    @staticmethod
    def _box(p):
        return [(p["t"][i] - abs(p["s"][i]) / 2, p["t"][i] + abs(p["s"][i]) / 2) for i in range(3)]

    def _outside(self, x, y, z, top_only=False):
        checks = [("top", self.y.near(x, z, "hi"), lambda s: y <= s + self.TOL)]
        if not top_only:
            checks += [("front", self.x.near(y, z, "hi"), lambda s: x <= s + self.TOL),
                       ("rear", self.x.near(y, z, "lo"), lambda s: x >= s - self.TOL),
                       ("left", self.z.near(x, y, "lo"), lambda s: z >= s - self.TOL),
                       ("right", self.z.near(x, y, "hi"), lambda s: z <= s + self.TOL)]
        return [name for name, s, ok in checks if s is None or not ok(s)]

    def test_what_is_inside_the_body_is_inside_it(self):
        out = []
        for pid, p in self.parts.items():
            inside = (p["layer"] in self.INSIDE_LAYERS or pid in self.INSIDE_IDS
                      or pid.startswith(("headlamp-l-seg-", "headlamp-r-seg-")))
            # The pack is the rescue sheet's, and its case's front corners
            # reach 5 cm past the body's sill toward the front wheelhouse.
            # The sheet wins that one; the pack is still held under the body.
            pack = pid == "pack-case" or pid.startswith("mod-")
            if not (inside or pack):
                continue
            (x0, x1), (y0, y1), (z0, z1) = self._box(p)
            cabin = p["layer"] in self.INSIDE_LAYERS
            for x in (x0, x1):
                for y in (y0, y1):
                    for z in (z0, z1):
                        for side in self._outside(x, y, z, top_only=pack):
                            out.append((pid, side, round(x, 3), round(y, 3), round(z, 3)))
                        # The body's outline at dash height is the hood, so a
                        # cabin part pushed through the windshield would still be
                        # "inside" it. The cabin ends at the glass.
                        if cabin:
                            glass = self.glass_x.near(y, z, "hi") if y >= self.WIND_BASE[1] else None
                            limit = glass if glass is not None else self.WIND_BASE[0]
                            if x > limit + self.TOL:
                                out.append((pid, "windshield", round(x, 3), round(y, 3), round(z, 3)))
        self.assertEqual(out, [], f"parts poking out of the body: {out[:12]}")

    #: part id, axis, which extreme of the skin the part's outward face is on
    ON_SKIN = ([("lightbar-letter-%d" % i, 0, "hi") for i in range(6)]
               + [("headlamp-%s-bar-%d" % (s, i), 0, "hi") for s in "lr" for i in range(8)]
               + [("taillamp-l", 0, "lo"), ("taillamp-r", 0, "lo")]
               + [("marker-f-%d" % i, 0, "hi") for i in range(3)]
               + [("marker-r-%d" % i, 0, "lo") for i in range(3)]
               + [("portlamp", 2, "lo")]
               + [("cam-front", 0, "hi"), ("cam-rear", 0, "lo"), ("cam-tailgate", 0, "lo"),
                  ("cam-under-f", 1, "lo"), ("cam-under-r", 1, "lo"),
                  ("cam-mirror-l", 1, "lo"), ("cam-mirror-r", 1, "lo")])

    def test_lamps_and_cameras_sit_on_the_skin(self):
        """On it: not buried anywhere across its face, and not standing off it.

        The skin is sampled exactly over the part's whole footprint, not at one
        point: its outward face must be at or proud of the skin's outermost
        point there, and its inner face must reach back to it.
        """
        tris = skin()["tris"]
        wrong = []
        for pid, axis, which in self.ON_SKIN:
            p = self.parts[pid]
            box = self._box(p)
            i, j = [a for a in range(3) if a != axis]
            extent = reference_skin.extreme(tris, axis, which, box[i], box[j])
            face = box[axis][1 if which == "hi" else 0]
            depth = abs(p["s"][axis])
            proud = None if extent is None else (face - extent if which == "hi" else extent - face)
            if proud is None or proud < -0.002 or proud - depth > 0.002:
                wrong.append((pid, None if proud is None else round(proud, 3)))
        self.assertEqual(wrong, [], f"not on the skin (proud, m): {wrong}")

    def test_the_charge_door_lies_on_its_panel(self):
        # The bedside tapers outward across the door, so the door is one plate
        # turned to follow it: outside the skin across its whole face, nowhere
        # more than 15 mm proud of it, and between the taillamp and the flare.
        door = self.parts["charge-door"]
        self.assertIsNotNone(door["yaw"], "the door is not turned to follow its panel")
        tris = [t for t in skin()["tris"]
                if min(q[0] for q in t[1:]) < -2.3 and min(q[2] for q in t[1:]) < -0.9]
        a, (length, height, thick) = door["yaw"], door["size"]
        ox = door["t"][0] - thick / 2 * math.sin(a)     # outer face centre
        oz = door["t"][2] - thick / 2 * math.cos(a)
        proud = []
        for k in range(9):
            u = -length / 2 + length * k / 8
            for m in range(7):
                x, y = ox + u * math.cos(a), door["t"][1] - height / 2 + height * m / 6
                s = reference_skin.extreme(tris, 2, "lo", (x - 5e-4, x + 5e-4), (y - 5e-4, y + 5e-4), samples=2)
                self.assertIsNotNone(s, f"no skin behind the door at x {x:.3f}, y {y:.3f}")
                proud.append(s - (oz - u * math.sin(a)))
        self.assertGreater(min(proud), 0, f"the door is buried {-min(proud) * 1000:.1f} mm")
        self.assertLess(max(proud), 0.015, f"the door stands {max(proud) * 1000:.1f} mm off its panel")
        (x0, x1), _, _ = self._box(door)
        self.assertGreater(x0, -2.72, "the door runs into the taillamp")
        self.assertLess(x1, -2.40, "the door runs onto the flare")

    def test_what_looks_into_the_bed_is_on_what_holds_it(self):
        # The bed's inside is not the body's outline, so these are measured
        # against the surfaces themselves: the bed lamps against the side walls'
        # inner faces, the bed camera against the cab's rear roof lip.
        tris = skin()["tris"]
        off = []
        for pid, p in self.parts.items():
            if not pid.startswith("bedlamp-"):
                continue
            sign = -1 if p["t"][2] < 0 else 1
            (x0, x1), (y0, y1), _ = self._box(p)
            wall = reference_skin.extreme(
                tris, 2, "lo" if sign < 0 else "hi", (x0, x1), (y0, y1),
                where=lambda a, b, c, s=sign: all(0.70 < s * q[2] < 0.80 for q in (a, b, c)))
            face = p["t"][2] + sign * abs(p["s"][2]) / 2
            gap = sign * (wall - face) if wall is not None else None
            if gap is None or not -0.002 <= gap <= 0.005:
                off.append((pid, gap))
        cam = self.parts["cam-bed"]
        (x0, x1), (y0, y1), (z0, z1) = self._box(cam)
        lip = reference_skin.extreme(
            tris, 0, "lo", (y0, y1), (z0, z1),
            where=lambda a, b, c: min(q[0] for q in (a, b, c)) > -1.5 and min(q[1] for q in (a, b, c)) > 1.8)
        proud = None if lip is None else lip - x0
        if proud is None or proud < -0.002 or proud - (x1 - x0) > 0.002:
            off.append(("cam-bed", proud))
        self.assertEqual(off, [], f"not on what holds them: {off}")


@unittest.skipIf(NODE is None, "node is not installed on this machine")
class TonneauTests(unittest.TestCase):
    """The power tonneau cover, LPO 5KM, drawn because the owner's truck has one.

    A dealer accessory that nothing the truck reports can confirm, so it is its
    own layer. Its sizes are GM's: 60.16 x 63.07 in, 9.23 mm, a 7.5 in canister.
    """

    @classmethod
    def setUpClass(cls):
        cls.parts = {p["id"]: p for p in build_parts()}

    @staticmethod
    def _box(p):
        return [(p["t"][i] - abs(p["s"][i]) / 2, p["t"][i] + abs(p["s"][i]) / 2) for i in range(3)]

    def test_it_is_its_own_layer_and_starts_drawn(self):
        layers = re.search(r"layers = \{([^}]*)\}", _script()).group(1)
        self.assertRegex(layers, r"tonneau:\s*1\b")
        self.assertIn('["tonneau", "Tonneau"]', _script())
        self.assertGreaterEqual(len([p for p in self.parts.values() if p["layer"] == "tonneau"]), 5)

    def test_the_cover_is_gms_size_and_spans_the_bed(self):
        main, front = self._box(self.parts["tonneau-cover"]), self._box(self.parts["tonneau-cover-front"])
        self.assertAlmostEqual(front[0][1] - main[0][0], 60.16 * 0.0254, delta=0.01)
        self.assertAlmostEqual(main[2][1] - main[2][0], 63.07 * 0.0254, delta=0.005)
        self.assertAlmostEqual(main[1][1] - main[1][0], 0.00923, delta=0.001)
        self.assertAlmostEqual(main[0][0], -2.760, delta=0.005, msg="not at the tailgate's inner face")
        self.assertAlmostEqual(front[0][1], -1.234, delta=0.005, msg="not at the cab's back wall")

    def test_the_cover_lies_on_the_rail_caps_below_the_rear_window(self):
        top = skin()["y"]
        main = self._box(self.parts["tonneau-cover"])
        rails = [top.at(x / 100, z) for x in range(-270, -160, 10) for z in (-0.80, 0.80)]
        rails = [r for r in rails if r is not None]
        self.assertTrue(rails)
        self.assertLess(max(abs(r - main[1][0]) for r in rails), 0.01, "not on the rail caps")
        window = min(p[1] for p in _glass_behind_the_cab())
        for pid in ("tonneau-cover", "tonneau-cover-front"):
            self.assertLess(self._box(self.parts[pid])[1][1], window, f"{pid} is above the rear window's edge")

    def test_the_front_of_the_cover_fits_between_the_sail_panels(self):
        front = self._box(self.parts["tonneau-cover-front"])
        wall = reference_skin.extreme(
            skin()["tris"], 2, "hi", front[0], front[1],
            where=lambda a, b, c: all(-0.80 < q[2] < -0.70 for q in (a, b, c)))
        self.assertIsNotNone(wall, "no sail panel found beside the front of the bed")
        self.assertLessEqual(front[2][1], -wall + 1e-3, "the cover runs into the sail panels")

    def test_the_canister_is_at_the_front_of_the_bed_under_the_cover(self):
        can = self._box(self.parts["tonneau-canister"])
        cover = self._box(self.parts["tonneau-cover-front"])
        self.assertAlmostEqual(can[0][1], -1.234, delta=0.005, msg="not against the cab's back wall")
        self.assertAlmostEqual(can[0][1] - can[0][0], 7.5 * 0.0254, delta=0.005)
        self.assertLessEqual(can[1][1], cover[1][0] + 1e-6, "the canister stands through the cover")
        self.assertGreater(can[1][0], skin()["y"].at(-1.35, 0.0), "the canister is below the bed floor")
        release = self._box(self.parts["tonneau-release"])
        self.assertGreater(release[2][0], 0, "the release lever is not on the passenger side")

    def test_nothing_else_is_inside_the_tonneau(self):
        clash = []
        for pid, p in self.parts.items():
            if p["layer"] != "tonneau":
                continue
            a = self._box(p)
            for qid, q in self.parts.items():
                if q["layer"] in ("tonneau", "shell"):
                    continue
                b = self._box(q)
                if all(a[i][0] < b[i][1] - 1e-6 and b[i][0] < a[i][1] - 1e-6 for i in range(3)):
                    clash.append((pid, qid))
        self.assertEqual(clash, [], f"inside the tonneau: {clash}")
