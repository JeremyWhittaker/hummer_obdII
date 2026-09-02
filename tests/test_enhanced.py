"""Supervised enhanced (UDS service 22) reads.

The safety property being tested is not "the enhanced gate works" but "the
enhanced gate cannot widen the unattended one".  Most of what follows is
therefore about what stays refused.

The captured frame used throughout is real: it is the reply this project's
first enhanced read got from the vehicle on 2026-09-02, recorded byte for byte
in ``evidence/enhanced-bt1-decoded.json``.
"""

import unittest

from hummer_obd import safety
from hummer_obd.decode import parse_reply, split_can_header
from hummer_obd.enhanced import (
    BT1,
    PROFILES,
    candidate_scalings,
    _describe_reply,
    run_profile,
)
from hummer_obd.safety import (
    ALLOWED_OBD_MODES,
    ENHANCED_READ_DIDS,
    UnsafeCommandError,
    validate_command,
    validate_enhanced_command,
)
from hummer_obd.transport import Response, SerialTransport, Transport

#: The real reply, exactly as the adapter printed it.
CAPTURED = "142AF1CB056227C6D18A\r\r>"


class TestTwoGatesStaySeparate(unittest.TestCase):
    """The whole design rests on these."""

    def test_unattended_allowlist_never_contains_service_22(self):
        self.assertNotIn("22", ALLOWED_OBD_MODES)

    def test_production_gate_still_refuses_the_allowlisted_identifier(self):
        # The identifier the enhanced gate accepts must still be refused by the
        # gate the collector uses.  If this ever passes, unattended collection
        # can transmit enhanced reads.
        self.assertFalse(safety.is_safe("2227C6"))
        with self.assertRaises(UnsafeCommandError):
            validate_command("2227C6")

    def test_enhanced_gate_refuses_ordinary_read_services(self):
        # Narrower, not wider: a caller reaching for the experimental path must
        # not get the routine one by accident.
        for command in ("0100", "010C", "03", "0902", "0600"):
            with self.subTest(command=command):
                with self.assertRaises(UnsafeCommandError):
                    validate_enhanced_command(command)

    def test_enhanced_gate_refuses_forbidden_services(self):
        for command in ("2E27C6", "3127C6", "2701", "1002", "04"):
            with self.subTest(command=command):
                with self.assertRaises(UnsafeCommandError):
                    validate_enhanced_command(command)


class TestEnhancedIdentifierAllowlist(unittest.TestCase):
    def test_the_allowlisted_identifier_is_accepted(self):
        self.assertEqual(validate_enhanced_command("2227C6"), "2227C6")
        self.assertEqual(validate_enhanced_command("22 27 c6"), "2227C6")

    def test_adjacent_identifiers_are_refused(self):
        # The anti-sweep property.  An earlier version of this test used 0x27C7
        # as a stand-in for "obviously fictional, the next thing a sweep would
        # try".  That turned out to be wrong -- 0x27C7 is a real, documented
        # range identifier on this platform, and it is now allowlisted with a
        # source.  Nearness to a real identifier is no evidence either way,
        # which is exactly why the rule is enumeration and not distance.
        for did in ("2227C5", "2227C8", "2227C9", "2227AE", "2227B0",
                    "220000", "22FFFF", "220045", "225400"):
            with self.subTest(did=did):
                with self.assertRaises(UnsafeCommandError):
                    validate_enhanced_command(did)

    def test_only_the_enumerated_identifiers_are_accepted(self):
        # Stronger than spot checks: walk the whole neighbourhood around every
        # allowlisted identifier and assert the accepted set is exactly the
        # allowlist.  A prefix rule, an off-by-one, or a stray range would all
        # show up here and nowhere else.
        accepted = set()
        for high in {did[:2] for did in ENHANCED_READ_DIDS}:
            for low in range(0x100):
                candidate = f"22{high}{low:02X}"
                try:
                    validate_enhanced_command(candidate)
                except UnsafeCommandError:
                    continue
                accepted.add(candidate[2:])
        self.assertEqual(accepted, set(ENHANCED_READ_DIDS))

    def test_collector_gate_refuses_every_enhanced_identifier(self):
        for did in ENHANCED_READ_DIDS:
            with self.subTest(did=did):
                self.assertFalse(safety.is_safe(f"22{did}"))

    def test_identifier_must_be_exactly_two_bytes(self):
        for command in ("22", "2227", "2227C6C6", "2227C600"):
            with self.subTest(command=command):
                with self.assertRaises(UnsafeCommandError):
                    validate_enhanced_command(command)

    def test_batching_is_refused(self):
        for command in ("2227C6;0100", "2227C6\r0100", "2227C6\n03", "2227C6\x00"):
            with self.subTest(command=command):
                with self.assertRaises(UnsafeCommandError):
                    validate_enhanced_command(command)

    def test_every_allowlisted_identifier_carries_provenance(self):
        # An identifier with no source has no business being transmittable.
        for did, source in ENHANCED_READ_DIDS.items():
            with self.subTest(did=did):
                self.assertRegex(did, r"^[0-9A-F]{4}$")
                self.assertGreater(len(source), 20, "provenance must be specific")

    def test_adapter_commands_reuse_the_production_gate(self):
        # Delegation, not duplication: the experimental path must not be able
        # to authorise an adapter command the ordinary gate would refuse.
        self.assertEqual(validate_enhanced_command("ATRV"), "ATRV")
        with self.assertRaises(UnsafeCommandError):
            validate_enhanced_command("ATMA")


