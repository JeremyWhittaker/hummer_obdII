"""End-to-end acceptance tests for the supervised service-22 scanner.

These tests deliberately exercise the real serial transport and append-only raw
log through a PTY.  The simulator represents the adapter and vehicle; no test
opens a vehicle device or relaxes either safety gate.
"""

from __future__ import annotations

import json
import io
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from elm_simulator import ElmSimulator
from hummer_obd import scan
from hummer_obd.rawlog import decode_record, iter_records
from hummer_obd.safety import validate_command, validate_scan_command


MODULE = "17"
PRIORITY = "14"
REPLY_HEADER = "142AF117"


def _positive(did: int, payload: str = "AA") -> str:
    data = f"62{did:04X}{payload}"
    return f"{REPLY_HEADER}{len(bytes.fromhex(data)):02X}{data}"


def _negative(code: int) -> str:
    return f"{REPLY_HEADER}037F22{code:02X}"


class ScanElm(ElmSimulator):
    """GM 29-bit scanner target with programmable guard and DID replies."""

    def __init__(
        self,
        *,
        did_answers: dict[int, str] | None = None,
        speed_answers: list[str] | None = None,
        dirty_service: str | None = None,
        dirty_round: int | None = None,
    ) -> None:
        super().__init__()
        self.did_answers = did_answers or {}
        self.speed_answers = list(speed_answers or [])
        self.dirty_service = dirty_service
        self.dirty_round = dirty_round
        self.speed_reads = 0
        self.dtc_round = 0
        self.answers: list[tuple[str, str]] = []

    def answer(self, command: str) -> str:
        if command == "ATZ":
            body = super().answer(command)
        elif command.startswith(("AT", "ST")):
            body = "OK"
        elif command == "010D":
            if self.speed_answers:
                index = min(self.speed_reads, len(self.speed_answers) - 1)
                body = self.speed_answers[index]
            else:
                body = "18DAF11703410D00"
            self.speed_reads += 1
        elif command in ("03", "07", "0A"):
            positive = {"03": "43", "07": "47", "0A": "4A"}[command]
            if command == self.dirty_service and self.dtc_round == self.dirty_round:
                body = f"18DAF14504{positive}010101"
            else:
                body = f"18DAF14502{positive}00"
            if command == "0A":
                self.dtc_round += 1
        elif len(command) == 6 and command.startswith("22"):
            did = int(command[2:], 16)
            body = self.did_answers.get(did, _positive(did))
        else:
            body = super().answer(command)
        self.answers.append((command, body))
        return body


class SilentAtzElm(ScanElm):
    """Accept the first command but emit no bytes, as the pilot adapter did."""

    def answer(self, command: str) -> str:
        if command == "ATZ":
            self.answers.append((command, ""))
            # ElmSimulator calls answer() before writing its reply.  Waiting on
            # its stop event therefore leaves the PTY completely silent until
            # SerialTransport reaches its own bounded timeout; tearDown then
            # releases this daemon thread without a late prompt reaching it.
            self._stop.wait(1.0)
            return ""
        return super().answer(command)


class LateBannerElm(ScanElm):
    """Answer ATZ, then write *late* unprompted, as the adapter did live.

    Measured 2026-09-16: the first reply after the tty opened was an identity
    banner with no echo, and a second banner arrived 0.83 s later.  Read as
    the reply to ATE0, it aborted the run.
    """

    def __init__(self, late: bytes, delay_s: float = 0.1, **kwargs) -> None:
        super().__init__(**kwargs)
        self.late = late
        self.delay_s = delay_s

    def answer(self, command: str) -> str:
        if command == "ATZ":
            timer = threading.Timer(self.delay_s, self._write_late)
            timer.daemon = True
            timer.start()
            self.answers.append((command, "ELM327 v1.4b"))
            return "ELM327 v1.4b"
        return super().answer(command)

    def _write_late(self) -> None:
        try:
            os.write(self.master, self.late)
        except OSError:
            pass


class ScanAcceptanceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.old_cwd = Path.cwd()
        os.chdir(self.root)

    def tearDown(self) -> None:
        os.chdir(self.old_cwd)
        self._tmp.cleanup()

    @contextmanager
    def simulator(self, **kwargs):
        sim = ScanElm(**kwargs).start()
        try:
            yield sim
        finally:
            sim.stop()

    def config(self, device: str, **changes) -> scan.ScanConfig:
        values = {
            "module": MODULE,
            "priority": PRIORITY,
            "start": 0x2400,
            "end": 0x2400,
            "device": device,
            "delay_ms": 50.0,
            "timeout": 0.5,
            "chunk_size": 16,
            "output_dir": Path("evidence/scans"),
            "startup_timeout": 0.5,
            "settle_s": 0.05,
        }
        values.update(changes)
        return scan.ScanConfig(**values)

    def run_scan(self, sim: ScanElm, **changes) -> dict:
        return scan.run_scan(
            self.config(sim.device, **changes), say=lambda _message: None,
            sleeper=lambda _delay: None,
        )

    def run_aborted(self, sim: ScanElm, **changes) -> dict:
        with self.assertRaises(scan.ScanAborted):
            self.run_scan(sim, **changes)
        files = self.state_files()
        self.assertEqual(len(files), 1)
        state = json.loads(files[0].read_text())
        self.assertEqual(state["status"], "aborted")
        return state

    def state_files(self) -> list[Path]:
        return list((Path.cwd() / "evidence/scans").glob("*.state.json"))

    def raw_files(self) -> list[Path]:
        return list((Path.cwd() / "evidence/scans").glob("*.raw.jsonl"))


class TestScanConfiguration(ScanAcceptanceCase):
    def test_defaults_and_frozen_dataclass(self):
        config = scan.ScanConfig()
        self.assertEqual(config.module, "17")
        self.assertEqual(config.priority, "14")
        self.assertEqual((config.start, config.end), (0x2400, 0x24FF))
        self.assertEqual(config.output_dir, Path("evidence/scans"))
        with self.assertRaises((AttributeError, TypeError)):
            config.start = 0  # type: ignore[misc]

    def test_invalid_values_are_refused_at_construction(self):
        invalid = (
            {"module": "10"}, {"module": "17;04"}, {"priority": "13"},
            {"start": -1}, {"end": 0x10000},
            {"start": 0x2401, "end": 0x2400},
            {"delay_ms": 49.999}, {"timeout": 0}, {"timeout": 301},
            {"chunk_size": 0}, {"chunk_size": 33},
            {"startup_timeout": 0.05}, {"startup_timeout": 31},
            {"startup_timeout": float("nan")},
            {"settle_s": 0.01}, {"settle_s": 6}, {"settle_s": float("inf")},
        )
        for changes in invalid:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    scan.ScanConfig(**changes)

    def test_dry_run_opens_nothing_and_creates_nothing(self):
        output = io.StringIO()
        with mock.patch.object(scan, "ScanTransport") as serial:
            with redirect_stdout(output), redirect_stderr(output):
                rc = scan.main([
                    "--module", "17", "--start", "0x2400", "--end", "0x2401",
                    "--device", "/definitely/not/a/tty",
                ])
        self.assertEqual(rc, 0)
        serial.assert_not_called()
        self.assertFalse((self.root / "evidence").exists())
        self.assertIn("DRY RUN", output.getvalue())

    def test_cli_default_priority_depends_on_module(self):
        for module, priority in (("17", "14"), ("40", "18"), ("45", "18")):
            output = io.StringIO()
            with self.subTest(module=module):
                with redirect_stdout(output), redirect_stderr(output):
                    rc = scan.main(["--module", module])
                self.assertEqual(rc, 0)
                self.assertIn(priority, output.getvalue())

    def test_output_must_resolve_below_cwd_evidence_scans(self):
        outside = self.root / "outside"
        outside.mkdir()
        allowed = self.root / "evidence/scans"
        allowed.mkdir(parents=True)
        (allowed / "escape").symlink_to(outside, target_is_directory=True)
        for output in (self.root / "elsewhere", allowed / "escape"):
            with self.subTest(output=output):
                with mock.patch.object(scan, "ScanTransport") as serial:
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        rc = scan.main([
                            "--confirm", "--output-dir", str(output),
                            "--device", "/definitely/not/a/tty",
                        ])
                self.assertEqual(rc, 2)
                serial.assert_not_called()

    def test_cli_range_refusal_opens_no_serial(self):
        with mock.patch.object(scan, "ScanTransport") as serial:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                rc = scan.main([
                    "--confirm", "--start", "2401", "--end", "2400",
                    "--device", "/definitely/not/a/tty",
                ])
        self.assertEqual(rc, 2)
        serial.assert_not_called()


