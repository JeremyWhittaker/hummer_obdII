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
from hummer_obd.transport import Response, Transport, TransportError

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

    def test_standard_pids_are_addressed_to_the_module_that_answers_them(self):
        # These used to be broadcast to DB33F1, and a broadcast is answered by
        # whoever speaks first.  Measured over a whole raw transcript: 010D and
        # 01A6 were each answered 545 times and *every* answer came from module
        # 17, while module 28 refused service 01 (7F 01 22) more than 760 times
        # -- faster than module 17 could answer.  The adapter returned the
        # refusal, so speed and odometer landed in 8 of 79 rows on 2026-09-03
        # while every enhanced read landed in all 79.
        fake = _Fake()
        record(fake, max_cycles=1, sleeper=lambda s: None)
        self.assertIn("ATSHDA17F1", fake.sent)
        self.assertIn("ATCP18", fake.sent)
        self.assertNotIn(
            "ATSHDB33F1", fake.sent,
            "the functional broadcast is what let the wrong module answer",
        )
        for setup in ("ATCP18", "ATSHDA17F1"):
            with self.subTest(setup=setup):
                self.assertLess(fake.sent.index(setup), fake.sent.index("010D"))

    def test_the_receive_filter_is_pinned_to_that_module_reply_address(self):
        # Module 17 answers from 18DAF117.  Filtering to it means a module that
        # was never asked cannot be mistaken for one that was.
        fake = _Fake()
        record(fake, max_cycles=1, sleeper=lambda s: None)
        self.assertIn("ATCRA18DAF117", fake.sent)
        self.assertLess(fake.sent.index("ATCRA18DAF117"), fake.sent.index("010D"))
        self.assertNotIn(
            "ATCM00000000", fake.sent,
            "clearing the mask is what allowed every module to be heard",
        )

    def test_the_standard_address_is_the_module_already_read_for_pack_power(self):
        # Not a new address and not a guess: module 17 is the one this node
        # already reads pack voltage and current from.
        pack_power = [g for g in drive.GROUPS if g.name == "pack_power"][0]
        self.assertIn("17", pack_power.address[0])
        self.assertTrue(
            any("17" in command for command in drive.STANDARD_ADDRESS),
            f"standard PIDs should target module 17, got {drive.STANDARD_ADDRESS}",
        )

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


class _Silent(_Fake):
    """An adapter that is reachable but answers nothing.

    This is what a transient RFCOMM glitch looks like from inside the recorder:
    the write succeeds and the read comes back empty.
    """

    def send(self, command, timeout=None):
        self.sent.append(command)
        return Response(command=command, data=b"", elapsed_s=0.0)


class TestSilenceIsNotSleep(unittest.TestCase):
    """Only a measured voltage may end a session.

    ``stop_when`` used to read ``(_volts(...) or 0) < WAKE_VOLTS``, so an
    unanswered ``ATRV`` became 0 V -- below every threshold -- and one Bluetooth
    timeout ended a session that was recording a drive.
    """

    def test_a_silent_adapter_is_not_reported_asleep(self):
        self.assertFalse(drive._asleep(_Silent(), 1.0))

    def test_a_measured_low_voltage_is_reported_asleep(self):
        self.assertTrue(drive._asleep(_Fake(volts_sequence=[12.8]), 1.0))

    def test_a_measured_running_voltage_is_not_reported_asleep(self):
        self.assertFalse(drive._asleep(_Fake(volts_sequence=[13.9]), 1.0))

    def test_the_threshold_is_the_only_thing_that_decides(self):
        # Relative to WAKE_VOLTS rather than a literal: the threshold is
        # evidence-driven and has moved once already.
        edge = drive.WAKE_VOLTS
        for volts, asleep in ((edge - 0.01, True), (edge, False), (edge + 0.01, False)):
            with self.subTest(volts=volts):
                self.assertEqual(
                    drive._asleep(_Fake(volts_sequence=[volts]), 1.0), asleep
                )


