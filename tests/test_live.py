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


class TestGroupedColumnsAreBrokenOut(unittest.TestCase):
    """A value that is captured but not visible will not get checked."""

    #: A real 0x2B43 reply from the vehicle.
    ARRAY = "C3C3C4C4C4C4C4C4C4C4C4C4C4C4C4C4C4C4C4C4C4C4C4C4C5C4"

    def test_all_twenty_six_values_are_shown_individually(self):
        entries = live._expand_array(self.ARRAY)
        self.assertEqual(len(entries), 26)
        for index, (label, _value) in enumerate(entries):
            with self.subTest(index=index):
                self.assertIn(f"{index:02d}", label)

    def test_each_value_is_shown_against_its_own_block(self):
        # Blocks matter: the two halves sit at slightly different levels, so a
        # drift measured against the whole array would flag every member of the
        # lower block and hide a real outlier inside it.
        blocks = [label.split("block ")[1].rstrip(")") for label, _ in
                  live._expand_array(self.ARRAY)]
        self.assertEqual(blocks[:2], ["?", "?"])
        self.assertEqual(set(blocks[2:14]), {"A"})
        self.assertEqual(set(blocks[14:]), {"B"})

    def test_a_module_pulling_away_from_its_neighbours_is_flagged(self):
        # One value dragged well below its block; the rest identical.
        raw = bytearray(bytes.fromhex(self.ARRAY))
        raw[7] = raw[7] - 6
        entries = live._expand_array(raw.hex().upper())
        self.assertIn("drifting", entries[7][1])
        # And its neighbours are not flagged with it.
        self.assertNotIn("drifting", entries[6][1])
        self.assertNotIn("drifting", entries[8][1])

    def test_a_flat_array_flags_nothing(self):
        entries = live._expand_array("C4" * 26)
        self.assertFalse([e for e in entries if "drifting" in e[1]])

    def test_undecoded_bytes_are_shown_as_hex_and_decimal(self):
        entries = live._expand_bytes("740FB317")
        self.assertEqual(len(entries), 4)
        self.assertIn("0x74", entries[0][1])
        self.assertIn("116", entries[0][1])
        self.assertIn("23", entries[3][1])

    def test_an_unparseable_value_does_not_raise(self):
        for expander in (live._expand_array, live._expand_bytes):
            with self.subTest(expander=expander.__name__):
                self.assertTrue(expander("not hex at all"))

    def test_the_view_breaks_the_array_out_by_default(self):
        rows = _rows([(0, {"array_2b43": self.ARRAY}), (5, {"array_2b43": self.ARRAY})])
        text = render(snapshot(rows), path="drive.csv")
        self.assertIn("value 00", text)
        self.assertIn("value 25", text)

    def test_compact_mode_leaves_it_collapsed(self):
        rows = _rows([(0, {"array_2b43": self.ARRAY}), (5, {"array_2b43": self.ARRAY})])
        text = render(snapshot(rows), path="drive.csv", expand=False)
        self.assertNotIn("value 00", text)

    def test_a_column_absent_from_an_older_session_is_skipped(self):
        # cell_extra_raw did not exist when the early sessions were recorded.
        rows = _rows([(0, {"pack_v": 390.0}), (5, {"pack_v": 391.0})])
        text = render(snapshot(rows), path="drive.csv")
        self.assertNotIn("0x2AF5 -- the 4 trailing bytes", text)


class TestModuleTemperatureArray(unittest.TestCase):
    """0x2AF1, proven to answer on 2026-09-03 and captured raw since."""

    #: The exact payload the vehicle returned when first asked.
    AF1 = "727372727272727272727373737372727272727272727372"

    def test_all_twenty_four_values_are_shown(self):
        entries = live._expand_flat_array(self.AF1)
        self.assertEqual(len(entries), 24)

    def test_no_block_structure_is_imposed_on_it(self):
        # 0x2B43's blocks were measured before they were used.  Nothing has
        # measured structure here, so none is asserted.
        for label, _value in live._expand_flat_array(self.AF1):
            with self.subTest(label=label):
                self.assertNotIn("block", label)

    def test_a_module_running_hot_is_flagged(self):
        raw = bytearray(bytes.fromhex(self.AF1))
        raw[9] = raw[9] + 6
        entries = live._expand_flat_array(raw.hex().upper())
        self.assertIn("drifting", entries[9][1])
        self.assertNotIn("drifting", entries[8][1])

    def test_an_even_array_flags_nothing(self):
        self.assertFalse(
            [e for e in live._expand_flat_array("72" * 24) if "drifting" in e[1]]
        )

    def test_it_is_carried_as_text_not_parsed_as_a_number(self):
        # The failure that made cell_extra_raw read as never answered.
        from hummer_obd.analyze import _TEXT_COLUMNS
        self.assertIn("array_2af1", _TEXT_COLUMNS)

    def test_the_recorder_asks_for_it_and_has_a_column_for_it(self):
        self.assertIn("array_2af1", drive.COLUMNS)
        battery = [g for g in drive.GROUPS if g.name == "battery"][0]
        self.assertIn("2AF1", battery.dids)

    def test_the_view_breaks_it_out(self):
        rows = _rows([(0, {"array_2af1": self.AF1}), (5, {"array_2af1": self.AF1})])
        text = render(snapshot(rows), path="drive.csv")
        self.assertIn("0x2AF1", text)
        self.assertIn("value 23", text)

    def test_the_scaling_is_not_asserted_anywhere(self):
        # The source calls these temperatures.  One sample at one temperature
        # is not enough to name a column after, and nothing here does.
        source = open(live.__file__, encoding="utf-8").read()
        source += open(drive.__file__, encoding="utf-8").read()
        for claim in ('"module_temp', '"temp_c', 'array_2af1_c'):
            with self.subTest(claim=claim):
                self.assertNotIn(claim, source)


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