class TestWireSafetyAndLogging(ScanAcceptanceCase):
    def test_silent_adapter_at_atz_aborts_without_follow_on_traffic(self):
        sim = SilentAtzElm().start()
        try:
            state = self.run_aborted(sim, timeout=0.1)
        finally:
            sim.stop()

        self.assertEqual(sim.received, ["ATZ"])
        self.assertEqual(state["next_did"], 0x2400)
        self.assertIsNone(state["last_did"])
        self.assertEqual(state["completed_reads"], 0)
        self.assertNotIn("010D", sim.received)
        self.assertFalse(any(c in {"03", "07", "0A"} for c in sim.received))
        self.assertFalse(any(c.startswith("22") for c in sim.received))

        records = list(iter_records(self.raw_files()[0]))
        io = [record for record in records if record.get("kind") == "io"]
        self.assertEqual(
            [decode_record(record) for record in io if record["dir"] == "tx"],
            [b"ATZ\r"],
        )
        self.assertEqual(
            [decode_record(record) for record in io if record["dir"] == "rx"],
            [b""],
        )

    def test_wire_contains_only_adapter_guards_and_selected_service_22(self):
        with self.simulator() as sim:
            state = self.run_scan(sim, start=0x2400, end=0x2402, chunk_size=2)
        self.assertEqual(state["status"], "complete")
        self.assertNotIn("10", [command[:2] for command in sim.received])
        self.assertNotIn("27", [command[:2] for command in sim.received])
        for command in sim.received:
            with self.subTest(command=command):
                if command.startswith(("AT", "ST")):
                    validate_command(command)
                elif command in ("010D", "03", "07", "0A"):
                    validate_command(command)
                else:
                    self.assertRegex(command, r"^22[0-9A-F]{4}$")
                    validate_scan_command(command)
        self.assertEqual(
            [c for c in sim.received if c.startswith("22")],
            ["222400", "222401", "222402"],
        )
        vehicle = [c for c in sim.received if not c.startswith(("AT", "ST"))]
        first_did = vehicle.index("222400")
        self.assertLess(vehicle.index("03"), first_did)
        for index, command in enumerate(vehicle):
            if command.startswith("22"):
                self.assertEqual(vehicle[index - 1], "010D")
        for mode in ("03", "07", "0A"):
            self.assertEqual(vehicle.count(mode), 3)  # before, chunk, final

    def test_state_and_raw_log_have_exact_required_names(self):
        # Abort at the first parked check: path naming is independent of scan
        # completion, and there is no reason to issue 256 DID requests here.
        with self.simulator(speed_answers=["18DAF11703410D01"]) as sim:
            self.run_aborted(sim, end=0x24FF)
        expected = self.root / "evidence/scans/module-17-priority-14-2400-24FF"
        self.assertTrue(expected.with_suffix(".state.json").is_file())
        self.assertTrue(expected.with_suffix(".raw.jsonl").is_file())

    def test_raw_log_preserves_every_tx_and_rx_byte_exactly(self):
        with self.simulator() as sim:
            self.run_scan(sim)
        records = list(iter_records(self.raw_files()[0]))
        io = [record for record in records if record.get("kind") == "io"]
        tx = [decode_record(record) for record in io if record["dir"] == "tx"]
        settle = [decode_record(record) for record in io
                  if record["dir"] == "rx" and record.get("note") == "post-reset settle"]
        rx = [decode_record(record) for record in io
              if record["dir"] == "rx" and record.get("note") != "post-reset settle"]
        self.assertEqual(tx, [(command + "\r").encode("ascii") for command in sim.received])
        # The receive-only settle window is logged too, even when it is silent.
        self.assertEqual(settle, [b""])
        self.assertEqual(rx, [(body + "\r\r>").encode("ascii") for _, body in sim.answers])

    def test_multiframe_positive_reply_is_accepted(self):
        multiframe = (
            "142AF117100A622400010203\r"
            "142AF1172104050607"
        )
        with self.simulator(did_answers={0x2400: multiframe}) as sim:
            state = self.run_scan(sim)
        self.assertEqual(state["status"], "complete")
        persisted = json.loads(self.state_files()[0].read_text())
        self.assertEqual(persisted["next_did"], 0x2401)
        events = [
            record["payload"] for record in iter_records(self.raw_files()[0])
            if record.get("event") == "did_result"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload_hex"], "01020304050607")


class TestParkedAndDtcGuards(ScanAcceptanceCase):
    def test_nonzero_speed_at_baseline_aborts_before_any_did(self):
        moving = "18DAF11703410D01"
        with self.simulator(speed_answers=[moving]) as sim:
            self.run_aborted(sim)
        self.assertEqual([c for c in sim.received if c.startswith("22")], [])

    def test_nonzero_speed_during_scan_aborts_before_next_did(self):
        stopped = "18DAF11703410D00"
        moving = "18DAF11703410D05"
        with self.simulator(speed_answers=[stopped, stopped, moving]) as sim:
            state = self.run_aborted(sim, end=0x2402)
        self.assertEqual([c for c in sim.received if c.startswith("22")], ["222400"])
        self.assertEqual(state["next_did"], 0x2401)

    def test_each_dtc_kind_stops_at_initial_during_and_final_bracket(self):
        # Round 0 is the initial bracket.  With chunk size one, round 1 is a
        # during-scan bracket.  With a larger chunk, round 1 is the final one.
        cases = []
        for service in ("03", "07", "0A"):
            cases.extend((
                (service, 0, 0x2401, 16, 0),
                (service, 1, 0x2402, 1, 1),
                (service, 1, 0x2400, 16, 1),
            ))
        for service, round_, end, chunk_size, did_count in cases:
            with self.subTest(service=service, round=round_, end=end, chunk=chunk_size):
                # Each subtest needs its own directory because state is
                # intentionally exclusive unless --resume is supplied.
                with tempfile.TemporaryDirectory() as child:
                    os.chdir(child)
                    try:
                        with self.simulator(
                            dirty_service=service, dirty_round=round_
                        ) as sim:
                            self.run_aborted(sim, end=end, chunk_size=chunk_size)
                        self.assertEqual(
                            len([c for c in sim.received if c.startswith("22")]),
                            did_count,
                        )
                    finally:
                        os.chdir(self.root)

    def test_no_data_or_malformed_guard_reply_fails_closed(self):
        cases = ("NO DATA", "18DAF11703410D", "18DAF19903410D00")
        for answer in cases:
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as child:
                os.chdir(child)
                try:
                    with self.simulator(speed_answers=[answer]) as sim:
                        self.run_aborted(sim)
                    self.assertFalse(any(c.startswith("22") for c in sim.received))
                finally:
                    os.chdir(self.root)


class TestDidReplyPolicy(ScanAcceptanceCase):
    def test_request_out_of_range_advances_and_continues(self):
        with self.simulator(did_answers={0x2400: _negative(0x31)}) as sim:
            state = self.run_scan(sim, end=0x2401)
        self.assertEqual(state["status"], "complete")
        self.assertEqual([c for c in sim.received if c.startswith("22")], ["222400", "222401"])
        self.assertEqual(state["next_did"], 0x2402)

    def test_service_not_supported_stops_without_trying_another_did(self):
        with self.simulator(did_answers={0x2400: _negative(0x11)}) as sim:
            state = self.run_scan(sim, end=0x2402)
        self.assertEqual(state["status"], "unsupported")
        self.assertEqual([c for c in sim.received if c.startswith("22")], ["222400"])
        self.assertEqual(state["next_did"], 0x2401)

    def test_allowed_terminal_negative_responses_advance(self):
        for code in (0x22, 0x33, 0x34, 0x7E, 0x7F):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as child:
                os.chdir(child)
                try:
                    with self.simulator(did_answers={0x2400: _negative(code)}) as sim:
                        state = self.run_scan(sim)
                    self.assertEqual(state["status"], "complete")
                    self.assertEqual(state["next_did"], 0x2401)
                finally:
                    os.chdir(self.root)

    def test_pending_without_terminal_reply_aborts_and_does_not_advance(self):
        with self.simulator(did_answers={0x2400: _negative(0x78)}) as sim:
            state = self.run_aborted(sim)
        self.assertEqual(state["next_did"], 0x2400)

    def test_pending_followed_by_positive_in_same_transaction_is_accepted(self):
        reply = _negative(0x78) + "\r" + _positive(0x2400, "0102")
        with self.simulator(did_answers={0x2400: reply}) as sim:
            state = self.run_scan(sim)
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["next_did"], 0x2401)

    def test_more_than_three_pending_frames_aborts(self):
        reply = "\r".join([_negative(0x78)] * 4 + [_positive(0x2400)])
        with self.simulator(did_answers={0x2400: reply}) as sim:
            state = self.run_aborted(sim)
        self.assertEqual(state["next_did"], 0x2400)

    def test_first_busy_repeat_aborts(self):
        with self.simulator(did_answers={0x2400: _negative(0x21)}) as sim:
            self.run_aborted(sim)

    def test_silence_malformed_wrong_header_wrong_echo_and_unexpected_nrc_abort(self):
        bad = {
            "silence": "NO DATA",
            "malformed": "142AF11707622400AA",
            "wrong_header": "142AF11D04622400AA",
            "wrong_echo": _positive(0x2401),
            "unexpected_nrc": _negative(0x13),
        }
        for name, answer in bad.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as child:
                os.chdir(child)
                try:
                    with self.simulator(did_answers={0x2400: answer}) as sim:
                        state = self.run_aborted(sim)
                    self.assertEqual(state["next_did"], 0x2400)
                finally:
                    os.chdir(self.root)


