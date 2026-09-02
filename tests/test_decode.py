"""Decoder tests use real adapter response shapes, including multi-frame."""

import unittest

from hummer_obd.decode import (
    decode_ascii_item,
    decode_ascii_items,
    decode_cvns,
    decode_dtcs,
    decode_freeze_frame,
    decode_monitor_tests,
    decode_pid,
    decode_pid_per_ecu,
    decode_vin,
    ecu_from_header,
    mask_vin,
    PID_DECODERS,
    parse_reply,
    PidValue,
    supported_mids,
    supported_pids,
    supported_service09_pids,
    UAS_SCALINGS,
)


class TestParseReply(unittest.TestCase):
    def test_no_data(self):
        reply = parse_reply(b"NO DATA\r\r>")
        self.assertEqual(reply.status, "no_data")
        self.assertEqual(reply.marker, "NO DATA")

    def test_unable_to_connect_is_an_error(self):
        self.assertEqual(parse_reply(b"UNABLE TO CONNECT\r>").status, "error")

    def test_searching_prefix_and_frames(self):
        reply = parse_reply(b"SEARCHING...\r41 00 BE 3F A8 13\r\r>")
        self.assertEqual(reply.status, "ok")
        self.assertEqual(reply.frames[0].hex(), "4100be3fa813")

    def test_can_header_is_separated(self):
        reply = parse_reply(b"7E8 06 41 0C 1A F8 00 00\r>")
        self.assertEqual(reply.headers, ["7E8"])
        self.assertEqual(reply.frames[0][0], 0x06)

    def test_29_bit_negative_response_is_not_reported_as_ok(self):
        reply = parse_reply(b"18DAF128037F0122\r>")
        self.assertEqual(reply.status, "negative_response")
        self.assertEqual(reply.negative_responses, [(0x01, 0x22)])
        self.assertIn("conditionsNotCorrect", reply.marker)

    def test_auto_formatted_negative_response_is_recognized(self):
        reply = parse_reply(b"7F 09 11\r>")
        self.assertEqual(reply.status, "negative_response")
        self.assertEqual(reply.negative_responses, [(0x09, 0x11)])
        self.assertIn("serviceNotSupported", reply.marker)

    def test_positive_and_negative_multi_ecu_mix_keeps_positive_status(self):
        reply = parse_reply(b"18DAF14504414233B3\r18DAF128037F0122\r>")
        self.assertEqual(reply.status, "ok")
        self.assertEqual(reply.negative_responses, [(0x01, 0x22)])
        self.assertAlmostEqual(decode_pid("42", reply).value, 13.235, places=3)


class TestServiceOne(unittest.TestCase):
    def test_rpm(self):
        value = decode_pid("0C", parse_reply(b"41 0C 1A F8\r>"))
        self.assertAlmostEqual(value.value, 1726.0)
        self.assertEqual(value.unit, "rpm")
        self.assertEqual(value.status, "ok")

    def test_speed_and_temperature(self):
        self.assertEqual(decode_pid("0D", parse_reply(b"41 0D 40\r>")).value, 64)
        self.assertEqual(decode_pid("05", parse_reply(b"41 05 5A\r>")).value, 50)

    def test_module_voltage(self):
        value = decode_pid("42", parse_reply(b"7E8 04 41 42 33 A0\r>"))
        self.assertAlmostEqual(value.value, 13.216)

    def test_no_data_is_reported_not_invented(self):
        value = decode_pid("0C", parse_reply(b"NO DATA\r>"))
        self.assertIsNone(value.value)
        self.assertEqual(value.status, "no_data")

    def test_support_bitmap(self):
        pids = supported_pids(parse_reply(b"41 00 BE 3F A8 13\r>"), "00")
        self.assertIn("0C", pids)
        self.assertIn("0D", pids)
        self.assertNotIn("02", pids)


class TestDtcs(unittest.TestCase):
    def test_two_codes(self):
        self.assertEqual(decode_dtcs("03", parse_reply(b"43 02 01 43 01 96\r>")), ["P0143", "P0196"])

    def test_no_codes(self):
        self.assertEqual(decode_dtcs("03", parse_reply(b"7E8 03 43 00 00 00\r>")), [])

    def test_pending_and_permanent_modes(self):
        self.assertEqual(decode_dtcs("07", parse_reply(b"47 01 C1 23\r>")), ["U0123"])
        self.assertEqual(decode_dtcs("0A", parse_reply(b"4A 01 81 34\r>")), ["B0134"])


