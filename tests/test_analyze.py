"""Offline analysis of a recorded session.

The arithmetic here is deliberately checkable by hand: constant speeds over
whole hours, so a wrong integral shows up as a wrong round number rather than
as a plausible one.
"""

import json
import os
import tempfile
import unittest

from hummer_obd import analyze
from hummer_obd.analyze import (
    KM_PER_MILE,
    analyze as analyze_session,
    format_report,
    read_session,
)
from hummer_obd.drive import COLUMNS


def _rows(samples):
    """Session rows from ``(elapsed_s, extra fields)`` pairs."""
    out = []
    for elapsed, extra in samples:
        row = {"utc": f"2026-09-03T15:00:{int(elapsed) % 60:02d}Z", "elapsed_s": float(elapsed)}
        row.update(extra)
        out.append(row)
    return out


class TestNumberParsing(unittest.TestCase):
    """A reading that is present must not be lost to its formatting."""

    def test_a_unit_suffix_does_not_discard_the_reading(self):
        # The volts column really is written as "13.8V" by the recorder.
        self.assertEqual(analyze._number("13.8V"), 13.8)

    def test_plain_numbers_and_negatives(self):
        for text, expected in (("0", 0.0), ("-1.06", -1.06), ("392.25", 392.25),
                               ("-0.00637", -0.00637), (" 95.0 ", 95.0)):
            with self.subTest(text=text):
                self.assertEqual(analyze._number(text), expected)

    def test_absent_and_non_numeric_are_none(self):
        for text in (None, "", "   ", "DBDBDDDD", "--"):
            with self.subTest(text=text):
                self.assertIsNone(analyze._number(text))


class TestDistanceAndSpeed(unittest.TestCase):
    def test_integrated_speed_matches_a_constant_hour(self):
        rows = _rows([(0, {"speed_kph": 100.0}), (3600, {"speed_kph": 100.0})])
        report = analyze_session(rows)
        self.assertAlmostEqual(report["motion"]["distance_from_speed_km"], 100.0, places=2)

    def test_odometer_distance_is_converted_to_miles(self):
        rows = _rows([
            (0, {"odometer_km": 2197.6, "speed_kph": 0.0}),
            (3600, {"odometer_km": 2213.7, "speed_kph": 0.0}),
        ])
        report = analyze_session(rows)
        self.assertAlmostEqual(report["motion"]["distance_km"], 16.1, places=2)
        self.assertAlmostEqual(
            report["motion"]["distance_mi"], round(16.1 / KM_PER_MILE, 2), places=2
        )

    def test_stopped_samples_are_counted_separately(self):
        rows = _rows([
            (0, {"speed_kph": 0.0}), (10, {"speed_kph": 0.0}),
            (20, {"speed_kph": 50.0}), (30, {"speed_kph": 60.0}),
        ])
        report = analyze_session(rows)
        self.assertEqual(report["motion"]["stopped_samples"], 2)
        self.assertEqual(report["motion"]["moving_samples"], 2)
        self.assertEqual(report["motion"]["max_speed_kph"], 60.0)


class TestHexColumnsAreNotParsedAsNumbers(unittest.TestCase):
    """A hex field missing from the text-column set fails silently.

    It gets fed to the number parser, fails, and reads as a column the vehicle
    never answered.  That is what happened to `cell_extra_raw` on its first
    live drive: the recorder captured it correctly and the reader threw it away.
    """

    def test_every_hex_column_survives_a_round_trip(self):
        # Values taken from a real drive: these all begin with a digit, so a
        # naive float() parse fails rather than truncating, which is why the
        # failure was invisible until a column read 0/33.
        samples = {
            "cell_extra_raw": "760FB817",
            "charger_5401_raw": "00",
            "array_2b43": "CCCCCDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCECD",
        }
        header = list(COLUMNS)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "drive-20260903T194958Z.csv")
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(",".join(header) + "\n")
                for elapsed in ("4.412", "7.16"):
                    row = dict(samples, utc="2026-09-03T19:50:04.013285Z",
                               elapsed_s=elapsed, pack_v="384.88")
                    handle.write(",".join(str(row.get(k, "")) for k in header) + "\n")
            rows, _warnings, _header = read_session(path)
        for column, expected in samples.items():
            with self.subTest(column=column):
                self.assertEqual(
                    rows[0][column], expected,
                    f"{column} must survive as text, not become None",
                )

    def test_a_hex_value_beginning_with_a_digit_is_not_silently_dropped(self):
        # "760FB817" starts with a digit, so the unit-suffix stripper leaves it
        # alone and float() then rejects the whole string.
        self.assertIsNone(analyze._number("760FB817"))
        self.assertIn("cell_extra_raw", analyze._TEXT_COLUMNS)