class TestResume(ScanAcceptanceCase):
    def test_unsupported_module_stays_stopped_if_postflight_fails_then_resumes(self):
        with self.simulator(did_answers={0x2400: _negative(0x11)},
                            dirty_service="03", dirty_round=1) as first:
            state = self.run_aborted(first, end=0x2402)
        self.assertEqual(state["service_unsupported_at"], 0x2400)
        self.assertEqual(state["next_did"], 0x2401)
        with self.simulator() as second:
            resumed = scan.run_scan(self.config(second.device, end=0x2402), resume=True,
                                    say=lambda _: None, sleeper=lambda _: None)
        self.assertEqual(resumed["status"], "unsupported")
        self.assertFalse(any(c.startswith("22") for c in second.received))
        self.assertEqual([c for c in second.received if c in ("03", "07", "0A")],
                         ["03", "07", "0A"] * 2)

    def test_final_dtc_failure_does_not_mark_range_complete(self):
        with self.simulator(dirty_service="03", dirty_round=1) as first:
            state = self.run_aborted(first)
        self.assertEqual(state["next_did"], 0x2401)
        self.assertIn("not completed", state["postflight"])
        with self.simulator() as second:
            resumed = scan.run_scan(self.config(second.device), resume=True,
                                    say=lambda _: None, sleeper=lambda _: None)
        self.assertEqual(resumed["status"], "complete")
        self.assertFalse(any(c.startswith("22") for c in second.received))
        self.assertNotIn("postflight", resumed)

    def test_resume_starts_at_next_did_and_retries_prior_aborted_did(self):
        bad = "142AF11707622401AA"
        with self.simulator(did_answers={0x2401: bad}) as first:
            state = self.run_aborted(first, end=0x2402)
        self.assertEqual(state["next_did"], 0x2401)

        with self.simulator() as second:
            resumed = scan.run_scan(
                self.config(second.device, end=0x2402), resume=True,
                say=lambda _message: None, sleeper=lambda _delay: None,
            )
        self.assertEqual(resumed["status"], "complete")
        self.assertEqual(
            [c for c in second.received if c.startswith("22")],
            ["222401", "222402"],
        )

    def test_existing_state_requires_resume_and_opens_no_serial(self):
        with self.simulator() as sim:
            self.run_scan(sim)
        with mock.patch.object(scan, "ScanTransport") as serial:
            with self.assertRaises(ValueError):
                scan.run_scan(self.config("/not/opened"), say=lambda _m: None)
        serial.assert_not_called()

    def test_resume_config_mismatch_is_refused_before_serial_open(self):
        with self.simulator() as sim:
            self.run_scan(sim)
        state_path = self.state_files()[0]
        state = json.loads(state_path.read_text())
        state["module"] = "1D"
        state_path.write_text(json.dumps(state))
        with mock.patch.object(scan, "ScanTransport") as serial:
            with self.assertRaises(ValueError):
                scan.run_scan(
                    self.config("/not/opened"), resume=True,
                    say=lambda _m: None,
                )
        serial.assert_not_called()


