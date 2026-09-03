"""The drive/charge session recorder.

Two properties matter more than the rest and are tested first: that a sleeping
vehicle receives nothing but ``ATRV``, and that the recorder cannot transmit
anything neither safety gate allows.
"""

import unittest

from hummer_obd import drive
from hummer_obd.drive import (
    COLUMNS,
    DECODERS,
    GROUPS,
    SESSION_INIT,
    STANDARD_ADDRESS,
    STANDARD_PIDS,
    record,
    run_auto,
)
from hummer_obd.safety import (
    ENHANCED_READ_DIDS,
    UnsafeCommandError,
    validate_command,
    validate_enhanced_command,
    validate_supervised_command,
)
from hummer_obd.transport import Response, Transport

#: Real frames captured from the vehicle on 2026-09-03.
REPLIES = {
    "2227C6": "142AF1CB056227C6C9DB\r\r>",
    "2227AF": "142AF1CB056227AF3B0C\r\r>",
    "2227C7": "142AF1CB066227C700693C\r\r>",
    "2227C0": "142AF1CB066227C0000138\r\r>",
    "220046": "142AF1CB0462004653\r\r>",
    "225401": "142AF1CB0462540196\r\r>",
    "224A7A": "142AF12807624A7A00000000\r\r>",
    "224A7C": "142AF12804624A7C0A\r\r>",
    "224C2D": "142AF12805624C2DFFBF\r\r>",
    "224C2F": "142AF12805624C2F0000\r\r>",
    "224C30": "142AF12805624C30FFFC\r\r>",
    "2233E5": "142AF11D046233E583\r\r>",
    "222885": "142AF1170562288597CC\r\r>",
    "222414": "142AF11705622414FE5D\r\r>",
    "010D": "18DAF11D03410D00\r\r>",
    "ATRV": "13.9V\r\r>",
}


class _Fake(Transport):
    def __init__(self, replies=None, volts_sequence=None):
        self.sent = []
        self._replies = dict(REPLIES)
        if replies:
            self._replies.update(replies)
        self._volts = list(volts_sequence or [])

    def open(self):  # pragma: no cover
        pass

    def close(self):  # pragma: no cover
        pass

    def send(self, command, timeout=None):
        self.sent.append(command)
        if command == "ATRV" and self._volts:
            data = f"{self._volts.pop(0)}V\r\r>"
        else:
            data = self._replies.get(command, "OK\r\r>")
        return Response(command=command, data=data.encode("ascii"), elapsed_s=0.01)


class TestSleepingVehicleSeesNoTraffic(unittest.TestCase):
    """The property that makes this safe to enable at boot."""

    def test_auto_mode_sends_only_atrv_while_asleep(self):
        fake = _Fake(volts_sequence=[12.8, 12.8, 12.8])
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 3

        run_auto(fake, output_dir="/tmp", sleeper=lambda s: None, stop=stop)
        self.assertTrue(fake.sent, "should have polled at least once")
        self.assertEqual(
            set(fake.sent), {"ATRV"},
            f"a sleeping vehicle must see only ATRV, saw {sorted(set(fake.sent))}",
        )

    def test_atrv_reaches_no_vehicle_module(self):
        # Cross-check against the module that already asserts this at import.
        from hummer_obd.voltage import assert_no_vehicle_traffic

        assert_no_vehicle_traffic(("ATRV",))


class TestEveryCommandIsGated(unittest.TestCase):
    def test_session_init_passes_the_ordinary_gate(self):
        for command in SESSION_INIT:
            with self.subTest(command=command):
                self.assertEqual(validate_command(command), command)

    def test_group_addressing_passes_the_ordinary_gate(self):
        for group in GROUPS:
            for command in group.address:
                with self.subTest(group=group.name, command=command):
                    self.assertEqual(validate_command(command), command)

    def test_standard_addressing_and_pids_pass_the_ordinary_gate(self):
        for command in STANDARD_ADDRESS + tuple(c for c, _ in STANDARD_PIDS):
            with self.subTest(command=command):
                self.assertEqual(validate_command(command), command)

    def test_every_did_is_on_the_enhanced_allowlist(self):
        for group in GROUPS:
            for did in group.dids:
                with self.subTest(did=did):
                    self.assertIn(did, ENHANCED_READ_DIDS)
                    self.assertEqual(validate_enhanced_command(f"22{did}"), f"22{did}")

    def test_recorder_transmits_nothing_outside_the_two_gates(self):
        fake = _Fake()
        record(fake, max_cycles=1, sleeper=lambda s: None)
        for command in fake.sent:
            with self.subTest(command=command):
                self.assertEqual(validate_supervised_command(command), command)