class TestDistanceSourcePreference(unittest.TestCase):
    """The obvious source is the least reliable one on this vehicle.

    `odometer_km` and `speed_kph` are standard OBD PIDs and answered in only 8
    of 79 rows on 2026-09-03, while every enhanced read answered in all 79.
    Keying distance off the odometer alone reported a 12.6-mile drive as 0.06.
    """

    def test_the_odometer_is_preferred_when_it_actually_moved(self):
        rows = _rows([
            (0, {"odometer_km": 100.0, "dist_since_chg_mi": 0.0}),
            (3600, {"odometer_km": 116.1, "dist_since_chg_mi": 9.0}),
        ])
        report = analyze_session(rows)
        self.assertEqual(report["motion"]["distance_basis"], "odometer_km")
        self.assertAlmostEqual(report["motion"]["distance_used_mi"], 10.0, places=1)

    def test_distance_since_charge_carries_a_drive_the_odometer_missed(self):
        # The odometer answered twice and did not change; the enhanced counter
        # answered every row and moved 12.68 miles.
        rows = _rows([
            (0, {"odometer_km": 2197.6, "dist_since_chg_mi": 0.0}),
            (600, {"dist_since_chg_mi": 6.0}),
            (1200, {"odometer_km": 2197.6, "dist_since_chg_mi": 12.68}),
        ])
        report = analyze_session(rows)
        self.assertEqual(report["motion"]["distance_basis"], "dist_since_chg_mi")
        self.assertAlmostEqual(report["motion"]["distance_used_mi"], 12.68, places=2)

    def test_a_charge_resetting_the_counter_is_not_a_reversing_truck(self):
        rows = _rows([
            (0, {"dist_since_chg_mi": 40.0}),
            (600, {"dist_since_chg_mi": 0.0}),
        ])
        report = analyze_session(rows)
        self.assertIsNone(
            report["motion"]["distance_since_charge_mi"],
            "a negative delta is the counter resetting on a charge",
        )

    def test_wheel_speeds_carry_distance_when_the_speed_pid_is_absent(self):
        rows = _rows([
            (0, {"wheel_fl_kph": 100.0, "wheel_fr_kph": 100.0,
                 "wheel_rl_kph": 100.0, "wheel_rr_kph": 100.0}),
            (3600, {"wheel_fl_kph": 100.0, "wheel_fr_kph": 100.0,
                    "wheel_rl_kph": 100.0, "wheel_rr_kph": 100.0}),
        ])
        report = analyze_session(rows)
        self.assertAlmostEqual(report["motion"]["distance_from_wheels_km"], 100.0, places=1)
        self.assertEqual(report["motion"]["distance_basis"], "wheel speeds")

    def test_a_derived_wheel_mean_is_not_reported_as_a_vehicle_column(self):
        rows = _rows([
            (0, {"wheel_fl_kph": 10.0, "wheel_fr_kph": 10.0,
                 "wheel_rl_kph": 10.0, "wheel_rr_kph": 10.0}),
            (10, {"wheel_fl_kph": 10.0, "wheel_fr_kph": 10.0,
                  "wheel_rl_kph": 10.0, "wheel_rr_kph": 10.0}),
        ])
        report = analyze_session(rows)
        invented = [k for k in report["completeness"] if "wheel_mean" in k or k.startswith("_")]
        self.assertEqual(invented, [], "completeness must report only what the vehicle sent")