class TestSilentAdapterRetriesPromptly(unittest.TestCase):
    """A dropped read must not cost a whole asleep interval of a live drive."""

    def test_first_silences_retry_fast_then_fall_back_to_the_slow_watch(self):
        fake = _Silent()
        slept = []
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > drive.UNANSWERED_RETRIES + 1

        run_auto(
            fake, output_dir="/tmp", sleeper=slept.append, stop=stop,
            asleep_interval_s=300.0,
        )
        # Still the property that makes this safe to run against a parked
        # vehicle: retrying sooner must not mean sending anything more.
        self.assertEqual(set(fake.sent), {"ATRV"})
        self.assertEqual(
            slept[: drive.UNANSWERED_RETRIES],
            [drive.UNANSWERED_INTERVAL_S] * drive.UNANSWERED_RETRIES,
            f"the first {drive.UNANSWERED_RETRIES} silences should retry "
            f"promptly, slept {slept}",
        )
        self.assertEqual(
            slept[drive.UNANSWERED_RETRIES], 300.0,
            f"a persistently silent adapter should fall back to the slow "
            f"watch, slept {slept}",
        )

    def test_prompt_retries_are_shorter_than_the_slow_watch(self):
        self.assertLess(drive.UNANSWERED_INTERVAL_S, 300.0)
        self.assertGreater(drive.UNANSWERED_RETRIES, 0)


class _Droppable(_Fake):
    """A link that can hang up, and that ``reconnect()`` may bring back.

    pyserial does not close the port on an I/O error, so a real hung-up rfcomm
    tty keeps failing every send with no path back on its own.  That is the
    behaviour modelled here.
    """

    def __init__(self, drop_after_sends=0, revives=False, **kwargs):
        super().__init__(**kwargs)
        self.sends = 0
        self.drop_after_sends = drop_after_sends
        self.revives = revives
        self.dead = False
        self.reconnects = 0
        self.sends_after_reconnect = []

    def send(self, command, timeout=None):
        self.sends += 1
        if self.drop_after_sends and self.sends > self.drop_after_sends:
            self.dead = True
        if self.dead:
            self.sent.append(command)
            raise TransportError("read failed")
        if self.reconnects:
            self.sends_after_reconnect.append(command)
        return super().send(command, timeout)

    def reconnect(self, attempt=0):
        self.reconnects += 1
        if self.revives:
            self.dead = False
            self.drop_after_sends = 0


def _bounded_clock(step=1.0):
    """A clock that always advances, so a broken loop cannot run forever."""
    state = {"t": 0.0}

    def tick():
        state["t"] += step
        return state["t"]

    return tick


