"""The live sensor view.

The property that matters most here is that a sensor which has gone quiet never
looks like one reporting zero.  That distinction is the whole reason the view
exists, and it is the one a CSV cannot show you.
"""

import os
import tempfile
import unittest

from hummer_obd import drive, live
from hummer_obd.live import column_sources, newest_session, render, snapshot


def _rows(samples):
    out = []
    for elapsed, extra in samples:
        row = {"utc": f"2026-09-03T16:00:{int(elapsed) % 60:02d}Z", "elapsed_s": float(elapsed)}
        row.update(extra)
        out.append(row)
    return out


class TestColumnSourcesAreDerivedNotWrittenDown(unittest.TestCase):
    """Every hand-kept inventory in this project has drifted at least once."""

    def test_every_recorded_column_has_a_source(self):
        sources = column_sources()
        missing = [c for c in drive.COLUMNS if c not in sources]
        self.assertEqual(missing, [], f"columns with no stated source: {missing}")

    def test_a_new_column_cannot_go_unattributed(self):
        # The map is built from drive.COLUMNS, GROUPS and DECODERS, so it
        # covers exactly what the recorder writes -- no more, no less.
        self.assertEqual(
            set(column_sources()) & set(drive.COLUMNS), set(drive.COLUMNS)
        )

    def test_columns_are_attributed_to_the_module_that_carries_them(self):
        sources = column_sources()
        for column, expect_module, expect_did in (
            ("soc_pct", "battery manager", "0x27C6"),
            ("pack_v", "pack power", "0x2885"),
            ("pack_a", "pack power", "0x2414"),
            ("brake_kpa", "brake / chassis", "0x4A7C"),
            ("dmc2_v", "drive motor", "0x33E5"),
            ("cell_spread_mv", "battery manager", "0x2AF5"),
        ):
            with self.subTest(column=column):
                group, did = sources[column]
                self.assertIn(expect_module, group)
                self.assertEqual(did, expect_did)

    def test_derived_columns_are_not_claimed_to_come_from_a_module(self):
        sources = column_sources()
        for column in ("power_kw", "hv_power_kw"):
            with self.subTest(column=column):
                self.assertIn("computed", sources[column][0])

    def test_the_standard_pid_label_follows_the_addressing_it_describes(self):
        # These were a functional broadcast until they were pointed at one
        # module.  A label reading "broadcast" would have survived that change
        # and quietly lied, which is the drift this whole map exists to avoid.
        where, _did = column_sources()["speed_kph"]
        header = next(c for c in drive.STANDARD_ADDRESS if c.startswith("ATSH"))
        if header.startswith("ATSHDA"):
            self.assertIn(header[6:8], where)
            self.assertNotIn("broadcast", where)
        else:
            self.assertIn("broadcast", where)

    def test_the_adapter_only_reading_is_marked_as_touching_no_bus(self):
        # This is the claim that makes the recorder safe to leave enabled.
        self.assertIn("CAN", column_sources()["volts"][0])