class TestEnergyAndEfficiency(unittest.TestCase):
    def test_energy_used_is_the_fall_in_energy_remaining(self):
        rows = _rows([
            (0, {"energy_kwh": 172.52, "odometer_km": 0.0}),
            (3600, {"energy_kwh": 162.52, "odometer_km": 50.0}),
        ])
        report = analyze_session(rows)
        self.assertAlmostEqual(report["energy"]["energy_used_kwh"], 10.0, places=3)
        # 50 km is 31.07 mi, so 3.11 mi/kWh.
        self.assertAlmostEqual(
            report["energy"]["efficiency_mi_per_kwh"],
            round((50.0 / KM_PER_MILE) / 10.0, 2), places=2,
        )

    def test_implied_pack_size_cross_checks_energy_against_soc(self):
        rows = _rows([(0, {"energy_kwh": 172.52, "soc_pct": 89.653})])
        report = analyze_session(rows)
        # 172.52 / 0.89653 = 192.4 kWh, which is the pack size the vehicle
        # itself implies rather than a specification figure.
        self.assertAlmostEqual(report["energy"]["implied_pack_kwh"], 192.4, places=1)

    def test_regenerated_energy_is_separated_from_energy_drawn(self):
        # An hour drawing 20 kW, then an hour regenerating 10 kW.
        rows = _rows([
            (0, {"hv_power_kw": 20.0}),
            (3600, {"hv_power_kw": 20.0}),
            (3601, {"hv_power_kw": -10.0}),
            (7201, {"hv_power_kw": -10.0}),
        ])
        report = analyze_session(rows)
        self.assertAlmostEqual(report["energy"]["drawn_kwh_from_pack_current"], 20.0, delta=0.05)
        self.assertAlmostEqual(report["energy"]["regen_kwh_from_pack_current"], 10.0, delta=0.05)
        self.assertAlmostEqual(report["energy"]["net_kwh_from_pack_current"], 10.0, delta=0.1)

    def test_no_efficiency_is_claimed_without_a_distance(self):
        rows = _rows([(0, {"energy_kwh": 100.0}), (3600, {"energy_kwh": 90.0})])
        report = analyze_session(rows)
        self.assertNotIn("efficiency_mi_per_kwh", report["energy"])


class TestDecoderCrossChecks(unittest.TestCase):
    """Ratios between decoded fields test the decoders, not the drive.

    Each divides one decoded field by another, so a scaling that silently
    changes -- a byte offset moving, a divisor edited, a source reinterpreted --
    moves these away from figures already measured across a thousand samples.
    """

    def test_series_cell_count_falls_out_of_pack_and_cell_voltage(self):
        rows = _rows([
            (0, {"pack_v": 392.40, "cell_avg_v": 4.0885}),
            (10, {"pack_v": 389.89, "cell_avg_v": 4.0595}),
        ])
        got = analyze_session(rows)["cross_checks"]["series_cells"]
        self.assertAlmostEqual(got["mean"], 96.0, delta=0.1)
        self.assertEqual(got["expected"], analyze.EXPECTED_SERIES_CELLS)

    def test_usable_capacity_falls_out_of_energy_and_charge(self):
        rows = _rows([
            (0, {"energy_kwh": 172.52, "soc_pct": 89.653}),
            (10, {"energy_kwh": 164.45, "soc_pct": 85.451}),
        ])
        got = analyze_session(rows)["cross_checks"]["implied_pack_kwh"]
        self.assertAlmostEqual(got["mean"], 192.0, delta=1.5)

    def test_a_changed_scaling_is_flagged_rather_than_reported_as_physics(self):
        # Half the pack voltage: the drive is unremarkable, the decoder is not.
        rows = _rows([
            (0, {"pack_v": 196.2, "cell_avg_v": 4.0885}),
            (10, {"pack_v": 195.0, "cell_avg_v": 4.0595}),
        ])
        report = analyze_session(rows)
        self.assertAlmostEqual(report["cross_checks"]["series_cells"]["mean"], 48.0, delta=0.5)
        self.assertTrue(
            any("cells in series" in w for w in report["warnings"]),
            f"a decoder scaling change must be called out, got {report['warnings']}",
        )

    def test_a_correct_session_raises_no_cross_check_warning(self):
        rows = _rows([
            (0, {"pack_v": 392.40, "cell_avg_v": 4.0885,
                 "energy_kwh": 172.52, "soc_pct": 89.653}),
            (10, {"pack_v": 389.89, "cell_avg_v": 4.0595,
                  "energy_kwh": 164.45, "soc_pct": 85.451}),
        ])
        warnings = analyze_session(rows)["warnings"]
        self.assertFalse([w for w in warnings if "expected" in w], warnings)

    def test_a_ratio_needs_two_samples_before_it_claims_anything(self):
        rows = _rows([(0, {"pack_v": 392.40, "cell_avg_v": 4.0885})])
        self.assertNotIn("series_cells", analyze_session(rows).get("cross_checks", {}))

    def test_a_zero_denominator_does_not_divide(self):
        rows = _rows([
            (0, {"pack_v": 392.40, "cell_avg_v": 0.0}),
            (10, {"pack_v": 389.89, "cell_avg_v": 0.0}),
        ])
        analyze_session(rows)  # must not raise

    def test_the_checks_are_shown_in_the_text_report(self):
        rows = _rows([
            (0, {"pack_v": 392.40, "cell_avg_v": 4.0885}),
            (10, {"pack_v": 389.89, "cell_avg_v": 4.0595}),
        ])
        self.assertIn("cells in series", format_report(analyze_session(rows)))


