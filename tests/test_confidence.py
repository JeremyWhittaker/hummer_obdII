"""The confidence table, and the claims it makes about the corpus.

`registry.py` exists because a hand-kept identifier list drifted thirty-six
commits behind the code. This is the same failure one layer up: how much each
identifier has been *proven* was recorded in prose, in three documents, none of
which anything checked.

Two kinds of test live here. The first are structural -- key parity with the
gate, levels in range, `answers_at` agreeing with the level. The second are
harder and more valuable: they **re-derive the level-3 claims from the committed
sessions**. A cross-validation asserted in a docstring is a story; one that a
test recomputes from the CSVs is a measurement that will tell us when it stops
being true.
"""

import glob
import os
import re
import unittest

from hummer_obd import drive
from hummer_obd.analyze import correlate, read_session, sane
from hummer_obd.confidence import (
    CONFIDENCE,
    LEVEL_NAMES,
    PRODUCTION_MINIMUM,
    at_least,
    unproven,
)
from hummer_obd.enhanced import PROFILES
from hummer_obd.safety import ENHANCED_READ_DIDS

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SESSIONS = sorted(glob.glob(os.path.join(_ROOT, "evidence", "sessions",
                                          "drive-*.csv")))


def _corpus():
    rows = []
    for path in _SESSIONS:
        found, _warnings, _header = read_session(path)
        rows.extend(r for r in found if sane(r))
    return rows


class TestParityWithTheGate(unittest.TestCase):
    """An identifier cannot exist in one table and not the other."""

    def test_the_key_sets_are_identical(self):
        self.assertEqual(set(CONFIDENCE), set(ENHANCED_READ_DIDS))

    def test_every_level_is_one_of_the_defined_levels(self):
        for did, ev in CONFIDENCE.items():
            with self.subTest(did=did):
                self.assertIn(ev.level, LEVEL_NAMES)

    def test_answers_at_agrees_with_the_level(self):
        # Level 0 *means* no module here answered it.  Any other level means one
        # did, and says which -- a level claimed without an address behind it is
        # the kind of assertion this table exists to stop.
        for did, ev in CONFIDENCE.items():
            with self.subTest(did=did):
                self.assertEqual(bool(ev.answers_at), ev.level >= 1)

    def test_every_entry_says_why(self):
        for did, ev in CONFIDENCE.items():
            with self.subTest(did=did):
                self.assertTrue(ev.basis.strip())
                self.assertTrue(ev.states)

    def test_a_cross_validated_claim_names_its_independent_route(self):
        # Level 3 means "something else agrees".  If the basis cannot say what
        # else, the level is an opinion.
        for did in at_least(PRODUCTION_MINIMUM):
            with self.subTest(did=did):
                basis = CONFIDENCE[did].basis.lower()
                self.assertTrue(
                    any(word in basis for word in
                        ("cross-validated", "corroborated", "agree", "against")),
                    f"0x{did} claims level {CONFIDENCE[did].level} without naming "
                    "the independent route that agrees")


class TestTheTableAgreesWithTheRecorder(unittest.TestCase):
    def test_everything_that_answers_here_is_captured(self):
        """The check that caught four proven identifiers being dropped.

        Stated the other way round from `test_drive.py`'s version: there, every
        proven identifier must be in a recorder group. Here, every identifier
        the confidence table says answers must have a decoder -- so the table
        cannot record a positive result for something nothing stores.
        """
        for did, ev in CONFIDENCE.items():
            if ev.level >= 1 and not did.startswith("F1"):
                with self.subTest(did=did):
                    self.assertIn(did, drive.DECODERS)

    def test_nothing_unproven_is_being_recorded_as_if_it_were(self):
        for did in unproven():
            with self.subTest(did=did):
                self.assertNotIn(did, drive.DECODERS)

    def test_every_allowlisted_identifier_is_reachable_from_something(self):
        """Allowlisted and unreachable is approval nobody can act on.

        `0x2429` was in the gate from 2026-09-03 and in no profile at all, so
        nothing could ever transmit it. Building the confidence table is what
        found that, and this is what stops it recurring.
        """
        sendable = {req[0][2:6] for profile in PROFILES.values()
                    for req in profile.requests if req[0].startswith("22")}
        recorded = set(drive.DECODERS)
        for did in CONFIDENCE:
            with self.subTest(did=did):
                self.assertTrue(
                    did in sendable or did in recorded,
                    f"0x{did} passes the gate but no profile and no recorder "
                    "group ever sends it")