class TestADeadLinkEndsTheProcess(unittest.TestCase):
    """A link that has gone away must not be recorded as data.

    Every transport failure is caught per group so that one quiet module costs
    only its own columns.  The same handling used to swallow a link that had
    gone entirely: the loop wrote rows carrying nothing but a timestamp for the
    rest of the session while the service stayed "active (running)".
    """

    def test_a_link_that_hangs_up_raises_instead_of_writing_empty_rows(self):
        fake = _Droppable(drop_after_sends=len(SESSION_INIT))
        with self.assertRaises(TransportError) as caught:
            record(fake, interval_s=0, duration_s=100.0, sleeper=lambda s: None,
                   clock=_bounded_clock())
        self.assertIn("decoded nothing", str(caught.exception))

    def test_no_row_is_written_for_a_cycle_that_decoded_nothing(self):
        fake = _Droppable(drop_after_sends=len(SESSION_INIT))
        written = []
        with self.assertRaises(TransportError):
            record(fake, interval_s=0, duration_s=100.0, sleeper=lambda s: None,
                   clock=_bounded_clock(), row_sink=written.append)
        self.assertEqual(
            written, [],
            "a row of nothing but a timestamp is not a sample and must not be "
            f"written, got {written}",
        )

    def test_it_tries_to_reconnect_before_giving_up(self):
        fake = _Droppable(drop_after_sends=len(SESSION_INIT))
        with self.assertRaises(TransportError):
            record(fake, interval_s=0, duration_s=100.0, sleeper=lambda s: None,
                   clock=_bounded_clock())
        self.assertEqual(
            fake.reconnects, drive.DEAD_CYCLES_BEFORE_EXIT - 1,
            "every dead cycle before the last should attempt a reconnect",
        )

    def test_a_link_that_comes_back_keeps_recording(self):
        fake = _Droppable(drop_after_sends=len(SESSION_INIT), revives=True)
        session = record(fake, interval_s=0, max_cycles=2, sleeper=lambda s: None,
                         clock=_bounded_clock())
        self.assertEqual(fake.reconnects, 1)
        self.assertEqual(session.cycles, 2, "recording should resume after a revive")
        self.assertTrue(session.rows)

    def test_a_revive_re_initialises_the_adapter(self):
        # Reopening the device re-establishes the Bluetooth link, which returns
        # the ELM to power-on defaults; skipping the header would leave echo on
        # and no protocol selected, which reads as corrupt data.
        fake = _Droppable(drop_after_sends=len(SESSION_INIT), revives=True)
        record(fake, interval_s=0, max_cycles=1, sleeper=lambda s: None,
               clock=_bounded_clock())
        self.assertEqual(
            fake.sends_after_reconnect[: len(SESSION_INIT)], list(SESSION_INIT),
            "the session header must be re-sent after a reconnect, got "
            f"{fake.sends_after_reconnect[:6]}",
        )

    def test_a_transport_that_cannot_reconnect_says_so(self):
        # reconnect() lives on SerialTransport, not the Transport interface.
        with self.assertRaises(TransportError):
            drive._revive(_Fake(), timeout=1.0, attempt=0)

    def test_one_quiet_module_does_not_look_like_a_dead_link(self):
        # The distinction the whole check rests on: a module that answers
        # nothing costs its own columns, and nothing else.
        class _OneGroupDown(_Fake):
            def send(self, command, timeout=None):
                if command == drive.GROUPS[0].address[0]:
                    self.sent.append(command)
                    raise TransportError("that module is quiet")
                return super().send(command, timeout)

        fake = _OneGroupDown()
        session = record(fake, interval_s=0, max_cycles=2, sleeper=lambda s: None,
                         clock=_bounded_clock())
        self.assertEqual(session.cycles, 2)
        self.assertTrue(session.rows, "a partial cycle is still a sample")
        self.assertTrue(
            any(k not in ("utc", "elapsed_s", "volts") for k in session.rows[0]),
            f"the surviving groups should still have decoded, got {session.rows[0]}",
        )


class TestDrivingBelowTheWakeBand(unittest.TestCase):
    """The failure that lost a real commute on 2026-09-03.

    The truck idled awake for 23 minutes at 13.9 V, which topped up its 12 V
    battery.  The DC-DC then dropped to float and the entire 12.6-mile drive
    happened at 12.9-13.1 V -- under WAKE_VOLTS.  The recorder read that as
    "asleep", ended the session, and slept 300 s at a time while the odometer
    moved 20.3 km.  Voltage cannot answer "is this vehicle awake"; whether its
    modules answer can.
    """

    def test_a_vehicle_that_answers_keeps_recording_below_the_wake_band(self):
        # Every enhanced read succeeds; only the 12 V rail looks asleep.
        fake = _Fake(volts_sequence=[12.9] * 40)
        session = record(fake, interval_s=0, max_cycles=5, sleeper=lambda s: None,
                         clock=_bounded_clock())
        self.assertEqual(
            session.cycles, 5,
            "a truck answering enhanced reads is awake whatever the rail says",
        )
        self.assertEqual(len(session.rows), 5)

    def test_low_voltage_ends_the_session_only_once_answers_have_stopped(self):
        # Now the link is gone *and* the rail is low: that really is asleep,
        # and it should end cleanly rather than raise for a restart.
        fake = _Droppable(drop_after_sends=len(SESSION_INIT), volts_sequence=[])
        fake._replies["ATRV"] = "12.9V\r\r>"

        class _AsleepLink(_Droppable):
            """Nothing on the CAN bus answers, but ATRV still reads low."""

            def send(self, command, timeout=None):
                self.sends += 1
                if command == "ATRV":
                    self.sent.append(command)
                    return Response(command=command, data=b"12.9V\r\r>", elapsed_s=0.01)
                if self.drop_after_sends and self.sends > self.drop_after_sends:
                    self.dead = True
                if self.dead:
                    self.sent.append(command)
                    raise TransportError("no answer")
                return _Fake.send(self, command, timeout)

        asleep = _AsleepLink(drop_after_sends=len(SESSION_INIT))
        session = record(asleep, interval_s=0, duration_s=100.0,
                         sleeper=lambda s: None, clock=_bounded_clock())
        self.assertEqual(
            asleep.reconnects, 0,
            "a sleeping vehicle is not a broken link and must not be reconnected",
        )
        self.assertLess(session.cycles, 3, "it should stop promptly, not grind on")