class TestSnapshotAgesEveryColumn(unittest.TestCase):
    def test_a_column_keeps_its_last_value_with_the_age_beside_it(self):
        rows = _rows([
            (0, {"pack_v": 390.0, "speed_kph": 55.0}),
            (10, {"pack_v": 391.0}),
            (20, {"pack_v": 392.0}),
        ])
        snap = snapshot(rows)
        self.assertEqual(snap["columns"]["pack_v"]["value"], 392.0)
        self.assertEqual(snap["columns"]["pack_v"]["age_s"], 0.0)
        # Still shown, but 20 s old -- not blank, and not mistakable for live.
        self.assertEqual(snap["columns"]["speed_kph"]["value"], 55.0)
        self.assertEqual(snap["columns"]["speed_kph"]["age_s"], 20.0)

    def test_a_column_that_never_answered_has_no_value(self):
        snap = snapshot(_rows([(0, {"pack_v": 390.0}), (10, {"pack_v": 391.0})]))
        self.assertIsNone(snap["columns"]["brake_kpa"]["value"])
        self.assertEqual(snap["columns"]["brake_kpa"]["samples"], 0)

    def test_a_zero_reading_is_a_value_and_not_a_silence(self):
        # The distinction the whole view exists for.
        snap = snapshot(_rows([(0, {"speed_kph": 0.0}), (10, {"speed_kph": 0.0})]))
        self.assertEqual(snap["columns"]["speed_kph"]["value"], 0.0)
        self.assertEqual(snap["columns"]["speed_kph"]["age_s"], 0.0)
        self.assertEqual(snap["columns"]["speed_kph"]["samples"], 2)

    def test_how_often_each_column_answered_is_counted(self):
        rows = _rows([
            (0, {"pack_v": 390.0, "speed_kph": 1.0}),
            (10, {"pack_v": 391.0}),
            (20, {"pack_v": 392.0}),
            (30, {"pack_v": 393.0}),
        ])
        snap = snapshot(rows)
        self.assertEqual(snap["columns"]["pack_v"]["samples"], 4)
        self.assertEqual(snap["columns"]["speed_kph"]["samples"], 1)
        self.assertEqual(snap["columns"]["speed_kph"]["of"], 4)

    def test_an_empty_session_does_not_raise(self):
        snap = snapshot([])
        self.assertEqual(snap["rows"], 0)
        self.assertIn("no samples yet", render(snap, path="x.csv"))


class TestRenderFlagsWhatIsWrong(unittest.TestCase):
    def test_a_stale_column_is_called_out(self):
        rows = _rows([
            (0, {"pack_v": 390.0, "speed_kph": 55.0}),
            (600, {"pack_v": 391.0}),
        ])
        text = render(snapshot(rows), path="drive.csv", stale_after=30.0)
        self.assertIn("STALE", text)
        self.assertIn("NOT ANSWERING", text)
        self.assertIn("speed_kph", text.split("NOT ANSWERING")[1])

    def test_a_column_that_never_answered_is_called_out_differently(self):
        rows = _rows([(0, {"pack_v": 390.0}), (10, {"pack_v": 391.0})])
        text = render(snapshot(rows), path="drive.csv")
        self.assertIn("NEVER ANSWERED", text)

    def test_a_healthy_session_says_so(self):
        row = {c: 1.0 for c in drive.COLUMNS if c not in ("utc", "array_2b43")}
        row["array_2b43"] = "DBDB"
        rows = _rows([(0, dict(row)), (5, dict(row))])
        text = render(snapshot(rows), path="drive.csv")
        self.assertIn("every column is answering", text)
        self.assertNotIn("STALE", text)

    def test_the_module_a_column_comes_from_appears_in_the_view(self):
        rows = _rows([(0, {"pack_v": 390.0}), (5, {"pack_v": 391.0})])
        text = render(snapshot(rows), path="drive.csv")
        self.assertIn("pack power", text)
        self.assertIn("0x2885", text)


class TestNeverTouchesTheVehicle(unittest.TestCase):
    """Safe to run at any time, including while driving."""

    def test_the_module_opens_no_serial_device_and_no_transport(self):
        source = open(live.__file__, encoding="utf-8").read()
        for forbidden in ("import serial", "from serial", "Transport",
                          "rfcomm", "SerialTransport"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_it_sends_no_command_because_it_has_no_way_to(self):
        source = open(live.__file__, encoding="utf-8").read()
        self.assertNotIn(".send(", source)


class TestSessionSelection(unittest.TestCase):
    def test_the_newest_session_is_chosen(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("drive-20260901T000000Z.csv", "drive-20260903T000000Z.csv"):
                open(os.path.join(tmp, name), "w").close()
            self.assertTrue(newest_session(tmp).endswith("drive-20260903T000000Z.csv"))

    def test_an_empty_directory_yields_nothing_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(newest_session(tmp))

    def test_the_cli_reports_when_there_is_no_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(live.main(["--dir", tmp]), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