class TestServiceNine(unittest.TestCase):
    VIN = "1G1JC5444R7252367"

    def test_vin_multiline_segments(self):
        raw = b"014\r0: 49 02 01 31 47 31\r1: 4A 43 35 34 34 34 52\r2: 37 32 35 32 33 36 37\r>"
        self.assertEqual(decode_vin(parse_reply(raw)), self.VIN)

    def test_vin_isotp_frames_with_headers(self):
        raw = (b"7E8 10 14 49 02 01 31 47 31\r"
               b"7E8 21 4A 43 35 34 34 34 52\r"
               b"7E8 22 37 32 35 32 33 36 37\r>")
        self.assertEqual(decode_vin(parse_reply(raw)), self.VIN)

    def test_vin_absent(self):
        self.assertIsNone(decode_vin(parse_reply(b"NO DATA\r>")))

    def test_mask_never_reveals_the_middle(self):
        masked = mask_vin(self.VIN)
        self.assertTrue(masked.startswith("1G1"))
        self.assertTrue(masked.endswith("(len=17)"))
        self.assertNotIn(self.VIN[3:-2], masked)
        self.assertEqual(mask_vin(None), "(none)")

    def test_ascii_item(self):
        raw = b"014\r0: 49 04 01 41 42 43\r1: 44 45 46 47 48 49\r>"
        self.assertEqual(decode_ascii_item(parse_reply(raw), 0x04), "ABCDEFGHI")

    def test_ascii_items_remain_separate_per_ecu(self):
        raw = b"7E8 06 49 04 01 41 42 43\r7E9 06 49 04 01 44 45 46\r>"
        self.assertEqual(decode_ascii_items(parse_reply(raw), 0x04), ["ABC", "DEF"])

    def test_service09_support_bitmap(self):
        reply = parse_reply(b"7E8 06 49 00 C0 00 00 00\r>")
        self.assertEqual(supported_service09_pids(reply), ["01", "02"])

    def test_calibration_verification_numbers_are_binary_hex(self):
        raw = (b"7E8 07 49 06 01 12 34 56 78\r"
               b"7E9 07 49 06 01 9A BC DE F0\r>")
        self.assertEqual(decode_cvns(parse_reply(raw)), ["12345678", "9ABCDEF0"])




class TestExtendedCanAddressing(unittest.TestCase):
    """This vehicle answers on ISO 15765-4 CAN 29/500, so every response line
    carries a four-byte identifier in front of the PCI byte.  Without splitting
    that off, byte 0 is 0x18 and every frame looks like the start of a
    multi-frame message — which is how a VIN comes back three characters long.
    """

    VIN = "1G1JC5444R7252367"

    def test_header_is_split_from_the_payload(self):
        from hummer_obd.decode import split_can_header

        header, body = split_can_header(bytes.fromhex("18DAF145024300"))
        self.assertEqual(header.hex().upper(), "18DAF145")
        self.assertEqual(body.hex(), "024300")

    def test_eleven_bit_frames_are_left_alone(self):
        from hummer_obd.decode import split_can_header

        header, body = split_can_header(bytes.fromhex("06410C1AF8"))
        self.assertEqual(header, b"")
        self.assertEqual(body.hex(), "06410c1af8")

    def test_multi_frame_vin_over_29_bit_addressing(self):
        raw = ("18DAF1451014490201314731\r"
               "18DAF145214A43353434345237\r"
               "18DAF1452232353233363700\r>")
        reply = parse_reply(raw)
        self.assertEqual(decode_vin(reply), self.VIN)
        self.assertIn("18DAF145", reply.headers)

    def test_interleaved_ecu_replies_are_reassembled_per_ecu(self):
        # Two modules answer 090A at once and their consecutive frames
        # interleave; each 0x21 frame must join its own 0x1n frame.
        reply = parse_reply(INTERLEAVED_090A)
        self.assertEqual(len(reply.frames), 2)
        self.assertTrue(all(f[:2].hex() == "490a" for f in reply.frames))
        self.assertEqual(sorted(reply.headers), ["18DAF117", "18DAF145"])

    def test_single_frame_dtc_replies_from_many_ecus(self):
        raw = ("18DAF145024300\r18DAF1CD024300\r18DAF11D024300\r"
               "18DAF128024300\r18DAF117024300\r>")
        reply = parse_reply(raw)
        self.assertEqual(len(reply.frames), 5)
        self.assertEqual(decode_dtcs("03", reply), [])
        self.assertEqual(reply.status, "ok")

    def test_current_data_over_29_bit_addressing(self):
        reply = parse_reply("18DAF14504414233B3\r>")
        value = decode_pid("42", reply)
        self.assertAlmostEqual(value.value, 13.235, places=3)
        self.assertEqual(value.status, "ok")