@unittest.skipUnless(_SESSIONS, "no committed sessions to re-derive against")
class TestTheLevelThreeClaimsAreRederived(unittest.TestCase):
    """Recompute the cross-validations rather than trusting the prose.

    These are slow-ish and read the whole committed corpus on purpose. A claim
    that a test cannot recompute is a claim nothing will notice going stale.
    """

    @classmethod
    def setUpClass(cls):
        cls.rows = _corpus()

    def test_wheel_speeds_still_track_the_legislated_speed(self):
        # 0x4A7A is a vendor scaling from an unmerged BEV3 source, confirmed by
        # PID 010D -- the standard's own measurement, from a different module,
        # in the same row.
        corners = ("wheel_fl_kph", "wheel_fr_kph", "wheel_rl_kph", "wheel_rr_kph")
        moving = [r for r in self.rows
                  if isinstance(r.get("speed_kph"), (int, float))
                  and r["speed_kph"] > 0
                  and all(isinstance(r.get(c), (int, float)) for c in corners)]
        self.assertGreater(len(moving), 200, "not enough moving samples")
        speed = [r["speed_kph"] for r in moving]
        self.assertGreater(max(speed) - min(speed), 50,
                           "a correlation across a narrow speed span says little")
        for corner in corners:
            with self.subTest(corner=corner):
                r = correlate(speed, [row[corner] for row in moving])
                self.assertIsNotNone(r)
                self.assertGreater(r, 0.99, f"{corner} no longer tracks 010D")
                mean_diff = (sum(row[corner] - row["speed_kph"] for row in moving)
                             / len(moving))
                self.assertLess(abs(mean_diff), 1.0,
                                f"{corner} has drifted from 010D by {mean_diff} kph")

    def test_longitudinal_acceleration_still_matches_the_speed_derivative(self):
        # Weaker by construction than the wheel-speed check and asserted as
        # such: the two quantities are read seconds apart, and a derivative over
        # an eight-second cycle is a smoothed version of an accelerometer.  The
        # magnitudes agreeing is the part that carries the claim.
        xs, ys = [], []
        for path in _SESSIONS:
            found, _w, _h = read_session(path)
            rows = [r for r in found if sane(r)]
            for a, b in zip(rows, rows[1:]):
                if not all(isinstance(r.get(k), (int, float))
                           for r in (a, b) for k in ("speed_kph", "elapsed_s")):
                    continue
                if not isinstance(b.get("longitudinal_g"), (int, float)):
                    continue
                dt = b["elapsed_s"] - a["elapsed_s"]
                if not 3.0 < dt < 20.0:
                    continue
                xs.append(((b["speed_kph"] - a["speed_kph"]) / 3.6) / dt)
                ys.append(b["longitudinal_g"] * 9.80665)
        self.assertGreater(len(xs), 500)
        r = correlate(xs, ys)
        self.assertIsNotNone(r)
        self.assertGreater(r, 0.7, "0x4C30 no longer follows the speed derivative")
        # Same physical units, so the ranges have to be comparable -- a scaling
        # error of any size would show up here even where the correlation held.
        span_derived = max(xs) - min(xs)
        span_field = max(ys) - min(ys)
        self.assertLess(abs(span_field - span_derived) / span_derived, 0.5,
                        f"magnitudes disagree: {span_derived:.2f} vs "
                        f"{span_field:.2f} m/s^2")