class TestUnionGate(unittest.TestCase):
    def test_accepts_what_either_gate_accepts(self):
        for command in ("010D", "01A6", "ATRV", "ATCP18", "2227C6", "224A7A"):
            with self.subTest(command=command):
                self.assertEqual(validate_supervised_command(command), command)

    def test_refuses_what_neither_gate_accepts(self):
        for command in ("04", "2E27C6", "3101", "2701", "1102", "ATMA", "2227C5"):
            with self.subTest(command=command):
                with self.assertRaises(UnsafeCommandError):
                    validate_supervised_command(command)

    def test_is_exactly_the_union_and_widens_nothing(self):
        # Anything the union accepts must be accepted by one of the two gates.
        samples = [
            "010D", "0100", "03", "0902", "ATRV", "ATZ", "ATCP14", "ATSHDA28F1",
            "2227C6", "222AF5", "224C30", "04", "2E1234", "2227C5", "22FFFF",
            "ATMA", "3101", "", "010D;03",
        ]
        for command in samples:
            with self.subTest(command=command):
                try:
                    validate_supervised_command(command)
                except UnsafeCommandError:
                    continue
                ok_ordinary = True
                try:
                    validate_command(command)
                except UnsafeCommandError:
                    ok_ordinary = False
                ok_enhanced = True
                try:
                    validate_enhanced_command(command)
                except UnsafeCommandError:
                    ok_enhanced = False
                self.assertTrue(
                    ok_ordinary or ok_enhanced,
                    f"union accepted {command!r} that neither gate allows",
                )


class TestDecoders(unittest.TestCase):
    def test_captured_frames_decode_to_the_expected_values(self):
        fake = _Fake()
        session = record(fake, max_cycles=1, sleeper=lambda s: None)
        row = session.rows[0]
        self.assertAlmostEqual(row["soc_pct"], 0xC9DB / 655.35, places=3)
        self.assertAlmostEqual(row["energy_kwh"], 0x3B0C / 100, places=2)
        self.assertEqual(row["temp_f"], round((0x53 - 40) * 1.8 + 32, 1))
        self.assertEqual(row["brake_kpa"], 0)
        self.assertEqual(
            [row["wheel_fl_kph"], row["wheel_fr_kph"],
             row["wheel_rl_kph"], row["wheel_rr_kph"]],
            [0, 0, 0, 0],
        )
        self.assertAlmostEqual(row["steering_deg"], -65 * 0.022, places=2)
        self.assertEqual(row["dmc2_v"], 13.1)

    def test_pack_voltage_and_current_decode_from_the_captured_frames(self):
        fake = _Fake()
        row = record(fake, max_cycles=1, sleeper=lambda s: None).rows[0]
        self.assertAlmostEqual(row["pack_v"], 388.60, places=2)
        self.assertAlmostEqual(row["pack_a"], -20.95, places=2)
        # Negative current is charging, and the product must follow that sign.
        self.assertLess(row["hv_power_kw"], 0)
        self.assertAlmostEqual(abs(row["hv_power_kw"]), 8.14, places=1)

    def test_pack_current_matches_its_sources_own_test_vectors(self):
        # OBDb/Cadillac-LYRIQ PR #14 ships these pairs; the formula must
        # reproduce them exactly or the identifier is not understood.
        self.assertAlmostEqual(
            DECODERS["2414"](bytes.fromhex("FE39"))["pack_a"], -22.75, places=2)
        self.assertAlmostEqual(
            DECODERS["2414"](bytes.fromhex("0012"))["pack_a"], 0.9, places=2)

    def test_hv_power_is_absent_without_both_halves(self):
        fake = _Fake(replies={"222414": "142AF117037F2231\r\r>"})
        row = record(fake, max_cycles=1, sleeper=lambda s: None).rows[0]
        self.assertNotIn("hv_power_kw", row)

    def test_signed_fields_decode_negative(self):
        self.assertLess(DECODERS["4C2D"](bytes.fromhex("FFBF"))["steering_deg"], 0)
        self.assertLess(DECODERS["4C30"](bytes.fromhex("FFFC"))["longitudinal_g"], 0)

    def test_short_payload_is_a_missing_sample_not_a_crash(self):
        # A truncated reply must not end a session that is recording a drive.
        for did in ("27C6", "27AF", "27C7", "2AF5", "4A7A", "4C2D"):
            with self.subTest(did=did):
                self.assertEqual(DECODERS[did](b""), {})
                self.assertEqual(DECODERS[did](b"\x01"), {})

    def test_charger_identifier_is_not_decoded(self):
        # This vehicle answers with one byte where the source describes two, so
        # the published equation is deliberately not applied.
        out = DECODERS["5401"](bytes.fromhex("96"))
        self.assertEqual(out, {"charger_5401_raw": "96"})
        self.assertNotIn("charger_kw", out)

    def test_2b43_is_kept_raw(self):
        payload = bytes.fromhex("C3C3C4C4")
        self.assertEqual(DECODERS["2B43"](payload), {"array_2b43": "C3C3C4C4"})

    def test_cell_spread_comes_from_raw_counts(self):
        # avg, min, max
        payload = bytes.fromhex("9C5A9C4A9C6E")
        out = DECODERS["2AF5"](payload)
        self.assertAlmostEqual(out["cell_spread_mv"], (0x9C6E - 0x9C4A) / 10, places=2)
        self.assertLess(out["cell_min_v"], out["cell_avg_v"])
        self.assertLess(out["cell_avg_v"], out["cell_max_v"])