class TestStartupSynchronization(ScanAcceptanceCase):
    """The measured live failure, reproduced, and the strictness kept around it."""

    BANNER = b"\r\rELM327 v1.4b\r\r>"

    def run_late(self, late: bytes, **changes):
        sim = LateBannerElm(late).start()
        try:
            return sim, self.run_scan(sim, settle_s=0.4, **changes)
        finally:
            sim.stop()

    def rx_notes(self):
        records = list(iter_records(self.raw_files()[0]))
        return [(r.get("note"), decode_record(r)) for r in records
                if r.get("kind") == "io" and r["dir"] == "rx"]

    def test_late_duplicate_banner_is_absorbed_and_logged(self):
        sim, state = self.run_late(self.BANNER)
        self.assertEqual(state["status"], "complete")
        self.assertEqual(sim.received[:2], ["ATZ", "ATE0"])
        self.assertIn(("post-reset settle", self.BANNER), self.rx_notes())

    def test_late_banner_with_reset_echo_is_absorbed(self):
        late = b"ATZ\r\r\rELM327 v1.4b\r\r>"
        _, state = self.run_late(late)
        self.assertEqual(state["status"], "complete")
        self.assertIn(("post-reset settle", late), self.rx_notes())

    def test_quiet_line_after_reset_proceeds_normally(self):
        with self.simulator() as sim:
            state = self.run_scan(sim)
        self.assertEqual(state["status"], "complete")
        self.assertIn(("post-reset settle", b""), self.rx_notes())

    def assert_stopped_after_reset(self, sim, state):
        self.assertEqual(sim.received, ["ATZ"])
        self.assertEqual(state["completed_reads"], 0)
        self.assertIn("not completed", state["postflight"])

    def test_anything_but_a_banner_after_reset_aborts_before_any_traffic(self):
        for late in (b"?\r\r>", b"OK\r\r>", b"BUS ERROR\r\r>", b"ATZ\r",
                     self.BANNER + self.BANNER, b"\r\rELM327 v1.4b\r\r"):
            with self.subTest(late=late):
                for path in (Path.cwd() / "evidence").rglob("*"):
                    if path.is_file():
                        path.unlink()
                sim = LateBannerElm(late).start()
                try:
                    state = self.run_aborted(sim, settle_s=0.4)
                finally:
                    sim.stop()
                self.assert_stopped_after_reset(sim, state)

    def test_reset_reply_must_still_be_an_identity_banner(self):
        class OkForReset(ScanElm):
            def answer(self, command):
                return "OK" if command == "ATZ" else super().answer(command)

        sim = OkForReset().start()
        try:
            state = self.run_aborted(sim)
        finally:
            sim.stop()
        self.assertEqual(sim.received, ["ATZ"])
        self.assertIn("ATZ", state["reason"])

    def test_reset_uses_its_own_budget_not_the_per_command_timeout(self):
        # A late banner that would miss a 0.5 s budget completes within 2 s.
        class SlowReset(ScanElm):
            def answer(self, command):
                if command == "ATZ":
                    self._stop.wait(0.8)
                return super().answer(command)

        sim = SlowReset().start()
        try:
            state = self.run_scan(sim, timeout=0.5, startup_timeout=2.0)
        finally:
            sim.stop()
        self.assertEqual(state["status"], "complete")


