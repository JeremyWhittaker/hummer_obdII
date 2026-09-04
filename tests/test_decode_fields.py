"""Correlating undecoded fields against what the vehicle reports.

This tool exists because the project's own published correlations could not be
re-derived from the repository. The first thing it did when run was disprove one
of them, which is the behaviour to protect.
"""

import os
import tempfile
import unittest

from hummer_obd import decode_fields
from hummer_obd.analyze import array_values, correlate, field_windows
from hummer_obd.decode_fields import collect, format_findings, rank, sane


def _rows(samples):
    """Rows from ``(hex, extra)`` pairs, with a sane pack voltage by default."""
    out = []
    for index, (text, extra) in enumerate(samples):
        row = {"utc": f"2026-09-03T00:00:{index % 60:02d}Z",
               "elapsed_s": float(index), "array_2b43": text, "pack_v": 380.0}
        row.update(extra)
        out.append(row)
    return out


class TestByteHelpers(unittest.TestCase):
    def test_a_hex_column_becomes_numbers(self):
        self.assertEqual(array_values("C3C4"), [195, 196])

    def test_anything_that_is_not_hex_is_none_rather_than_an_error(self):
        for text in (None, "", "   ", "not hex", 42, "C3C"):
            with self.subTest(text=text):
                self.assertIsNone(array_values(text))

    def test_windows_cover_the_widths_this_vehicle_actually_uses(self):
        # 0x27C6 is a u16, 0x27C7 a u24, 0x2414 a signed 16 -- so an array of
        # unknown encoding has to be tried at all three.
        windows = field_windows([0x01, 0x02, 0x03])
        self.assertEqual(windows["b00"], [1])
        self.assertEqual(windows["u16@00"], [0x0102])
        self.assertEqual(windows["u24@00"], [0x010203])

    def test_signed_windows_are_actually_signed(self):
        self.assertEqual(field_windows([0xFE, 0x39])["s16@00"], [-455])
        self.assertEqual(field_windows([0x00, 0x12])["s16@00"], [18])

    def test_correlation_declines_where_it_is_undefined(self):
        # Each of these is a finding the report must state, not a traceback.
        self.assertIsNone(correlate([1, 1, 1, 1], [1, 2, 3, 4]))  # constant field
        self.assertIsNone(correlate([1, 2], [1, 2]))              # too few
        self.assertIsNone(correlate([1, 2, 3], [1, 2]))           # mismatched
        self.assertAlmostEqual(correlate([1, 2, 3, 4], [2, 4, 6, 8]), 1.0)


class TestTransitionalRowsAreDropped(unittest.TestCase):
    """Correlating through a wake or sleep edge invents relationships."""

    def test_an_impossible_pack_voltage_is_not_a_measurement(self):
        # Observed live: a pack_v of 1.0 V as the vehicle woke.
        self.assertFalse(sane({"pack_v": 1.0}))
        self.assertTrue(sane({"pack_v": 380.0}))

    def test_an_impossible_temperature_is_dropped(self):
        self.assertFalse(sane({"temp_f": 999.0}))
        self.assertTrue(sane({"temp_f": 102.2}))

    def test_a_row_missing_the_filtered_column_is_kept(self):
        # Absence is not implausibility; only a present, impossible value is.
        self.assertTrue(sane({"soc_pct": 80.0}))

    def test_the_report_says_how_many_it_dropped(self):
        rows = _rows([("C3", {}), ("C4", {}), ("C5", {})])
        rows[1]["pack_v"] = 1.0
        collected = collect(rows, ["array_2b43"])
        self.assertEqual(collected["rows_dropped"], 1)
        self.assertIn("1 dropped as transitional",
                      format_findings(collected, rank(collected)))


