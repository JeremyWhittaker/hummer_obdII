"""Which field responded to an intervention -- and the two metrics that decide.

Both were wrong on this tool's first run against real data, and both were caught
because the validation case had a known answer: a drive, where `speed_kph`,
the four wheel speeds and `brake_kpa` obviously must respond.

**The numeric metric divided by the larger of the two spreads.** A field that sat
at a constant zero and then started swinging has a huge spread afterwards, so
dividing by it suppressed exactly the response that matters most -- `speed_kph`
did not appear in a report about a drive.

**The text metric asked "are there new values".** Useless for a payload like
`array_2b43`, which carries 779 distinct values across the corpus: any two
windows are nearly disjoint, so every raw column claimed a response and buried
the real ones.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from hummer_obd import respond
from hummer_obd.respond import MIN_SAMPLES, format_report, responses, segment


def rows_at(start, count, step_s=8, **columns):
    """`count` rows starting at `start`, each column a constant or a callable."""
    out = []
    for i in range(count):
        row = {"utc": (start + timedelta(seconds=i * step_s)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")}
        for name, value in columns.items():
            row[name] = value(i) if callable(value) else value
        out.append(row)
    return out


T0 = datetime(2026, 9, 4, 1, 0, 0, tzinfo=timezone.utc)


def marks(*pairs):
    return [{"utc": (T0 + timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
             "label": label} for s, label in pairs]


class TestTheNumericMetric(unittest.TestCase):
    def test_a_field_that_was_flat_and_started_moving_is_found(self):
        """The bug that hid speed_kph from a report about a drive."""
        rows = (rows_at(T0, 20, speed_kph=0.0)
                + rows_at(T0 + timedelta(seconds=200), 20,
                          speed_kph=lambda i: float(i * 3)))
        result = responses(segment(rows, marks((200, "driving"))), ["speed_kph"])
        change = result[-1]["changes"][0]
        self.assertEqual(change["column"], "speed_kph")
        self.assertGreater(change["effect"], 2.0,
                           "a flat-to-varying field must rank as a response")

    def test_a_pure_variance_change_counts_even_with_no_mean_shift(self):
        # Same mean, wildly different spread: a real response, and one a
        # difference-of-means test alone would miss entirely.
        rows = (rows_at(T0, 20, x=10.0)
                + rows_at(T0 + timedelta(seconds=200), 20,
                          x=lambda i: 10.0 + (5.0 if i % 2 else -5.0)))
        change = responses(segment(rows, marks((200, "after"))), ["x"])[-1]["changes"][0]
        self.assertAlmostEqual(change["delta"], 0.0, places=6)
        self.assertGreater(change["effect"], 1.0)
        self.assertGreater(change["spread_ratio"], 10)

    def test_a_steady_field_that_did_not_move_ranks_below_one(self):
        rows = (rows_at(T0, 20, x=lambda i: 10.0 + (i % 3) * 0.1)
                + rows_at(T0 + timedelta(seconds=200), 20,
                          x=lambda i: 10.0 + (i % 3) * 0.1))
        change = responses(segment(rows, marks((200, "after"))), ["x"])[-1]["changes"][0]
        self.assertLess(change["effect"], 1.0)


class TestTheTextMetric(unittest.TestCase):
    def test_a_churning_hex_payload_is_not_reported_as_a_response(self):
        """The bug that buried the real answers under every raw column.

        A field with a fresh value in almost every row is disjoint between any
        two windows. That is drift, not a response to anything.
        """
        rows = (rows_at(T0, 20, raw=lambda i: f"{i:04X}")
                + rows_at(T0 + timedelta(seconds=200), 20,
                          raw=lambda i: f"{i + 500:04X}"))
        changes = responses(segment(rows, marks((200, "after"))), ["raw"])[-1]["changes"]
        effects = [c["effect"] for c in changes if c["column"] == "raw"]
        self.assertTrue(all(e < 1.0 for e in effects),
                        f"churning payload should not rank as a response: {effects}")

    def test_a_stable_hex_field_that_switched_value_is_a_strong_response(self):
        rows = (rows_at(T0, 20, raw="00A1")
                + rows_at(T0 + timedelta(seconds=200), 20, raw="00FF"))
        change = responses(segment(rows, marks((200, "after"))), ["raw"])[-1]["changes"][0]
        self.assertEqual(change["column"], "raw")
        self.assertEqual(change["overlap"], 0.0)
        self.assertEqual(change["stability"], 0.95)
        self.assertGreater(change["effect"], 3.0)

    def test_a_stable_hex_field_that_did_not_change_is_not_reported(self):
        rows = (rows_at(T0, 20, raw="00A1")
                + rows_at(T0 + timedelta(seconds=200), 20, raw="00A1"))
        changes = responses(segment(rows, marks((200, "after"))), ["raw"])[-1]["changes"]
        self.assertEqual([c for c in changes if c["column"] == "raw"], [])


class TestSegmentation(unittest.TestCase):
    def test_rows_before_the_first_mark_are_kept_as_the_baseline(self):
        rows = rows_at(T0, 12, x=1.0) + rows_at(T0 + timedelta(seconds=200), 12, x=2.0)
        segs = segment(rows, marks((200, "event")))
        self.assertEqual(len(segs), 2)
        self.assertIn("before the first mark", segs[0]["label"])
        self.assertEqual(len(segs[0]["rows"]), 12)

    def test_a_mark_with_no_rows_after_it_does_not_create_an_empty_segment(self):
        rows = rows_at(T0, 12, x=1.0)
        self.assertEqual(len(segment(rows, marks((5000, "much later")))), 1)

    def test_a_malformed_timestamp_is_skipped_not_fatal(self):
        rows = rows_at(T0, 12, x=1.0) + [{"utc": "not a time", "x": 9.0}]
        self.assertEqual(sum(len(s["rows"]) for s in segment(rows, [])), 12)


class TestItRefusesToOverclaim(unittest.TestCase):
    def test_a_thin_segment_yields_no_response_at_all(self):
        """With five samples a difference is a coincidence with a decimal point."""
        rows = (rows_at(T0, MIN_SAMPLES - 1, x=0.0)
                + rows_at(T0 + timedelta(seconds=200), 40, x=100.0))
        self.assertEqual(responses(segment(rows, marks((200, "after"))), ["x"])[-1]["changes"], [])

    def test_bookkeeping_columns_are_never_reported(self):
        rows = (rows_at(T0, 20, elapsed_s=lambda i: float(i))
                + rows_at(T0 + timedelta(seconds=200), 20,
                          elapsed_s=lambda i: float(i + 500)))
        changes = responses(segment(rows, marks((200, "after"))),
                            ["utc", "elapsed_s"])[-1]["changes"]
        self.assertEqual(changes, [], "elapsed_s always differs and means nothing")

    def test_the_report_says_association_is_not_identification(self):
        rows = (rows_at(T0, 20, x=0.0) + rows_at(T0 + timedelta(seconds=200), 20, x=9.0))
        text = format_report(responses(segment(rows, marks((200, "hvac on"))), ["x"]))
        self.assertIn("association in time and nothing more", text)
        self.assertIn("did NOT", text)

    def test_nothing_moving_is_reported_as_a_result(self):
        rows = (rows_at(T0, 20, x=1.0) + rows_at(T0 + timedelta(seconds=200), 20, x=1.0))
        text = format_report(responses(segment(rows, marks((200, "after"))), ["x"]))
        self.assertIn("That is a result", text)

    def test_since_drops_marks_and_rows_before_the_window(self):
        """Every stray mark is a segment boundary.

        Three setup marks written while testing the tooling would otherwise
        slice a real experiment into noise, and the fix is not to edit an
        append-only file -- it is to say where the experiment started.
        """
        with tempfile.TemporaryDirectory() as tmp:
            sessions = os.path.join(tmp, "s")
            os.makedirs(sessions)
            with open(os.path.join(sessions, "drive-a.csv"), "w") as fh:
                fh.write("utc,x\n")
                for r in rows_at(T0, 40, step_s=8, x=lambda i: float(i)):
                    fh.write(f"{r['utc']},{r['x']}\n")
            marks_path = os.path.join(tmp, "m.jsonl")
            with open(marks_path, "w") as fh:
                for entry in marks((10, "stray setup mark"), (160, "real start")):
                    fh.write(json.dumps(entry) + "\n")
            cutoff = (T0 + timedelta(seconds=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
            rc = respond.main(["--dir", sessions, "--marks", marks_path,
                               "--since", cutoff, "--quiet"])
            self.assertEqual(rc, 0)

    def test_a_malformed_since_is_refused_with_the_expected_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            marks_path = os.path.join(tmp, "m.jsonl")
            with open(marks_path, "w") as fh:
                fh.write(json.dumps(marks((0, "x"))[0]) + "\n")
            self.assertEqual(respond.main(["--dir", tmp, "--marks", marks_path,
                                           "--since", "last tuesday"]), 2)

    def test_no_marks_is_an_error_with_the_command_that_fixes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                respond.main(["--dir", tmp, "--marks", os.path.join(tmp, "none.jsonl")]), 2)
        self.assertIn("hummer-obd-experiment mark", format_report([]))


class TestItNeverTouchesTheVehicle(unittest.TestCase):
    def test_it_opens_no_serial_device(self):
        source = open(respond.__file__, encoding="utf-8").read()
        for forbidden in ("import serial", "SerialTransport", "rfcomm", "MonitorTransport"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