class ChangingElm(ScanElm):
    """One identifier's payload flips after *flip_after* reads of it."""

    def __init__(self, did: int, flip_after: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self.did, self.flip_after, self.reads = did, flip_after, 0

    def answer(self, command: str) -> str:
        if command == f"22{self.did:04X}":
            self.reads += 1
            payload = "AA" if self.reads <= self.flip_after else "BB"
            self.did_answers[self.did] = _positive(self.did, payload)
        return super().answer(command)


class TestWatch(ScanAcceptanceCase):
    DIDS = (0x2414, 0x2429, 0x242B)

    def watch(self, sim, dids=DIDS, **kwargs):
        kwargs.setdefault("passes", 3)
        kwargs.setdefault("interval_s", 0)
        config = self.config(sim.device)
        return scan.run_watch(config, dids, say=lambda _m: None,
                              sleeper=lambda _d: None, **kwargs)

    def test_watch_reports_which_identifier_changed_and_when(self):
        sim = ChangingElm(0x2429, flip_after=1).start()
        try:
            summary = self.watch(sim, label="driver-door")
        finally:
            sim.stop()
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["reads"], 9)
        self.assertEqual(summary["changes"], {"2429": [2]})

    def test_watch_sends_only_guards_adapter_and_listed_reads(self):
        with self.simulator() as sim:
            self.watch(sim, passes=2)
        reads = [c for c in sim.received if c.startswith("22")]
        self.assertEqual(reads, [f"22{d:04X}" for d in self.DIDS] * 2)
        for command in sim.received:
            with self.subTest(command=command):
                self.assertTrue(command.startswith("AT") or command in scan.GUARD_READS
                                or command in reads, command)
        # speed before every read, plus before DTCs and postflight
        self.assertEqual(sim.received.count("010D"), len(self.DIDS) * 2 + 2)

    def test_watch_stops_on_motion_without_further_traffic(self):
        speeds = ["18DAF11703410D00"] * 3 + ["18DAF11703410D05"]
        sim = ScanElm(speed_answers=speeds).start()
        try:
            with self.assertRaises(scan.ScanAborted):
                self.watch(sim)
        finally:
            sim.stop()
        self.assertEqual(sim.received[-1], "010D")
        records = list(iter_records(next((Path.cwd() / "evidence/scans").glob("watch-*.raw.jsonl"))))
        aborted = [r for r in records if r.get("event") == "watch_aborted"]
        self.assertEqual(len(aborted), 1)
        self.assertIn("moving", aborted[0]["payload"]["reason"])

    def test_invalid_watch_requests_are_refused_before_any_io(self):
        with self.simulator() as sim:
            for kwargs in ({"dids": ()}, {"dids": (0x2429, 0x2429)},
                           {"dids": tuple(range(65))}, {"dids": (0x10000,)},
                           {"passes": 0}, {"passes": 121}, {"interval_s": -1},
                           {"label": "../x"}, {"label": ""}):
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(ValueError):
                        self.watch(sim, **kwargs)
            self.assertEqual(sim.received, [])

    def test_watch_cli_dry_run_and_resume_refusal(self):
        output = io.StringIO()
        with mock.patch.object(scan, "ScanTransport") as serial:
            with redirect_stdout(output), redirect_stderr(output):
                rc = scan.main(["--module", "40", "--watch", "40E5,4127", "--label", "door"])
        self.assertEqual(rc, 0)
        serial.assert_not_called()
        self.assertFalse((self.root / "evidence").exists())
        self.assertIn("40E5 4127", output.getvalue())
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                scan.main(["--watch", "2414", "--resume"])


