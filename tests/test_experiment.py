"""Labelled observations, and the field that decides whether one is worth having.

Every number in this project comes from the vehicle, which is a problem for the
fields it cannot decode: correlating the truck's numbers against the truck's
other numbers can only ever show that two of its outputs move together. An
outside measurement -- a thermometer, a charger display, the dashboard -- breaks
that circle.

`label_source` is the load-bearing field. A label *derived from the session CSV*
does not break the circle, and recording it as though it did would launder an
inference into evidence. Both kinds are worth keeping; only one is worth
correlating against, and a reader has to be able to tell which they hold.
"""

import json
import tempfile
import unittest
from pathlib import Path

from hummer_obd import experiment as ex
from hummer_obd.experiment import (
    CHARGE_STATES,
    VEHICLE_STATES,
    Experiment,
    load,
    load_all,
    sidecar_path,
    unlabelled_sessions,
)


def make(**kw):
    base = dict(session="drive-X.csv", vehicle_state="parked-awake",
                charge_state="unplugged", label_source="observed-at-vehicle")
    base.update(kw)
    return Experiment(**base)


class TestAnInferredLabelCannotLaunderItselfIntoEvidence(unittest.TestCase):
    def test_an_inferred_label_may_not_carry_an_outside_measurement(self):
        """The rule the whole schema exists for.

        "It was 72 degrees" is an observation. "charge_state was charging
        because pack current was negative" is a restatement of the data. Letting
        the second carry the first would make a derived label look like ground
        truth, and the correlation built on it would be circular without saying so.
        """
        bad = make(label_source="inferred-from-telemetry", ambient_f=72.0)
        self.assertFalse(bad.valid)
        self.assertIn("not an observation", " ".join(bad.problems()))

    def test_an_inferred_label_with_no_outside_measurement_is_fine(self):
        # Still worth recording: it says what the session was without pretending
        # to be independent of it.
        self.assertTrue(make(label_source="inferred-from-telemetry").valid)

    def test_an_observed_label_may_carry_outside_measurements(self):
        self.assertTrue(make(ambient_f=41.0, dashboard_soc_pct=68.0).valid)

    def test_label_source_has_no_default(self):
        # A default would be answered by whichever value someone picked, and the
        # distinction would quietly stop being made.
        with self.assertRaises(TypeError):
            Experiment(session="s", vehicle_state="parked-awake",
                       charge_state="unplugged")


class TestValidation(unittest.TestCase):
    def test_the_state_vocabularies_are_closed(self):
        for kw in ({"vehicle_state": "parked"}, {"charge_state": "charging"},
                   {"label_source": "guessed"}):
            with self.subTest(**kw):
                self.assertFalse(make(**kw).valid)

    def test_parked_and_parked_awake_are_different_states(self):
        # Free text would let these blur, and they say different things about
        # what a reading is worth.
        self.assertIn("parked-awake", VEHICLE_STATES)
        self.assertIn("asleep", VEHICLE_STATES)
        self.assertNotIn("parked", VEHICLE_STATES)

    def test_plugged_but_idle_is_its_own_state(self):
        # An EVSE-current field read plugged-in-but-idle is a different
        # observation from one read unplugged, and from one read mid-charge.
        self.assertIn("plugged-idle", CHARGE_STATES)

    def test_a_charge_rate_without_a_charge_is_refused(self):
        bad = make(charge_state="unplugged", evse_kw=7.4)
        self.assertFalse(bad.valid)
        self.assertIn("EVSE rate", " ".join(bad.problems()))

    def test_impossible_outside_values_are_refused(self):
        for kw in ({"ambient_f": 300.0}, {"dashboard_soc_pct": 140.0},
                   {"evse_amps": -3.0}):
            with self.subTest(**kw):
                self.assertFalse(make(**kw).valid)

    def test_problems_are_returned_not_raised(self):
        # One bad field should not discard the good ones.
        bad = make(vehicle_state="nonsense", ambient_f=41.0)
        self.assertIsInstance(bad.problems(), list)
        self.assertEqual(bad.ambient_f, 41.0)