class TestReplyStatus(unittest.TestCase):
    def test_plain_adapter_text_is_not_an_error(self):
        for raw in (b"OK\r\r>", b"ELM327 v1.4b\r\r>", b"OBD SOLUTIONS LLC\r>",
                    b"STN2255 v5.12.4\r>", b"13.9V\r>"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_reply(raw).status, "text")

    def test_real_failures_still_read_as_failures(self):
        self.assertEqual(parse_reply(b"?\r\r>").status, "error")
        self.assertEqual(parse_reply(b"NO DATA\r>").status, "no_data")
        self.assertEqual(parse_reply(b"UNABLE TO CONNECT\r>").status, "error")
        self.assertEqual(parse_reply(b"CAN ERROR\r>").status, "error")

    def test_a_text_reply_never_produces_a_value(self):
        value = decode_pid("0C", parse_reply(b"OK\r>"))
        self.assertIsNone(value.value)
        self.assertEqual(value.status, "text")


#: Two ECUs answering 090A at once, each a two-frame ISO-TP message whose
#: declared length (0x00C) matches what it actually sends.
INTERLEAVED_090A = ("18DAF145100C490A01476174\r"     # ECU 45 first frame
                    "18DAF117100C490A01444D43\r"     # ECU 17 first frame
                    "18DAF1452165776179303000\r"     # ECU 45 consecutive
                    "18DAF11721322D4D4F443000\r>")   # ECU 17 consecutive


class TestIncompleteMultiFrameFailsClosed(unittest.TestCase):
    """A truncated ISO-TP sequence must not look like an answer.

    Synthetic data only — the real VIN never appears in this repository.
    """

    def test_three_frame_vin_decodes_to_exactly_seventeen_characters(self):
        raw = ("18DAF1451014490201314731\r"
               "18DAF145214A43353434345237\r"
               "18DAF1452232353233363700\r>")
        vin = decode_vin(parse_reply(raw))
        self.assertEqual(vin, "1G1JC5444R7252367")
        self.assertEqual(len(vin), 17)

    def test_missing_consecutive_frames_yield_no_vin(self):
        raw = "18DAF1451014490201314731\r>"          # 0x14 bytes promised, 6 sent
        reply = parse_reply(raw)
        self.assertEqual(reply.status, "incomplete")
        self.assertEqual(reply.incomplete, 1)
        self.assertEqual(reply.frames, [])
        self.assertIsNone(decode_vin(reply))
        self.assertEqual(mask_vin(decode_vin(reply)), "(none)")

    def test_partial_sequence_missing_the_last_frame(self):
        raw = ("18DAF1451014490201314731\r"
               "18DAF145214A43353434345237\r>")      # 0x22 never arrives
        reply = parse_reply(raw)
        self.assertIsNone(decode_vin(reply))
        self.assertEqual(reply.incomplete, 1)

    def test_two_response_ids_are_reassembled_independently(self):
        reply = parse_reply(INTERLEAVED_090A)
        self.assertEqual(len(reply.frames), 2)
        names = {decode_ascii_item(parse_reply_from_frames(f), 0x0A) for f in reply.frames}
        self.assertEqual(names, {"Gateway00", "DMC2-MOD0"},
                         "the two ECUs' payloads must not be merged or swapped")
        self.assertIn("18DAF145", reply.headers)
        self.assertIn("18DAF117", reply.headers)

    def test_one_broken_ecu_does_not_hide_a_complete_one(self):
        raw = ("18DAF145100C490A01476174\r"          # ECU 45: 0x21 never arrives
               "18DAF11704490A0141\r>")              # ECU 17: complete, single frame
        reply = parse_reply(raw)
        self.assertEqual(reply.incomplete, 1)
        self.assertEqual(len(reply.frames), 1)
        self.assertEqual(reply.status, "ok")


