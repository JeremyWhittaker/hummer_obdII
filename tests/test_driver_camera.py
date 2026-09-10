"""The driver camera's roll has to survive the view matrix that consumes it.

This is a maths bug that reads as correct code. The camera was handed an
up-vector of [sin(lean), cos(lean), 0] with a comment explaining that it tips
the horizon. It does not: the driver camera looks along +X, so that tilt lies
almost entirely ALONG the view direction, and m4look orthogonalises the up
against the view before building its basis -- removing exactly the component
that carried the roll. The angle was computed, passed in, and cancelled.

Nothing observable failed. The horizon simply never moved, which looks the same
as a vehicle that is not leaning.
"""

import math
import re
import unittest
from importlib.resources import files

PAGE = files("hummer_obd").joinpath("dashboard.html").read_text(encoding="utf-8")


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]]


def _norm(v):
    n = math.hypot(*v) or 1.0
    return [x / n for x in v]


def rolled_up(forward, lean):
    """The construction the page now uses, reproduced."""
    fwd = _norm(forward)
    right = _norm([-fwd[2], 0.0, fwd[0]])          # fwd x worldUp
    true_up = _cross(right, fwd)
    cl, sl = math.cos(lean), math.sin(lean)
    return [true_up[i] * cl + right[i] * sl for i in range(3)]


class RollSurvivesTests(unittest.TestCase):
    FORWARD = (1.0, 0.0, 0.02)     # the driver camera, dyaw 0.02

    def test_the_up_vector_stays_perpendicular_to_the_view(self):
        """Which is the whole fix. A component along the view is discarded."""
        for lean in (0.0, 0.05, 0.14, -0.14):
            with self.subTest(lean=lean):
                up = rolled_up(self.FORWARD, lean)
                dot = sum(up[i] * _norm(self.FORWARD)[i] for i in range(3))
                self.assertAlmostEqual(dot, 0.0, places=12)

    def test_the_old_construction_would_have_been_cancelled(self):
        # [sin, cos, 0] against a forward of +X: the dot product is the part
        # m4look throws away, and at 0.14 rad it is the entire roll.
        fwd = _norm(self.FORWARD)
        up = [math.sin(0.14), math.cos(0.14), 0.0]
        self.assertGreater(abs(sum(up[i] * fwd[i] for i in range(3))), 0.1)

    def test_the_up_vector_actually_moves_with_lean(self):
        flat = rolled_up(self.FORWARD, 0.0)
        tilted = rolled_up(self.FORWARD, 0.14)
        moved = max(abs(flat[i] - tilted[i]) for i in range(3))
        self.assertGreater(moved, 0.05, "a lean that does not move the horizon")

    def test_the_roll_reverses_with_the_sign_of_the_lean(self):
        left = rolled_up(self.FORWARD, 0.14)
        right = rolled_up(self.FORWARD, -0.14)
        self.assertLess(left[2] * right[2], 0, "both leans tip the same way")

    def test_the_right_vector_is_not_negated(self):
        # The first version inlined the zeros of worldUp and reversed the
        # operands, giving [f2, 0, -f0] -- the negation -- so the horizon
        # would have tipped the wrong way.
        fwd = _norm((1.0, 0.0, 0.0))
        self.assertEqual(_norm([-fwd[2], 0.0, fwd[0]]),
                         _norm(_cross(fwd, [0.0, 1.0, 0.0])))


class SourceTests(unittest.TestCase):
    def test_the_page_no_longer_passes_a_world_plane_tilt(self):
        self.assertNotIn("[Math.sin(lean), Math.cos(lean), 0]", PAGE)

    def test_the_page_builds_the_up_vector_from_the_forward_vector(self):
        for token in ("var fwd =", "trueUp", "right[0] * sl"):
            with self.subTest(token=token):
                self.assertIn(token, PAGE)

    def test_the_model_does_not_breathe_from_the_drivers_seat(self):
        # The charge breath scales the whole model about the world origin,
        # which from a fixed eye slides the dashboard in and out -- the exact
        # motion the driver camera was rebuilt to stop.
        self.assertRegex(
            PAGE, r'if \(live\.charging && cam\.mode !== "driver"\)')

    def test_everything_outside_the_cabin_is_faded_in_the_driver_view(self):
        # Fading only shell and wheels left the pack, drive units, thermal
        # plumbing and eTrunk at full opacity, visible through a firewall that
        # had itself been faded.
        match = re.search(r'cam\.mode === "driver" && ([^)]*)\)', PAGE)
        self.assertIsNotNone(match, "no driver-view layer rule found")
        self.assertIn('p.layer !== "cabin"', match.group(1))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