class TestUnsolicitedBytes(unittest.TestCase):
    """Stray bytes are logged and refused, never silently discarded."""

    class FakeSerial:
        is_open = True

        def __init__(self, pending: bytes):
            self.pending = pending
            self.written = []

        @property
        def in_waiting(self):
            return len(self.pending)

        def read(self, n=1):
            data, self.pending = self.pending[:n], self.pending[n:]
            return data

        def write(self, data):
            self.written.append(data)

    def transport(self, pending: bytes, log):
        transport = scan.ScanTransport("/unused", log, serial_module=mock.Mock())
        transport._serial = self.FakeSerial(pending)
        return transport

    def test_stray_bytes_before_a_command_abort_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw.jsonl"
            with scan.RawLog(path, "t") as log:
                for command, guard in (("ATE0", False), ("010D", True)):
                    with self.subTest(command=command):
                        transport = self.transport(b"\r\rELM327 v1.4b\r\r>", log)
                        send = transport.send_guard if guard else transport.send
                        with self.assertRaises(scan.ScanAborted):
                            send(command)
                        self.assertEqual(transport._serial.written, [])
            rx = [decode_record(r) for r in iter_records(path)
                  if r.get("kind") == "io" and r["dir"] == "rx"]
        self.assertEqual(rx, [b"\r\rELM327 v1.4b\r\r>"] * 2)

    def test_stray_bytes_before_the_reset_are_logged_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw.jsonl"
            with scan.RawLog(path, "t") as log:
                transport = self.transport(b"junk", log)
                transport._refuse_unsolicited("ATZ")
            notes = [r.get("note") for r in iter_records(path) if r.get("kind") == "io"]
        self.assertEqual(notes, ["unsolicited bytes before ATZ"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