class TestPidsThisVehicleAdvertises(unittest.TestCase):
    """PIDs the Hummer's own service 01 support bitmap advertises.

    The vehicle advertises 01 0D 1C 1F 20 21 30 31 40 42 60 80 A0 A6, but the
    original probe used a generic PID list that overlapped it in only three
    places.  These cover the readings that were being left on the table.
    """

    def test_odometer_is_four_bytes_at_a_tenth_of_a_kilometre(self):
        # 18DAF117 | 06 | 41 A6 00 12 D6 87  ->  0x0012D687 = 1234567 tenths
        reply = parse_reply(b"18DAF1170641A60012D687\r\r>")
        value = decode_pid("A6", reply)
        self.assertEqual(value.name, "odometer")
        self.assertEqual(value.unit, "km")
        self.assertAlmostEqual(value.value, 123456.7)
        self.assertEqual(value.status, "ok")

    def test_odometer_short_frame_fails_closed_instead_of_inventing_a_reading(self):
        reply = parse_reply(b"18DAF1170341A600\r\r>")
        value = decode_pid("A6", reply)
        self.assertIsNone(value.value)
        self.assertEqual(value.status, "short_frame")

    def test_warm_ups_since_codes_cleared(self):
        value = decode_pid("30", parse_reply(b"18DAF11703413007\r\r>"))
        self.assertEqual(value.value, 7.0)
        self.assertEqual(value.unit, "count")

    def test_obd_standard_code_is_an_enumeration_and_carries_no_unit(self):
        value = decode_pid("1C", parse_reply(b"18DAF11703411C06\r\r>"))
        self.assertEqual(value.value, 6.0)
        self.assertEqual(value.unit, "")

    def test_every_advertised_pid_is_either_decoded_or_explicitly_undecoded(self):
        # Support-bitmap PIDs (20/40/60/80/A0) are pointers, not readings, and
        # 01 is a composite (MIL bit plus readiness monitors) that the scalar
        # PidValue shape cannot represent honestly.  Everything else the
        # vehicle advertises must have a decoder.
        advertised = ["01", "0D", "1C", "1F", "20", "21", "30",
                      "31", "40", "42", "60", "80", "A0", "A6"]
        bitmaps = {"20", "40", "60", "80", "A0"}
        composite = {"01"}
        missing = [p for p in advertised
                   if p not in bitmaps and p not in composite
                   and p not in PID_DECODERS]
        self.assertEqual(missing, [])


#: The real shape of a ``0142`` broadcast on this vehicle: eight modules answer,
#: each reporting the supply voltage measured at its own connector.  Header
#: 18DAF1xx, ISO-TP length 04, then 41 42 and the two-byte millivolt count.
EIGHT_ECUS_ANSWER_0142 = ("18DAF14504414235B3\r"
                          "18DAF1170441423503\r"
                          "18DAF1400441423633\r"
                          "18DAF1CB044142357D\r"
                          "18DAF11D04414234BC\r"
                          "18DAF11E04414234D4\r"
                          "18DAF1CD044142359E\r"
                          "18DAF1280441423656\r>")

#: Addresses and voltages of the eight answers above, in arrival order.
EIGHT_ECU_VOLTAGES = [("45", 13.747), ("17", 13.571), ("40", 13.875), ("CB", 13.693),
                      ("1D", 13.500), ("1E", 13.524), ("CD", 13.726), ("28", 13.910)]