class TestTheWatchCanGetADeadLinkBack(unittest.TestCase):
    """A link that dies while the vehicle is parked must not strand the watch.

    `record` revives a link that dies mid-session.  Nothing revived one that
    died while parked, so the watch sat on a dead file descriptor forever.
    Observed on 2026-09-03: the vehicle slept, the OBD port lost power, the
    adapter dropped Bluetooth, and the recorder reported "adapter still silent"
    every five minutes against an rfcomm channel showing `closed`.
    """

    def test_a_persistently_silent_adapter_triggers_a_reopen(self):
        fake = _Silent()
        fake.reconnects = 0

        def reconnect(attempt=0):
            fake.reconnects += 1

        fake.reconnect = reconnect
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > drive.UNANSWERED_RETRIES + 2

        run_auto(fake, output_dir="/tmp", sleeper=lambda s: None, stop=stop,
                 asleep_interval_s=300.0)
        self.assertGreater(
            fake.reconnects, 0,
            "the watch must try to reopen a link that has stopped answering",
        )

    def test_prompt_retries_come_before_any_reopen(self):
        # A single dropped read is a glitch, not a dead link; reopening on the
        # first silence would tear down a working link over one timeout.
        fake = _Silent()
        order = []
        fake.reconnect = lambda attempt=0: order.append("reconnect")
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > drive.UNANSWERED_RETRIES + 2

        def sleeper(seconds):
            order.append(f"sleep{seconds}")

        run_auto(fake, output_dir="/tmp", sleeper=sleeper, stop=stop,
                 asleep_interval_s=300.0)
        first_reconnect = order.index("reconnect")
        # Prompt retries recur legitimately after a successful reopen, because
        # the counter resets -- so what matters is how many came BEFORE the
        # first one, not where the last one landed.
        before = [o for o in order[:first_reconnect]
                  if o == f"sleep{drive.UNANSWERED_INTERVAL_S}"]
        self.assertGreaterEqual(
            len(before), drive.UNANSWERED_RETRIES,
            f"a link should not be torn down before {drive.UNANSWERED_RETRIES} "
            f"prompt retries, got {order[:first_reconnect + 1]}",
        )

    def test_it_still_sends_only_atrv_while_doing_so(self):
        # The property that makes this safe to leave enabled against a parked
        # vehicle: reopening must not put anything new on the CAN bus.
        fake = _Silent()
        fake.reconnect = lambda attempt=0: None
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > drive.UNANSWERED_RETRIES + 2

        run_auto(fake, output_dir="/tmp", sleeper=lambda s: None, stop=stop)
        # Reopening re-sends the session header, which is adapter configuration
        # -- ATZ, ATE0, ATSP7 and the rest.  None of it reaches the CAN bus.
        # The property is not "only ATRV is ever sent" but "nothing that
        # reaches the vehicle is", so the assertion is that every command is an
        # adapter command and no OBD service request appears.
        non_adapter = [c for c in fake.sent if not c.startswith("AT")]
        self.assertEqual(
            non_adapter, [],
            f"a parked vehicle must see no service request, saw {non_adapter}",
        )
        self.assertIn("ATRV", fake.sent)

    def test_a_reopen_that_fails_is_survived(self):
        fake = _Silent()

        def failing(attempt=0):
            raise TransportError("adapter has no power")

        fake.reconnect = failing
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > drive.UNANSWERED_RETRIES + 3

        run_auto(fake, output_dir="/tmp", sleeper=lambda s: None, stop=stop)
        non_adapter = [c for c in fake.sent if not c.startswith("AT")]
        self.assertEqual(non_adapter, [])