class TestPowerCrossCheck(unittest.TestCase):
    """The two power columns carry opposite signs; the report must normalise."""

    def test_agreeing_routes_report_a_small_difference(self):
        # Discharging at 8 kW: hv_power_kw is positive, power_kw (the slope of
        # energy *remaining*) is negative for the same physical event.
        rows = _rows([
            (0, {"hv_power_kw": 8.0, "power_kw": -8.0}),
            (10, {"hv_power_kw": 8.1, "power_kw": -7.9}),
        ])
        report = analyze_session(rows)
        cross = report["power_cross_check"]
        self.assertEqual(cross["samples_compared"], 2)
        self.assertLess(cross["mean_abs_difference_kw"], 0.3)
        self.assertGreater(cross["mean_hv_power_kw"], 0)
        self.assertGreater(cross["mean_slope_power_kw"], 0, "both should normalise to discharge-positive")
        self.assertEqual(report["warnings"], [])

    def test_a_route_with_the_wrong_sign_is_flagged(self):
        # If both columns were positive while discharging, one of them is not
        # what it is labelled -- exactly the failure this check exists for.
        rows = _rows([
            (0, {"hv_power_kw": 8.0, "power_kw": 8.0}),
            (10, {"hv_power_kw": 8.0, "power_kw": 8.0}),
        ])
        report = analyze_session(rows)
        self.assertTrue(
            any("disagree" in w for w in report["warnings"]),
            f"expected a disagreement warning, got {report['warnings']}",
        )


class TestCaptureQuality(unittest.TestCase):
    """Reported first, because every other figure inherits it."""

    def test_the_median_period_is_measured_not_assumed(self):
        rows = _rows([(0, {}), (9.5, {}), (19.0, {}), (28.5, {})])
        report = analyze_session(rows)
        self.assertAlmostEqual(report["sampling"]["median_period_s"], 9.5, places=2)

    def test_a_coarser_capture_than_intended_is_flagged(self):
        rows = _rows([(0, {}), (9.5, {}), (19.0, {})])
        report = analyze_session(rows, expected_period_s=5.5)
        self.assertTrue(
            any("coarser than intended" in w for w in report["warnings"]),
            f"got {report['warnings']}",
        )

    def test_a_capture_that_met_its_target_is_not_flagged(self):
        rows = _rows([(0, {}), (5.5, {}), (11.0, {})])
        report = analyze_session(rows, expected_period_s=5.5)
        self.assertFalse([w for w in report["warnings"] if "coarser" in w])

    def test_a_dropped_read_shows_up_as_a_gap(self):
        # Five normal periods then one long one: the shape of a Bluetooth drop.
        rows = _rows([(t, {}) for t in (0, 5, 10, 15, 20, 320)])
        report = analyze_session(rows)
        self.assertEqual(report["sampling"]["gap_count"], 1)
        self.assertAlmostEqual(report["sampling"]["gap_seconds_total"], 300.0, places=1)
        self.assertTrue(any("gap" in w for w in report["warnings"]))


class TestSessionFileHandling(unittest.TestCase):
    def test_a_real_shaped_session_round_trips(self):
        header = list(COLUMNS)
        live = {
            "utc": "2026-09-03T15:44:19.366887Z", "elapsed_s": "1146.123",
            "volts": "13.8V", "speed_kph": "0", "odometer_km": "2197.6",
            "soc_pct": "89.653", "energy_kwh": "172.52", "range_mi": "301.35",
            "dist_since_chg_mi": "0.0", "temp_f": "95.0", "charger_5401_raw": "00",
            "power_kw": "-3.0", "cell_avg_v": "4.0867", "cell_min_v": "4.0863",
            "cell_max_v": "4.0885", "cell_spread_mv": "2.2", "pack_v": "392.25",
            "pack_a": "9.3", "hv_power_kw": "3.65", "dmc2_v": "13.1",
            "wheel_fl_kph": "0", "wheel_fr_kph": "0", "wheel_rl_kph": "0",
            "wheel_rr_kph": "0", "brake_kpa": "2500", "steering_deg": "-1.06",
            "lateral_g": "0.0", "longitudinal_g": "-0.00637",
            "array_2b43": "DBDBDDDDDDDDDDDDDDDDDDDDDDDEDDDDDDDDDDDDDDDDDDDDDEDD",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "drive-20260903T152511Z.csv")
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(",".join(header) + "\n")
                for elapsed in (1146.123, 1155.6):
                    row = dict(live, elapsed_s=str(elapsed))
                    handle.write(",".join(str(row.get(k, "")) for k in header) + "\n")
            rows, warnings, read_header = read_session(path)

        self.assertEqual(read_header, header)
        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 2)
        # The unit suffix is stripped, the opaque array is kept as text.
        self.assertEqual(rows[0]["volts"], 13.8)
        self.assertEqual(rows[0]["pack_v"], 392.25)
        self.assertTrue(rows[0]["array_2b43"].startswith("DBDB"))
        report = analyze_session(rows, path="drive.csv", expected_period_s=9.5)
        self.assertEqual(report["session"]["rows"], 2)
        self.assertEqual(report["pack"]["v_min"], 392.25)
        self.assertEqual(report["cells"]["spread_mv_max"], 2.2)
        self.assertEqual(report["chassis"]["brake_kpa_max"], 2500.0)
        # Formatting must not raise on a report with every section present.
        self.assertIn("capture quality", format_report(report))
        # And it must be serialisable, since --json writes it.
        json.dumps(report)

    def test_a_torn_final_row_is_dropped_with_a_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "drive.csv")
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write("utc,elapsed_s,pack_v\n")
                handle.write("2026-09-03T15:00:00Z,0,392.0\n")
                handle.write("2026-09-03T15:00:10Z\n")  # power cut mid-write
            rows, warnings, _ = read_session(path)
        self.assertEqual(len(rows), 1, "the torn row must not reach the analysis")
        self.assertTrue(any("incomplete" in w for w in warnings))

    def test_an_empty_session_says_so_rather_than_dividing_by_zero(self):
        report = analyze_session([])
        self.assertEqual(report["session"]["rows"], 0)
        self.assertIn("no rows", " ".join(report["warnings"]))
        self.assertIn("session", format_report(report))