class TestAddressingCommands(unittest.TestCase):
    """The commands GM 29-bit enhanced addressing needs, all read-path."""

    def test_new_adapter_commands_are_allowed(self):
        for command in (
            "ATCP14", "ATSHDACBF1", "ATCRA142AF1CB",
            "ATFCSH14DACBF1", "ATFCSD300000", "ATFCSM1", "ATST96",
        ):
            with self.subTest(command=command):
                self.assertEqual(validate_command(command), command)

    def test_malformed_variants_are_still_refused(self):
        for command in ("ATCP", "ATCP1", "ATCPZZ", "ATFCSM3", "ATFCSH", "ATFCSDZZ"):
            with self.subTest(command=command):
                with self.assertRaises(UnsafeCommandError):
                    validate_command(command)

    def test_every_profile_init_command_is_on_the_ordinary_allowlist(self):
        for profile in PROFILES.values():
            for command in profile.init:
                with self.subTest(profile=profile.key, command=command):
                    self.assertEqual(validate_command(command), command)

    def test_every_profile_request_is_on_the_enhanced_allowlist(self):
        for profile in PROFILES.values():
            for request, _signal, _decoder in profile.requests:
                with self.subTest(profile=profile.key, request=request):
                    self.assertEqual(validate_enhanced_command(request), request)


class TestGmResponseHeader(unittest.TestCase):
    """The GM reply is not the legislated 18 DA form and must still parse."""

    def test_priority_14_header_is_recognised(self):
        header, rest = split_can_header(bytes.fromhex("142AF1CB056227C6D18A"))
        self.assertEqual(header.hex().upper(), "142AF1CB")
        self.assertEqual(rest.hex().upper(), "056227C6D18A")

    def test_legislated_header_is_unchanged(self):
        header, rest = split_can_header(bytes.fromhex("18DAF145101449020000"))
        self.assertEqual(header.hex().upper(), "18DAF145")
        self.assertEqual(rest.hex().upper(), "101449020000")

    def test_payload_that_merely_starts_142a_is_not_split(self):
        # 0x9F is not a plausible ISO-TP PCI, so this must be left whole rather
        # than have four bytes taken off it on the strength of two.
        frame = bytes.fromhex("142AF1CB9F1122")
        header, rest = split_can_header(frame)
        self.assertEqual(header, b"")
        self.assertEqual(rest, frame)

    def test_single_frame_length_must_agree(self):
        # PCI says seven data bytes but only two followed.
        frame = bytes.fromhex("142AF1CB071122")
        self.assertEqual(split_can_header(frame)[0], b"")

    def test_captured_reply_parses_as_a_complete_frame(self):
        reply = parse_reply(CAPTURED)
        self.assertEqual(reply.status, "ok")
        self.assertEqual(reply.incomplete, 0)
        self.assertEqual([f.hex().upper() for f in reply.frames], ["056227C6D18A"])
        self.assertEqual(reply.frame_headers, ["142AF1CB"])