class TestPerGroupPriority(unittest.TestCase):
    """There is no universal CAN priority, and assuming one hid a module.

    Established by asking every module at both: 17, 1D, 1E and CB answer at
    0x14 and 0x18; module 28 answers at 0x14 and returns 7F 22 11
    (serviceNotSupported) at 0x18; module 40 answers only at 0x18 and returns
    nothing at all at 0x14.  28 and 40 cannot share one global priority, which
    is why module 40 sat unreachable while the recorder sent 0x14 to everything.
    """

    def test_each_group_carries_its_own_priority(self):
        for group in drive.GROUPS:
            with self.subTest(group=group.name):
                self.assertIn(group.priority, ("ATCP14", "ATCP18"))

    def test_the_body_module_uses_the_priority_it_answers_at(self):
        body = [g for g in drive.GROUPS if g.ecu == "40"][0]
        self.assertEqual(body.priority, "ATCP18")

    def test_the_brake_controller_keeps_the_one_it_answers_at(self):
        # 28 returns serviceNotSupported at 0x18; moving it would lose wheel
        # speeds, brake pressure, steering and both acceleration axes.
        chassis = [g for g in drive.GROUPS if g.ecu == "28"][0]
        self.assertEqual(chassis.priority, "ATCP14")

    def test_each_group_priority_is_sent_before_its_header(self):
        fake = _Fake()
        record(fake, max_cycles=1, sleeper=lambda s: None)
        for group in drive.GROUPS:
            with self.subTest(group=group.name):
                header = group.address[0]
                self.assertIn(group.priority, fake.sent)
                # The priority must precede the header it applies to.
                self.assertLess(
                    fake.sent.index(group.priority), fake.sent.index(header),
                    f"{group.name}'s priority must be sent before its header",
                )

    def test_both_priorities_actually_go_out_in_one_cycle(self):
        fake = _Fake()
        record(fake, max_cycles=1, sleeper=lambda s: None)
        self.assertIn("ATCP14", fake.sent)
        self.assertIn("ATCP18", fake.sent)

    def test_every_group_command_passes_the_gate(self):
        from hummer_obd.safety import validate_command, validate_enhanced_command
        for group in drive.GROUPS:
            with self.subTest(group=group.name):
                validate_command(group.priority)
                for command in group.address:
                    validate_command(command)
                for did in group.dids:
                    validate_enhanced_command("22" + did)