class TestEcuFromHeader(unittest.TestCase):
    def test_29_bit_headers_reduce_to_the_responding_module_byte(self):
        self.assertEqual(ecu_from_header("18DAF145"), "45")
        self.assertEqual(ecu_from_header("18DAF1CB"), "CB")
        self.assertEqual(ecu_from_header("18DAF128"), "28")

    def test_11_bit_identifiers_are_kept_whole(self):
        # Documented choice: an 11-bit identifier has no separate address byte,
        # so the whole identifier is the module's address.
        self.assertEqual(ecu_from_header("7E8"), "7E8")
        self.assertEqual(ecu_from_header("7e9"), "7E9")

    def test_absent_header_names_no_module(self):
        self.assertEqual(ecu_from_header(""), "")
        self.assertEqual(ecu_from_header("   "), "")


class TestEveryRespondingEcuIsKept(unittest.TestCase):
    """Eight modules answer 0142; seven of them used to be thrown away.

    The raw hex always survived in the transcript, but the queryable data was
    one eighth of what the vehicle said.  These are eight measurements of eight
    different things, not eight attempts at one number.
    """

    def test_all_eight_answers_are_decoded_with_their_addresses(self):
        values = decode_pid_per_ecu("42", parse_reply(EIGHT_ECUS_ANSWER_0142))
        self.assertEqual(len(values), 8)
        self.assertEqual([(v.ecu, v.value) for v in values], EIGHT_ECU_VOLTAGES)
        self.assertTrue(all(v.status == "ok" and v.unit == "V" for v in values))

    def test_the_eight_readings_are_genuinely_distinct(self):
        values = decode_pid_per_ecu("42", parse_reply(EIGHT_ECUS_ANSWER_0142))
        self.assertEqual(len({v.value for v in values}), 8)
        self.assertEqual(len({v.ecu for v in values}), 8)

    def test_each_value_carries_only_its_own_frame_as_evidence(self):
        values = decode_pid_per_ecu("42", parse_reply(EIGHT_ECUS_ANSWER_0142))
        self.assertEqual(values[0].raw_hex, "04414235b3")
        self.assertEqual(values[3].raw_hex, "044142357d")

    def test_decode_pid_still_returns_only_the_first_answer(self):
        # Regression: the singular decoder is what existing callers use, and it
        # must keep reporting one value with the whole reply as evidence.
        value = decode_pid("42", parse_reply(EIGHT_ECUS_ANSWER_0142))
        self.assertIsInstance(value, PidValue)
        self.assertAlmostEqual(value.value, 13.747, places=3)
        self.assertEqual(value.ecu, "45")
        self.assertEqual(len(value.raw_hex.split()), 8)

    def test_eleven_bit_replies_are_attributed_to_the_whole_identifier(self):
        reply = parse_reply(b"7E8 04 41 42 35 B3\r7E9 04 41 42 34 BC\r>")
        values = decode_pid_per_ecu("42", reply)
        self.assertEqual([v.ecu for v in values], ["7E8", "7E9"])
        self.assertAlmostEqual(values[1].value, 13.500, places=3)

    def test_a_reply_without_headers_names_no_module(self):
        values = decode_pid_per_ecu("42", parse_reply(b"41 42 35 B3\r>"))
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].ecu, "")
        self.assertAlmostEqual(values[0].value, 13.747, places=3)

    def test_frames_answering_a_different_pid_are_skipped_not_invented(self):
        # ECU 17 is answering 010D and ECU 28 rejected the request outright;
        # neither is a voltage, and neither may become one.
        reply = parse_reply("18DAF14504414235B3\r"
                            "18DAF11703410D40\r"
                            "18DAF128037F0122\r>")
        values = decode_pid_per_ecu("42", reply)
        self.assertEqual([(v.ecu, v.value) for v in values], [("45", 13.747)])

    def test_a_module_that_answered_short_is_reported_not_dropped(self):
        # ECU 17 sent 41 42 35: one byte where the millivolt count needs two.
        reply = parse_reply("18DAF14504414235B3\r18DAF11703414235\r>")
        values = decode_pid_per_ecu("42", reply)
        self.assertEqual([v.status for v in values], ["ok", "short_frame"])
        self.assertEqual(values[1].ecu, "17")
        self.assertIsNone(values[1].value)

    def test_no_answer_yields_no_rows_rather_than_a_placeholder(self):
        for raw in (b"NO DATA\r>", b"CAN ERROR\r>", b"OK\r>"):
            with self.subTest(raw=raw):
                self.assertEqual(decode_pid_per_ecu("42", parse_reply(raw)), [])

    def test_frame_headers_stay_aligned_with_frames(self):
        reply = parse_reply(EIGHT_ECUS_ANSWER_0142)
        self.assertEqual(len(reply.frame_headers), len(reply.frames))
        self.assertEqual(reply.frame_headers[0], "18DAF145")

    def test_an_unattributable_reply_still_decodes(self):
        # A hand-built reply carries no frame_headers at all; the value is
        # still decoded, with no module claimed for it.
        values = decode_pid_per_ecu("42", parse_reply_from_frames(bytes.fromhex("04414235B3")))
        self.assertEqual([(v.ecu, v.value) for v in values], [("", 13.747)])