class TestScalings(unittest.TestCase):
    def test_windows_cover_every_adjacent_pair(self):
        windows = candidate_scalings(bytes.fromhex("01020304"))
        self.assertEqual([w["offset"] for w in windows], ["B0:B1", "B1:B2", "B2:B3"])
        self.assertEqual(windows[0]["raw"], 0x0102)

    def test_single_byte_payload_yields_no_window(self):
        self.assertEqual(candidate_scalings(b"\x01"), [])

    def test_published_offset_lands_on_the_published_value(self):
        # The claim being tested is the one the documentation makes: counting
        # B0 from the first byte of the whole CAN frame, the profile's B8:B9 is
        # the state-of-charge field.  This is what makes the offset checkable
        # rather than asserted.
        record = _describe_reply(parse_reply(CAPTURED), "2227C6")
        self.assertTrue(record["positive_response"])
        self.assertEqual(record["payload_hex"], "D18A")
        self.assertEqual(record["can_frame_hex"], "142AF1CB056227C6D18A")
        window = {w["offset"]: w for w in record["scalings_from_can_frame"]}["B8:B9"]
        self.assertEqual(window["hex"], "D18A")
        self.assertAlmostEqual(window["div_655_35"], 81.852, places=2)


class TestNegativeResponses(unittest.TestCase):
    def test_negative_response_is_recorded_not_decoded(self):
        record = _describe_reply(parse_reply("142AF1CB037F2231\r\r>"), "2227C6")
        self.assertFalse(record["positive_response"])
        self.assertEqual(
            record["negative_responses"],
            [{"service": "22", "code": "31", "name": "requestOutOfRange"}],
        )

    def test_unrelated_frame_is_not_read_as_an_answer(self):
        # A frame that survived the receive filter but does not echo 62 27 C6
        # must not be mistaken for a reading.
        record = _describe_reply(parse_reply("142AF1CB0562280000\r\r>"), "2227C6")
        self.assertFalse(record["positive_response"])
        self.assertNotIn("payload_hex", record)


class _FakeTransport(Transport):
    """Records what was sent and replays canned replies."""

    def __init__(self, replies):
        self.sent = []
        self._replies = replies

    def open(self):  # pragma: no cover - not used
        pass

    def close(self):  # pragma: no cover - not used
        pass

    def send(self, command, timeout=None):
        self.sent.append(command)
        data = self._replies.get(command, "OK\r\r>")
        return Response(command=command, data=data.encode("ascii"), elapsed_s=0.01)


class TestRunProfile(unittest.TestCase):
    def test_sends_init_then_each_request_exactly_once(self):
        fake = _FakeTransport({"2227C6": CAPTURED, "ATRV": "13.8V\r\r>"})
        result = run_profile(BT1, fake)
        self.assertEqual(fake.sent[: len(BT1.init)], list(BT1.init))
        self.assertEqual(result.adapter_voltage, "13.8V")

        # The invariant is "each identifier once", not "only one identifier":
        # a profile may carry several, but nothing may be retried or iterated.
        requested = [c for c in fake.sent if not c.startswith(("AT", "ST"))]
        self.assertEqual(requested, [r[0] for r in BT1.requests])
        self.assertEqual(len(requested), len(set(requested)), "no repeats")
        self.assertEqual(len(result.reads), len(BT1.requests))

        soc = next(r for r in result.reads if r["request"] == "2227C6")
        self.assertEqual(soc["payload_hex"], "D18A")

    def test_every_request_is_a_read_by_identifier(self):
        # Nothing in a profile may be a write, control or security service,
        # whatever else a future edit adds to it.
        for profile in PROFILES.values():
            for request, _signal, _decoder in profile.requests:
                with self.subTest(profile=profile.key, request=request):
                    self.assertTrue(request.startswith("22"))

    def test_records_the_voltage_so_a_no_data_can_be_interpreted(self):
        # A NO DATA at 12.8 V (asleep) and at 13.8 V (awake) are different
        # results; without the voltage they file as the same one.
        fake = _FakeTransport({"2227C6": "NO DATA\r\r>", "ATRV": "12.8V\r\r>"})
        result = run_profile(BT1, fake)
        self.assertEqual(result.adapter_voltage, "12.8V")
        self.assertEqual(result.reads[0]["status"], "no_data")


class TestTransportValidatorDefault(unittest.TestCase):
    """The transport re-validates independently of its caller."""

    class _Log:
        def log_tx(self, *a, **k):
            pass

        def log_rx(self, *a, **k):
            pass

        def write_event(self, *a, **k):
            pass

    def test_default_validator_is_the_unattended_gate(self):
        transport = SerialTransport("/dev/null", self._Log(), serial_module=object())
        self.assertIs(transport._validator, validate_command)

    def test_enhanced_validator_must_be_passed_explicitly(self):
        transport = SerialTransport(
            "/dev/null", self._Log(), serial_module=object(),
            validator=validate_enhanced_command,
        )
        self.assertIs(transport._validator, validate_enhanced_command)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