class TestSidecarsOnDisk(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.exp = self.dir / "experiments"
        self.sess = self.dir / "sessions"
        self.exp.mkdir()
        self.sess.mkdir()

    def write(self, name, data):
        path = self.exp / name
        path.write_text(json.dumps(data))
        return path

    def test_a_sidecar_is_named_for_its_session(self):
        self.assertTrue(sidecar_path("evidence/sessions/drive-A.csv")
                        .endswith("drive-A.json"))

    def test_a_typo_in_a_field_name_is_an_error_not_a_shrug(self):
        """Silently dropping it is the worst outcome for a hand-written record:
        the observation was made, written down, and thrown away."""
        p = self.write("a.json", {"session": "a.csv", "vehicle_state": "parked-awake",
                                  "charge_state": "unplugged",
                                  "label_source": "observed-at-vehicle",
                                  "ambiant_f": 41.0})
        with self.assertRaises(ValueError) as caught:
            load(str(p))
        self.assertIn("ambiant_f", str(caught.exception))

    def test_a_missing_required_field_names_itself(self):
        p = self.write("b.json", {"session": "b.csv", "vehicle_state": "parked-awake"})
        with self.assertRaises(ValueError) as caught:
            load(str(p))
        self.assertIn("charge_state", str(caught.exception))
        self.assertIn("label_source", str(caught.exception))

    def test_a_bad_sidecar_is_reported_not_hidden(self):
        self.write("good.json", {"session": "good.csv", "vehicle_state": "driving",
                                 "charge_state": "unplugged",
                                 "label_source": "observed-at-vehicle"})
        self.write("broken.json", {"nope": 1})
        loaded = load_all(str(self.exp))
        self.assertIn("good", loaded["experiments"])
        self.assertIn("broken.json", loaded["problems"])

    def test_unlabelled_sessions_are_listed(self):
        (self.sess / "drive-1.csv").write_text("utc\n")
        (self.sess / "drive-2.csv").write_text("utc\n")
        self.write("drive-1.json", {"session": "drive-1.csv",
                                    "vehicle_state": "driving",
                                    "charge_state": "unplugged",
                                    "label_source": "observed-at-vehicle"})
        self.assertEqual(unlabelled_sessions(str(self.sess), str(self.exp)),
                         ["drive-2.csv"])


class TestCli(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_recording_writes_a_sidecar(self):
        rc = ex.main(["record", "evidence/sessions/drive-Z.csv",
                      "--vehicle-state", "driving", "--charge-state", "unplugged",
                      "--label-source", "observed-at-vehicle",
                      "--ambient-f", "41", "--dir", str(self.dir)])
        self.assertEqual(rc, 0)
        data = json.loads((self.dir / "drive-Z.json").read_text())
        self.assertEqual(data["ambient_f"], 41.0)
        self.assertEqual(data["label_source"], "observed-at-vehicle")

    def test_an_invalid_record_is_refused_and_writes_nothing(self):
        rc = ex.main(["record", "drive-Y.csv", "--vehicle-state", "parked-awake",
                      "--charge-state", "unplugged",
                      "--label-source", "inferred-from-telemetry",
                      "--ambient-f", "41", "--dir", str(self.dir)])
        self.assertEqual(rc, 2)
        self.assertFalse((self.dir / "drive-Y.json").exists())

    def test_check_exits_nonzero_when_a_sidecar_is_bad(self):
        (self.dir / "bad.json").write_text('{"session":"x","vehicle_state":"nope",'
                                           '"charge_state":"unplugged",'
                                           '"label_source":"observed-at-vehicle"}')
        self.assertEqual(ex.main(["check", "--dir", str(self.dir),
                                  "--sessions", str(self.dir)]), 1)


class TestItNeverTouchesTheVehicle(unittest.TestCase):
    def test_it_alters_no_session_csv(self):
        source = open(ex.__file__, encoding="utf-8").read()
        self.assertNotIn("drive-*.csv\", \"w\"", source)
        for forbidden in ("import serial", "SerialTransport", "rfcomm"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_the_sidecar_directory_is_not_the_session_directory(self):
        # The CSV is what the vehicle said and must stay exactly that.
        self.assertNotEqual(ex.DEFAULT_SESSION_DIR, ex.DEFAULT_EXPERIMENT_DIR)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