class TestFieldsAndTargetsArePairedPerColumn(unittest.TestCase):
    """The bug that made the tool report nothing against a corpus that had it.

    Building the field series and the target series independently looks simpler
    and is wrong: a session recorded before a column existed still contributes
    target values, the two series then differ in length, and every pairing is
    silently skipped. The tool then says "nothing here" without having compared
    anything, which is worse than crashing.
    """

    def test_rows_without_the_column_do_not_break_the_pairing(self):
        rows = _rows([("0A", {"soc_pct": 10.0}), ("14", {"soc_pct": 20.0}),
                      ("1E", {"soc_pct": 30.0}), ("28", {"soc_pct": 40.0})])
        # A session predating the column: present in the corpus, no hex value.
        rows.append({"utc": "2026-09-02T00:00:00Z", "elapsed_s": 99.0,
                     "pack_v": 380.0, "soc_pct": 50.0})
        found = rank(collect(rows, ["array_2b43"]))
        b00 = [f for f in found if f["field"] == "array_2b43/b00"][0]
        self.assertEqual(b00["samples"], 4, "the extra row must not be paired in")
        self.assertTrue(b00["correlations"], "a perfect relationship must be found")
        self.assertAlmostEqual(b00["correlations"][0]["r"], 1.0, places=3)

    def test_a_column_absent_from_every_row_is_skipped_not_crashed(self):
        collected = collect(_rows([("0A", {})]), ["never_recorded"])
        self.assertNotIn("never_recorded", collected["columns"])


class TestWhatTheReportMustSay(unittest.TestCase):
    def test_a_constant_field_is_reported_rather_than_skipped(self):
        # hv_temp_raw held at 70 across 264 samples; two of 0x2AF5's unknown
        # bytes held through a 97.8 kW pull.  Not moving is the finding.
        rows = _rows([("46", {"soc_pct": float(10 * i)}) for i in range(1, 5)])
        found = rank(collect(rows, ["array_2b43"]))
        b00 = [f for f in found if f["field"] == "array_2b43/b00"][0]
        self.assertTrue(b00["constant"])
        self.assertEqual(b00["correlations"], [])
        self.assertIn("constant across every sample",
                      format_findings(collect(rows, ["array_2b43"]), found))

    def test_every_correlation_carries_the_span_it_was_measured_across(self):
        # A strong r across five degrees says almost nothing, and a reader
        # cannot tell without the span.
        rows = _rows([("0A", {"temp_f": 100.0}), ("14", {"temp_f": 101.0}),
                      ("1E", {"temp_f": 102.0}), ("28", {"temp_f": 103.0})])
        found = rank(collect(rows, ["array_2b43"]))
        best = [f for f in found if f["field"] == "array_2b43/b00"][0]
        span = [c for c in best["correlations"] if c["target"] == "temp_f"][0]
        self.assertAlmostEqual(span["target_span"], 3.0)
        self.assertIn("target spanned",
                      format_findings(collect(rows, ["array_2b43"]), found))

    def test_the_minimum_threshold_filters_weak_relationships(self):
        rows = _rows([("0A", {"soc_pct": 10.0}), ("14", {"soc_pct": 90.0}),
                      ("1E", {"soc_pct": 20.0}), ("28", {"soc_pct": 80.0})])
        strict = rank(collect(rows, ["array_2b43"]), minimum=0.99)
        self.assertFalse(any(f["correlations"] for f in strict))

    def test_an_empty_corpus_does_not_raise(self):
        collected = collect([], ["array_2b43"])
        self.assertEqual(collected["rows_kept"], 0)
        self.assertIn("FIELD DECODE", format_findings(collected, rank(collected)))


class TestNeverTouchesTheVehicle(unittest.TestCase):
    def test_the_module_opens_no_serial_device(self):
        source = open(decode_fields.__file__, encoding="utf-8").read()
        for forbidden in ("import serial", "from serial", "Transport",
                          "rfcomm", ".send("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_it_adds_no_third_party_dependency(self):
        # analyze.py is 700 lines of hand-rolled stdlib statistics on purpose,
        # and the deployment target is a Pi Zero.
        source = open(decode_fields.__file__, encoding="utf-8").read()
        for heavy in ("import pandas", "import numpy"):
            with self.subTest(heavy=heavy):
                self.assertNotIn(heavy, source)


class TestCli(unittest.TestCase):
    def test_it_reads_a_directory_of_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "drive-20260903T000000Z.csv")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("utc,elapsed_s,pack_v,soc_pct,array_2b43\n")
                for i in range(4):
                    handle.write(f"2026-09-03T00:00:0{i}Z,{i},380.0,"
                                 f"{10 * (i + 1)},{i + 10:02X}\n")
            out = os.path.join(tmp, "findings.json")
            self.assertEqual(
                decode_fields.main(["--dir", tmp, "--quiet", "--json", out]), 0)
            self.assertTrue(os.path.exists(out))

    def test_no_sessions_is_an_error_rather_than_an_empty_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(decode_fields.main(["--dir", tmp, "--quiet"]), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