class TestBodyModuleColumns(unittest.TestCase):
    """Module 40's nine identifiers, captured raw."""

    def test_all_nine_have_columns(self):
        for column in ("evse_current_raw", "group_v1_raw", "group_v2_raw",
                       "group_v3_raw", "hv_temp_raw", "batt_temp_a_raw",
                       "batt_temp_b_raw", "coolant_1_raw", "coolant_2_raw"):
            with self.subTest(column=column):
                self.assertIn(column, drive.COLUMNS)

    def test_they_are_carried_as_text_not_parsed_as_numbers(self):
        # The mistake that made cell_extra_raw read as never answered, twice.
        from hummer_obd.analyze import _TEXT_COLUMNS
        for column in ("evse_current_raw", "group_v1_raw", "hv_temp_raw",
                       "coolant_1_raw", "coolant_2_raw"):
            with self.subTest(column=column):
                self.assertIn(column, _TEXT_COLUMNS)

    def test_no_scaling_is_claimed_for_any_of_them(self):
        # 416C read 2589 then 2593 a minute apart, 416D and 416E returned
        # identical values, and the vehicle was parked and unplugged.
        source = open(drive.__file__, encoding="utf-8").read()
        for claim in ('"evse_current_a"', '"group_v1_v"', '"hv_temp_c"',
                      '"coolant_1_c"'):
            with self.subTest(claim=claim):
                self.assertNotIn(claim, source)

    def test_a_real_reply_decodes_to_its_raw_payload(self):
        self.assertEqual(drive.DECODERS["434F"](bytes.fromhex("64")),
                         {"hv_temp_raw": "64"})
        self.assertEqual(drive.DECODERS["416C"](bytes.fromhex("0A21")),
                         {"group_v1_raw": "0A21"})


class TestEveryProvenIdentifierIsCaptured(unittest.TestCase):
    """Proving an identifier answers and then not recording it wastes it.

    The only way to learn what a field means is to watch it across states it
    has never been seen in.  Four identifiers were proven at module CB on
    2026-09-03 and then left out of the recorder, so each had been seen in
    exactly one state -- warm, parked, just driven -- and could never be
    decoded from that.
    """

    #: Everything module CB has been shown to answer on this vehicle.
    PROVEN_AT_CB = frozenset({
        "27C6", "27AF", "27C7", "27C0", "0046", "5401", "2AF5", "2B43",
        "2AF1", "27BF", "27BB", "27B5", "2709",
    })

    def test_nothing_proven_at_cb_is_left_uncaptured(self):
        recorded = {d for g in drive.GROUPS for d in g.dids}
        missing = sorted(self.PROVEN_AT_CB - recorded)
        self.assertEqual(
            missing, [],
            f"proven to answer and never recorded, so never decodable: {missing}",
        )

    def test_the_four_added_have_columns_and_are_read_as_text(self):
        from hummer_obd.analyze import _TEXT_COLUMNS
        for column in ("regen_field_raw", "thermal_energy_raw",
                       "thermal_distance_raw", "compressor_temp_raw"):
            with self.subTest(column=column):
                self.assertIn(column, drive.COLUMNS)
                self.assertIn(column, _TEXT_COLUMNS)

    def test_no_scaling_is_claimed_for_them(self):
        source = open(drive.__file__, encoding="utf-8").read()
        for claim in ('"regen_kwh"', '"thermal_energy_kwh"',
                      '"thermal_distance_mi"', '"compressor_temp_c"'):
            with self.subTest(claim=claim):
                self.assertNotIn(claim, source)


class TestWakeThreshold(unittest.TestCase):
    def test_threshold_sits_between_the_measured_bands(self):
        # Asleep measured 12.7-12.9 V, running 13.7-13.9 V.
        self.assertGreater(drive.WAKE_VOLTS, 12.9)
        self.assertLess(drive.WAKE_VOLTS, 13.7)

    def test_the_threshold_is_below_every_voltage_measured_while_driving(self):
        # The ATRV probes taken across the drive lost on 2026-09-03.  A
        # threshold above any of these cannot detect this vehicle driving,
        # which is exactly why that drive recorded nothing: the old 13.2 sat
        # above all three.
        for driving in (13.1, 13.1, 13.0):
            with self.subTest(driving=driving):
                self.assertLess(
                    drive.WAKE_VOLTS, driving,
                    "a vehicle measured driving at this voltage must read as awake",
                )

    def test_the_threshold_is_above_every_voltage_measured_asleep(self):
        # The other half of the constraint: a parked vehicle must never be
        # polled on the CAN bus.
        for asleep in (12.7, 12.9):
            with self.subTest(asleep=asleep):
                self.assertGreater(drive.WAKE_VOLTS, asleep)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