class TestDerivedQuantities(unittest.TestCase):
    """The derived block: what the raw columns mean.

    These exist because the first working version of this block printed a
    1.06 V pack and 0.3 cells in series -- it read the very last row of a real
    session, which was the vehicle going to sleep with the contactors open.
    A person glancing at that sees a broken decoder, not a sleeping truck.
    """

    def test_pack_state_ignores_the_contactors_open_row(self):
        rows = _rows([
            (0, {"pack_v": 388.6, "cell_avg_v": 4.0479, "soc_pct": 80.0,
                 "energy_kwh": 152.4}),
            (9, {"pack_v": 388.7, "cell_avg_v": 4.0489, "soc_pct": 80.0,
                 "energy_kwh": 152.4}),
            # The vehicle drops the contactors: a real row, an impossible pack.
            (18, {"pack_v": 1.06, "cell_avg_v": 4.0489, "soc_pct": 80.0,
                  "energy_kwh": 152.4}),
        ])
        d = live.derive(rows)
        self.assertAlmostEqual(d["pack_v"], 388.7, places=1)
        self.assertGreater(d["series_cells"], 90)
        self.assertLess(d["series_cells"], 100)

    def test_efficiency_is_measured_over_the_moving_window_only(self):
        # Parked with a load for two samples (energy falls, distance does not),
        # then a drive.  Charging that parked draw against the drive's distance
        # is what turned a real 42 kWh/100km into 63.
        rows = _rows([
            (0,  {"speed_kph": 0.0, "odometer_km": 100.0, "energy_kwh": 160.0,
                  "pack_v": 388.0, "cell_avg_v": 4.04}),
            (10, {"speed_kph": 0.0, "odometer_km": 100.0, "energy_kwh": 158.0,
                  "pack_v": 388.0, "cell_avg_v": 4.04}),
            (20, {"speed_kph": 50.0, "odometer_km": 100.0, "energy_kwh": 158.0,
                  "pack_v": 388.0, "cell_avg_v": 4.04}),
            (30, {"speed_kph": 50.0, "odometer_km": 110.0, "energy_kwh": 154.0,
                  "pack_v": 388.0, "cell_avg_v": 4.04}),
        ])
        d = live.derive(rows)
        self.assertAlmostEqual(d["drive_km"], 10.0, places=3)
        self.assertAlmostEqual(d["drive_kwh"], 4.0, places=3)
        self.assertAlmostEqual(d["kwh_per_100km"], 40.0, places=1)
        # The whole-session figure keeps the parked draw and is reported too,
        # so the two are visibly different rather than silently merged.
        self.assertAlmostEqual(d["energy_used_kwh"], 6.0, places=3)

    def test_resistance_recovers_a_known_value_from_a_synthetic_pack(self):
        # V = OCV - I*R with R = 20 mOhm exactly.
        ocv, r_ohms = 390.0, 0.020
        samples = []
        for i, amps in enumerate([0, 200, 0, 400, 0, -150, 300, 0]):
            samples.append((i * 9, {
                "pack_a": float(amps),
                "pack_v": ocv - amps * r_ohms,
                "cell_avg_v": 4.04,
            }))
        result = live.pack_resistance(_rows(samples))
        self.assertIsNotNone(result)
        milliohms, n, _r = result
        self.assertAlmostEqual(milliohms, 20.0, places=6)
        self.assertGreaterEqual(n, live._MIN_STEPS)

    def test_resistance_is_withheld_when_the_current_never_moves(self):
        # A parked session cannot measure resistance and must not pretend to:
        # dividing sensor noise by a near-zero current step gives a number.
        rows = _rows([(i * 9, {"pack_a": 0.4, "pack_v": 388.6}) for i in range(40)])
        self.assertIsNone(live.pack_resistance(rows))

    def test_the_torque_signal_is_reported_as_signed_counts_from_its_zero(self):
        for raw, direction, counts in (("5806", "neutral", 0),
                                       ("5A00", "drive", 0x5A00 - live.TORQUE_ZERO),
                                       ("5000", "regen", 0x5000 - live.TORQUE_ZERO)):
            with self.subTest(raw=raw):
                d = live.derive(_rows([(0, {"field_2429_raw": raw})]))
                self.assertEqual(d["torque_counts"], counts)
                self.assertEqual(d["torque_dir"], direction)

    def test_the_state_word_is_read_under_either_header_era(self):
        # Sessions written before the rename carry batt_temp_a_raw; the live
        # view must not go blank on the entire back catalogue.
        for column in ("field_4127_raw", "batt_temp_a_raw"):
            with self.subTest(column=column):
                d = live.derive(_rows([(0, {column: "0418"})]))
                self.assertEqual(d["state_word"], 1048)
                self.assertIn("no road speed", d["powertrain"])

    def test_a_hex_column_is_never_read_as_a_decimal_number(self):
        # "0418" is 1048, not 418.  float() would take it and not complain.
        d = live.derive(_rows([(0, {"field_4127_raw": "0418",
                                    "thermal_energy_raw": "00BE"})]))
        self.assertEqual(d["state_word"], 1048)
        self.assertEqual(d["thermal_energy"], 190)

    def test_the_block_renders_with_nothing_available(self):
        # Every field absent must give dashes, not a traceback: the recorder
        # writes rows before the vehicle answers anything.
        text = "\n".join(live.render_derived(live.derive(_rows([(0, {})]))))
        self.assertIn("--", text)
        self.assertIn("TRACTION PACK", text)

    def test_derive_on_no_rows_is_empty_rather_than_an_error(self):
        self.assertEqual(live.derive([]), {})