class TestOnBoardMonitoringTests(unittest.TestCase):
    """Service 06: the monitor results a module computed on its own."""

    #: ECU 45 sends two nine-byte records as one 0x13-byte ISO-TP message while
    #: ECU 17 answers with a single record in a single frame.  Record one uses
    #: UASID 01 (raw counts, known); record two uses UASID 0A, which this build
    #: deliberately does not claim to know.
    TWO_ECUS = ("18DAF145101346010B010100\r"
                "18DAF1170A4602212401000000FFFF\r"
                "18DAF14521000001F4010C0A\r"
                "18DAF14522123400102000\r>")

    def test_supported_mid_bitmap(self):
        mids = supported_mids(parse_reply("18DAF145064600C0000000\r>"))
        self.assertEqual(mids, ["01", "02"])

    def test_a_bitmap_reply_yields_no_test_results(self):
        # 46 00 C0 00 00 00 is an advertisement, not a measurement.
        self.assertEqual(decode_monitor_tests(parse_reply("18DAF145064600C0000000\r>")), [])

    def test_multiple_records_decode_with_their_raw_counts(self):
        tests = decode_monitor_tests(parse_reply(self.TWO_ECUS))
        self.assertEqual(len(tests), 3)
        first, second, third = tests
        self.assertEqual(
            (first.mid, first.tid, first.uasid, first.value, first.min_limit, first.max_limit),
            (0x01, 0x0B, 0x01, 0x0100, 0x0000, 0x01F4),
        )
        self.assertEqual(
            (second.mid, second.tid, second.uasid, second.value,
             second.min_limit, second.max_limit),
            (0x01, 0x0C, 0x0A, 0x1234, 0x0010, 0x2000),
        )
        self.assertEqual(
            (third.mid, third.tid, third.uasid, third.value, third.min_limit, third.max_limit),
            (0x02, 0x21, 0x24, 0x0100, 0x0000, 0xFFFF),
        )

    def test_each_record_is_attributed_to_the_module_that_sent_it(self):
        tests = decode_monitor_tests(parse_reply(self.TWO_ECUS))
        self.assertEqual([t.ecu for t in tests], ["45", "45", "17"])

    def test_a_known_uasid_is_scaled(self):
        first = decode_monitor_tests(parse_reply(self.TWO_ECUS))[0]
        self.assertEqual(first.uasid, 0x01)
        self.assertEqual(
            (first.scaled_value, first.scaled_min, first.scaled_max), (256.0, 0.0, 500.0)
        )

    def test_an_unknown_uasid_reports_no_scaling_and_keeps_the_raw_counts(self):
        second = decode_monitor_tests(parse_reply(self.TWO_ECUS))[1]
        self.assertNotIn(second.uasid, UAS_SCALINGS)
        self.assertIsNone(second.scaled_value)
        self.assertIsNone(second.scaled_min)
        self.assertIsNone(second.scaled_max)
        self.assertEqual(second.unit, "")
        # The measurement itself is untouched: it is recoverable later, once
        # the scaling is confirmed, precisely because nothing was guessed.
        self.assertEqual((second.value, second.min_limit, second.max_limit),
                         (0x1234, 0x0010, 0x2000))

    def test_the_scaling_table_claims_nothing_it_cannot_state(self):
        # Every entry present must carry a real multiplier; the table is
        # allowed to be small, not to be vague.
        for uasid, scaling in UAS_SCALINGS.items():
            with self.subTest(uasid=uasid):
                self.assertIsInstance(scaling.multiplier, float)
                self.assertGreater(scaling.multiplier, 0.0)

    def test_a_truncated_trailing_record_is_dropped(self):
        # Four bytes where nine are needed: a record read out of this would
        # have a plausible TID and invented limits.
        self.assertEqual(decode_monitor_tests(parse_reply("18DAF1450546010B0101\r>")), [])

    def test_an_incomplete_multi_frame_reply_yields_nothing(self):
        reply = parse_reply("18DAF145101346010B010100\r>")
        self.assertEqual(reply.status, "incomplete")
        self.assertEqual(decode_monitor_tests(reply), [])

    def test_a_failed_request_yields_nothing(self):
        self.assertEqual(decode_monitor_tests(parse_reply(b"NO DATA\r>")), [])
        self.assertEqual(decode_monitor_tests(parse_reply(b"7F 06 12\r>")), [])