class TestDegenerateSessions(unittest.TestCase):
    """A capture cut short must still produce a readable report."""

    def test_a_single_row_session_reports_rather_than_raising(self):
        rows = _rows([(0, {"pack_v": 392.0, "speed_kph": 0.0, "energy_kwh": 172.52,
                           "soc_pct": 89.653, "hv_power_kw": 0.0})])
        report = analyze_session(rows)
        self.assertEqual(report["session"]["rows"], 1)
        # There is no period to measure from one sample, and saying so is
        # better than inventing one.
        self.assertIsNone(report["sampling"]["median_period_s"])
        self.assertIsNone(report["sampling"]["min_period_s"])
        text = format_report(report)
        self.assertNotIn("None", text, f"a one-row report should render '--', got:\n{text}")

    def test_a_clamped_integral_does_not_report_negative_zero(self):
        # Integrating a clamped series can land on -0.0, which in a report
        # about which way energy flowed reads as a direction.
        rows = _rows([(0, {"hv_power_kw": 0.0}), (10, {"hv_power_kw": 0.0})])
        report = analyze_session(rows)
        for key in ("regen_kwh_from_pack_current", "drawn_kwh_from_pack_current",
                    "net_kwh_from_pack_current"):
            with self.subTest(key=key):
                self.assertEqual(report["energy"][key], 0.0)
                self.assertNotIn("-0.0", str(report["energy"][key]))


class TestCompleteness(unittest.TestCase):
    def test_an_empty_column_is_reported_rather_than_read_as_zero(self):
        rows = _rows([
            (0, {"pack_v": 392.0, "pack_a": None}),
            (10, {"pack_v": 392.1, "pack_a": None}),
        ])
        report = analyze_session(rows)
        self.assertEqual(report["completeness"]["pack_a"], "0/2")
        self.assertEqual(report["completeness"]["pack_v"], "2/2")
        self.assertTrue(any("pack_a" in w and "empty" in w for w in report["warnings"]))
        self.assertIsNone(report["pack"]["a_min"])


class TestNeverTouchesTheVehicle(unittest.TestCase):
    """The same property the capabilities report is held to."""

    def test_the_module_imports_no_transport_and_no_serial(self):
        source = open(analyze.__file__, encoding="utf-8").read()
        for forbidden in ("import serial", "from serial", "Transport", "rfcomm"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


class TestCli(unittest.TestCase):
    def test_the_newest_session_in_a_directory_is_chosen(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("drive-20260901T000000Z.csv", "drive-20260903T000000Z.csv"):
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as handle:
                    handle.write("utc,elapsed_s,pack_v\n2026-09-03T15:00:00Z,0,392.0\n")
            out = os.path.join(tmp, "report.json")
            self.assertEqual(analyze.main(["--dir", tmp, "--quiet", "--json", out]), 0)
            report = json.loads(open(out, encoding="utf-8").read())
        self.assertTrue(report["session"]["path"].endswith("drive-20260903T000000Z.csv"))

    def test_a_missing_directory_fails_rather_than_reporting_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(analyze.main(["--dir", tmp, "--quiet"]), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