class TestTheCatalogAgreesWithTheLevels(unittest.TestCase):
    """The gap the audit found, closed the way `registry.py` closes its own.

    `docs/TELEMETRY_CATALOG.md` grades every signal `measured` / `read` / `raw`
    and states the mapping to these numeric levels in its own header. Nothing
    checked that the two agreed, and within hours of the levels existing three
    signals disagreed: `0x4A7A`, `0x4C30` and `0x33E5` were promoted to level 3
    in code while the catalog still said `read`.

    That is the fourth hand-kept inventory in this project to drift. This is the
    test that makes it the last one for this pair.

    Deliberately *not* a generated table. The catalog is prose written for a
    human -- scalings, units, the reasoning behind each decode -- and generating
    it would cost more than it saves. Checking one column of it costs nothing.
    """

    #: The mapping the catalog states in its own "Evidence levels" section.
    WORD_FOR_LEVEL = {1: "raw", 2: "read", 3: "measured", 4: "measured"}
    RANK = {"raw": 1, "read": 2, "measured": 3}

    CATALOG = os.path.join(_ROOT, "docs", "TELEMETRY_CATALOG.md")

    #: A table row whose last cell is exactly a bolded level word.  Rows whose
    #: last cell is prose -- the refusals table, the standard-OBD notes -- carry
    #: no grade to check and are skipped rather than guessed at.
    ROW = re.compile(r"^\|.*?\|\s*\*\*(measured|read|raw)\*\*\s*\|\s*$")
    IDENT = re.compile(r"`0x([0-9A-F]{4})`")

    def catalog_grades(self):
        """Identifier -> the strongest grade the catalog gives it.

        An identifier legitimately appears more than once with different grades:
        `0x2AF5` is **measured** for the three cell voltages it carries and
        **raw** for the four trailing bytes nobody has decoded. The strongest
        grade is the claim about the identifier; the weaker rows are claims
        about parts of its payload.
        """
        grades: dict[str, str] = {}
        with open(self.CATALOG, encoding="utf-8") as handle:
            for line in handle:
                match = self.ROW.match(line.rstrip("\n"))
                if not match:
                    continue
                word = match.group(1)
                for did in self.IDENT.findall(line):
                    # `grades.get(did, "raw")` looks equivalent and is not: it
                    # gives an unseen identifier rank 1, so "raw" never beats
                    # its own default and every raw-only row is dropped.  The
                    # vacuity guard below is what caught that.
                    current = grades.get(did)
                    if current is None or self.RANK[word] > self.RANK[current]:
                        grades[did] = word
        return grades

    def test_the_catalog_grades_something(self):
        # A regex that silently matches nothing would make every assertion below
        # pass vacuously, which is the failure mode of every table-scraping test.
        grades = self.catalog_grades()
        self.assertGreater(len(grades), 25, "the row pattern stopped matching")
        self.assertIn("2885", grades)

    def test_every_graded_identifier_matches_its_confidence_level(self):
        for did, word in sorted(self.catalog_grades().items()):
            if did not in CONFIDENCE:
                continue
            with self.subTest(did=did):
                self.assertEqual(
                    word, self.WORD_FOR_LEVEL[CONFIDENCE[did].level],
                    f"docs/TELEMETRY_CATALOG.md grades 0x{did} **{word}** while "
                    f"confidence.py has it at level {CONFIDENCE[did].level}",
                )

    def test_everything_that_answers_here_is_graded_somewhere_in_the_catalog(self):
        """The other direction: a signal the vehicle answers must be written up.

        Only the four ISO identifiers are legitimately absent, and they are the
        one case where absence is correct -- they are reachability probes, not
        signals, and no module has ever returned a positive response to one.
        Anything else missing means the vehicle answered something and the
        catalog never said what it was worth.
        """
        graded = set(self.catalog_grades())
        ungraded = sorted(d for d, e in CONFIDENCE.items()
                          if e.level >= 1 and d not in graded)
        self.assertEqual(ungraded, [],
                         f"identifiers this vehicle answers with no catalog row: {ungraded}")

    def test_nothing_the_catalog_grades_is_missing_from_the_gate(self):
        unknown = sorted(d for d in self.catalog_grades() if d not in CONFIDENCE)
        self.assertEqual(unknown, [],
                         f"the catalog grades identifiers the gate does not hold: {unknown}")


class TestWhatTheLevelsAreFor(unittest.TestCase):
    def test_production_telemetry_starts_at_three(self):
        self.assertEqual(PRODUCTION_MINIMUM, 3)
        for did in at_least(PRODUCTION_MINIMUM):
            self.assertGreaterEqual(CONFIDENCE[did].level, 3)

    def test_the_identifier_this_vehicle_contradicted_stays_at_one(self):
        # 0x5401 is published as two-byte charger DC power / 4350.  It answers
        # with one byte and reads non-zero at idle.  A source disagreeing with
        # the vehicle is exactly when a level must not creep upward.
        self.assertEqual(CONFIDENCE["5401"].level, 1)

    def test_the_iso_identifiers_are_level_zero_and_that_is_correct(self):
        # They have never returned a positive response and never will here.
        # A formed 7F 22 31 is what they are sent for.
        for did in ("F187", "F188", "F189", "F191"):
            with self.subTest(did=did):
                self.assertEqual(CONFIDENCE[did].level, 0)
                self.assertEqual(CONFIDENCE[did].answers_at, ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