class TestFreezeFrame(unittest.TestCase):
    """Service 02: the snapshot a module stored when a DTC set."""

    def test_speed_decodes_through_the_service_01_table(self):
        value = decode_freeze_frame("0D", parse_reply("18DAF14504420D0040\r>"))
        self.assertEqual(value.value, 64.0)
        self.assertEqual(value.unit, "km/h")
        self.assertEqual(value.status, "ok")
        self.assertEqual(value.ecu, "45")

    def test_the_frame_byte_is_not_fed_to_the_pid_decoder(self):
        # 42 0C 00 1A F8: frame 00, then 1A F8 = 1726 rpm.  Treating the frame
        # byte as data would give 6.5 rpm — a plausible-looking wrong number,
        # which is exactly what this guards against.
        value = decode_freeze_frame("0C", parse_reply("18DAF14505420C001AF8\r>"))
        self.assertAlmostEqual(value.value, 1726.0)

    def test_a_different_stored_frame_is_not_reported_as_this_one(self):
        value = decode_freeze_frame("0C", parse_reply("18DAF14505420C001AF8\r>"), frame=1)
        self.assertIsNone(value.value)
        self.assertEqual(value.status, "unmatched")

    def test_a_non_zero_frame_number_is_matched(self):
        value = decode_freeze_frame("0D", parse_reply("18DAF14504420D0140\r>"), frame=1)
        self.assertEqual(value.value, 64.0)

    def test_a_truncated_snapshot_fails_closed(self):
        value = decode_freeze_frame("0D", parse_reply("18DAF14503420D00\r>"))
        self.assertIsNone(value.value)
        self.assertEqual(value.status, "short_frame")

    def test_no_answer_is_reported_not_invented(self):
        value = decode_freeze_frame("0D", parse_reply(b"NO DATA\r>"))
        self.assertIsNone(value.value)
        self.assertEqual(value.status, "no_data")


class TestAttributionSurvivesTheAwkwardCases(unittest.TestCase):
    """The cases where a wrong module name, not a wrong number, is the defect.

    Every test here failed to fail before it was written: each one pins a
    behaviour the implementation already had but nothing checked, so a later
    simplification could have quietly reintroduced a misattributed reading.
    """

    def test_a_dropped_multi_frame_reply_does_not_shift_attribution(self):
        # ECU 45 starts a multi-frame answer whose consecutive frames never
        # arrive, so it is discarded; ECU 17 then answers 0142 in one frame.
        # ``headers`` records both modules (45 spoke, even though its bytes did
        # not survive) while ``frames`` holds only ECU 17's, so indexing
        # ``headers`` positionally would credit 13.571 V to module 45 — a
        # reading attributed to a module that never reported one.
        reply = parse_reply("18DAF145101449020101\r18DAF1170441423503\r>")
        self.assertEqual(reply.incomplete, 1)
        self.assertEqual(reply.headers, ["18DAF145", "18DAF117"])
        self.assertEqual(reply.frame_headers, ["18DAF117"])
        values = decode_pid_per_ecu("42", reply)
        self.assertEqual([(v.ecu, v.value) for v in values], [("17", 13.571)])

    def test_two_eleven_bit_modules_do_not_have_their_frames_spliced(self):
        # Both modules send a multi-frame VIN and their consecutive frames
        # interleave, ECU 7E9's arriving first at each sequence number.  Frames
        # must be joined per identifier: matching on the sequence number alone
        # hands 7E9's characters to 7E8 and produces two well-formed,
        # seventeen-character, entirely fictional VINs.
        reply = parse_reply("7E8 10 14 49 02 01 31 47 31\r"
                            "7E9 10 14 49 02 01 35 59 5A\r"
                            "7E9 21 39 39 39 39 39 39 39\r"
                            "7E8 21 43 50 34 39 42 30 30\r"
                            "7E9 22 39 39 39 39 39 39 00\r"
                            "7E8 22 30 30 30 31 32 33 00\r>")
        self.assertEqual(reply.frame_headers, ["7E8", "7E9"])
        text = ["".join(chr(b) for b in f if 0x20 <= b <= 0x7E) for f in reply.frames]
        self.assertTrue(text[0].endswith("1G1CP49B00000123"), text[0])
        self.assertTrue(text[1].endswith("5YZ9999999999999"), text[1])