class TestCycleShape(unittest.TestCase):
    def test_each_identifier_is_asked_once_per_cycle(self):
        fake = _Fake()
        record(fake, max_cycles=1, sleeper=lambda s: None)
        for group in GROUPS:
            for did in group.dids:
                with self.subTest(did=did):
                    self.assertEqual(fake.sent.count(f"22{did}"), 1)

    def test_standard_pids_are_asked_after_the_broadcast_header_is_restored(self):
        # Without this the request goes to whichever module the last enhanced
        # group selected, and the vehicle answers NO DATA.
        fake = _Fake()
        record(fake, max_cycles=1, sleeper=lambda s: None)
        self.assertIn("ATSHDB33F1", fake.sent)
        self.assertIn("ATCP18", fake.sent)
        self.assertLess(fake.sent.index("ATSHDB33F1"), fake.sent.index("010D"))
        self.assertLess(fake.sent.index("ATCP18"), fake.sent.index("010D"))

    def test_priority_is_restored_for_the_next_cycle(self):
        fake = _Fake()
        record(fake, max_cycles=2, sleeper=lambda s: None)
        self.assertGreater(fake.sent.count("ATCP14"), 1)

    def test_flow_control_follows_the_header_in_every_group(self):
        # Setting ATFCSM before ATFCSH truncates multi-frame replies, which is
        # how the cell-voltage read lost its second frame on the first run.
        for group in GROUPS:
            with self.subTest(group=group.name):
                order = list(group.address)
                self.assertLess(
                    order.index([c for c in order if c.startswith("ATFCSH")][0]),
                    order.index("ATFCSM1"),
                )

    def test_rows_are_persisted_as_they_are_taken(self):
        # A session ends when the vehicle powers down, which is also when the
        # node can lose power.  Holding rows until then loses the drive.
        seen = []
        fake = _Fake()
        session = record(
            fake, max_cycles=3, sleeper=lambda s: None, row_sink=seen.append
        )
        self.assertEqual(len(seen), 3)
        self.assertEqual(seen, session.rows)

    def test_power_is_derived_from_the_energy_slope(self):
        # 0x5401 is published as charger power but is non-zero at idle on this
        # vehicle and did not scale to a measured AC charge, so power comes
        # from the energy field's slope instead.
        fake = _Fake()
        rising = ["142AF1CB056227AF3B0C\r\r>", "142AF1CB056227AF3B70\r\r>"]

        class _Rising(_Fake):
            def send(self, command, timeout=None):
                if command == "2227AF" and rising:
                    self.sent.append(command)
                    data = rising.pop(0)
                    return Response(command=command, data=data.encode(), elapsed_s=0.01)
                return super().send(command, timeout)

        fake = _Rising()
        clock = {"t": 0.0}

        def tick():
            clock["t"] += 60.0
            return clock["t"]

        session = record(fake, max_cycles=2, sleeper=lambda s: None, clock=tick)
        self.assertIsNone(session.rows[0].get("power_kw"), "no slope from one point")
        self.assertIn("power_kw", session.rows[1])
        self.assertGreater(session.rows[1]["power_kw"], 0, "energy rose, so charging")

    def test_power_is_smoothed_over_a_window_not_consecutive_samples(self):
        # energy_kwh is quantised to 0.01 kWh.  At a ~7 s cycle one quantum is
        # about 5 kW, so a consecutive-sample slope alternated 9.5/4.8 kW while
        # the true rate was a steady 7.8.  The window is what fixes that.
        from hummer_obd.drive import POWER_WINDOW_S, _power_over_window

        rows = []
        # 0.01 kWh steps arriving every 7 s: a real, steady ~5.1 kW.
        for i in range(30):
            rows.append({"elapsed_s": i * 7.0, "energy_kwh": 100.0 + (i // 2) * 0.01})
        latest = rows[-1]
        windowed = _power_over_window(rows[:-1], latest)
        consecutive = (
            (latest["energy_kwh"] - rows[-2]["energy_kwh"])
            / ((latest["elapsed_s"] - rows[-2]["elapsed_s"]) / 3600.0)
        )
        self.assertIsNotNone(windowed)
        # The consecutive-sample figure is either ~0 or ~5 kW; the windowed one
        # sits near the true average and is far less extreme.
        self.assertLess(abs(windowed - 2.57), 1.5)
        self.assertGreater(abs(consecutive - windowed), 1.0)
        self.assertGreater(POWER_WINDOW_S, 30)

    def test_power_is_absent_until_there_is_history(self):
        from hummer_obd.drive import _power_over_window

        self.assertIsNone(_power_over_window([], {"elapsed_s": 0.0, "energy_kwh": 1.0}))

    def test_power_column_is_declared(self):
        self.assertIn("power_kw", COLUMNS)

    def test_bounds_are_honoured(self):
        fake = _Fake()
        self.assertEqual(record(fake, max_cycles=3, sleeper=lambda s: None).cycles, 3)

    def test_every_decoded_column_is_declared(self):
        fake = _Fake()
        session = record(fake, max_cycles=1, sleeper=lambda s: None)
        for key in session.rows[0]:
            with self.subTest(column=key):
                self.assertIn(key, COLUMNS)


class TestAutoMode(unittest.TestCase):
    def test_waking_starts_a_session_and_sleeping_ends_it(self):
        fake = _Fake(volts_sequence=[12.8, 13.9, 12.8, 12.8])
        messages = []
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 4

        run_auto(
            fake, output_dir="/tmp", sleeper=lambda s: None,
            say=messages.append, stop=stop,
        )
        joined = " ".join(messages)
        self.assertIn("awake", joined)

    def test_unreadable_adapter_does_not_transmit_to_the_vehicle(self):
        class _Dead(_Fake):
            def send(self, command, timeout=None):
                self.sent.append(command)
                return Response(command=command, data=b"", elapsed_s=0.0)

        fake = _Dead()
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 2

        run_auto(fake, output_dir="/tmp", sleeper=lambda s: None, stop=stop)
        self.assertEqual(set(fake.sent), {"ATRV"})


class TestWakeThreshold(unittest.TestCase):
    def test_threshold_sits_between_the_measured_bands(self):
        # Asleep measured 12.7-12.9 V, running 13.7-13.9 V.
        self.assertGreater(drive.WAKE_VOLTS, 12.9)
        self.assertLess(drive.WAKE_VOLTS, 13.7)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