class TestScalingIsClaimedOnlyWhereItIsKnown(unittest.TestCase):
    def test_the_uas_table_holds_exactly_the_rows_that_were_verified(self):
        """Pin the table so it can only grow by a deliberate, reviewed edit.

        A UASID row is an assertion about what a raw count means, and a wrong
        one is indistinguishable from a measurement.  This test fails whenever
        a row is added, changed or removed, which is the point: the failure is
        the prompt to check the new row against the J1979 UAS table before the
        number it produces is ever written to the database.
        """
        self.assertEqual(
            {uasid: (s.unit, s.multiplier) for uasid, s in UAS_SCALINGS.items()},
            {0x01: ("", 1.0)},
        )

    def test_uasid_24_is_reported_unscaled_because_it_was_never_verified(self):
        """0x24 was in this table with a multiplier of 1.0 and was removed.

        The argument for keeping it was that a multiplier of 1 cannot change a
        magnitude, which is circular: it only holds if 1.0 is the correct
        multiplier, and that could not be confirmed against the J1979 UAS
        table.  If 0x24 is really 0.1, or signed, an entry claiming 1.0 would
        have produced a wrong number that looked exactly like a measurement.
        """
        third = decode_monitor_tests(parse_reply(TestOnBoardMonitoringTests.TWO_ECUS))[2]
        self.assertEqual(third.uasid, 0x24)
        self.assertIsNone(third.scaled_value)
        self.assertIsNone(third.scaled_min)
        self.assertIsNone(third.scaled_max)
        self.assertEqual(third.unit, "")
        # The raw counts are still there: an unknown scaling loses nothing.
        self.assertIsInstance(third.value, int)
        self.assertIsInstance(third.min_limit, int)
        self.assertIsInstance(third.max_limit, int)

    def test_a_bitmap_mid_stops_the_record_walk(self):
        # Two supported-MID bitmaps concatenated in one reply: long enough to
        # look like a nine-byte test record, and its "test" would carry a
        # real-looking TID and invented limits.  Reading must stop at the
        # bitmap MID rather than parse the advertisement as a measurement.
        reply = parse_reply("18DAF145 10 0D 46 00 C0 00 00 00\r"
                            "18DAF145 21 46 20 80 00 00 00 00\r>")
        self.assertEqual(reply.status, "ok")
        self.assertGreaterEqual(len(reply.frames[0]) - 1, 9)
        self.assertEqual(decode_monitor_tests(reply), [])


class TestPidValueStaysBackwardCompatible(unittest.TestCase):
    def test_six_positional_fields_still_construct_a_value(self):
        value = PidValue("0D", "vehicle speed", 0.0, "km/h", "410d00", "ok")
        self.assertEqual(value.ecu, "")


def parse_reply_from_frames(frame: bytes):
    """Wrap a single reassembled frame so a decoder can be pointed at it."""
    from hummer_obd.decode import AdapterReply

    return AdapterReply(raw="", lines=[], frames=[frame], status="ok")


if __name__ == "__main__":
    unittest.main()
