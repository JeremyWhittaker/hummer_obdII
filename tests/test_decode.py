"""Decoder tests use real adapter response shapes, including multi-frame."""

import unittest

from hummer_obd.decode import (
    decode_ascii_item,
    decode_ascii_items,
    decode_ascii_items_per_ecu,
    decode_cvns,
    decode_cvns_per_ecu,
    decode_dtcs,
    decode_dtcs_per_ecu,
    decode_freeze_frame,
    decode_monitor_status,
    decode_monitor_tests,
    decode_pid,
    decode_pid_per_ecu,
    decode_vin,
    ecu_from_header,
    mask_vin,
    negative_response_name,
    parse_can_status,
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


#: The real shape of an ``03`` request on this vehicle: five modules answer and
#: every one of them says ``43 00`` — a positive response whose code count is
#: zero.  Header 18DAF1xx, ISO-TP length 02, then the ``43`` response byte and
#: the count.  Nothing is wrong with this vehicle, so this is the only DTC
#: reply it has ever produced.
FIVE_ECUS_REPORT_NO_CODES = ("18DAF145024300\r18DAF1CD024300\r18DAF11D024300\r"
                             "18DAF128024300\r18DAF117024300\r>")


class TestDtcsPerEcu(unittest.TestCase):
    """A module reporting nothing is a row; that is the whole point.

    ``decode_dtcs`` keeps the byte alignment that *yields codes*, so a module
    answering ``43 00`` yields nothing at either alignment.  Ported naively,
    that drops precisely the rows this vehicle produces, and the result — an
    empty list — is indistinguishable from a bus that never answered.
    """

    def test_every_module_that_answered_gets_a_row_with_no_codes(self):
        rows = decode_dtcs_per_ecu("03", parse_reply(FIVE_ECUS_REPORT_NO_CODES))
        self.assertEqual([r.ecu for r in rows], ["45", "CD", "1D", "28", "17"])
        self.assertTrue(all(r.status == "ok" for r in rows))
        self.assertTrue(all(r.codes == [] for r in rows))
        self.assertTrue(all(r.detail == "" for r in rows))

    def test_five_modules_answering_nothing_is_not_the_same_as_silence(self):
        # The observation that matters on a healthy vehicle: the singular
        # decoder returns [] for both of these replies, and this one does not.
        answered = decode_dtcs_per_ecu("03", parse_reply(FIVE_ECUS_REPORT_NO_CODES))
        silent = decode_dtcs_per_ecu("03", parse_reply(b"NO DATA\r>"))
        self.assertEqual(decode_dtcs("03", parse_reply(FIVE_ECUS_REPORT_NO_CODES)), [])
        self.assertEqual(decode_dtcs("03", parse_reply(b"NO DATA\r>")), [])
        self.assertEqual(len(answered), 5)
        self.assertEqual(silent, [])

    def test_a_module_holding_codes_reports_them(self):
        # 18DAF117 | 06 | 43 02 01 43 01 96 -> count 02, then P0143 and P0196.
        rows = decode_dtcs_per_ecu("03", parse_reply("18DAF11706430201430196\r>"))
        self.assertEqual([(r.ecu, r.codes) for r in rows], [("17", ["P0143", "P0196"])])
        self.assertEqual(rows[0].status, "ok")

    def test_codes_and_silence_from_different_modules_stay_apart(self):
        rows = decode_dtcs_per_ecu("03", parse_reply("18DAF145024300\r"
                                                     "18DAF11706430201430196\r"
                                                     "18DAF128024300\r>"))
        self.assertEqual([(r.ecu, r.codes) for r in rows],
                         [("45", []), ("17", ["P0143", "P0196"]), ("28", [])])

    def test_the_same_code_from_two_modules_is_two_rows(self):
        # decode_dtcs de-duplicates because it answers "what is wrong with the
        # vehicle".  Per module the repetition is the finding: two modules
        # independently set U0140, which is not one fault reported twice.
        # 04 | 43 01 C1 40: one code, U0140 ("lost communication with the
        # ECM/PCM"), which two modules on one bus really would set together.
        raw = "18DAF145044301C140\r18DAF128044301C140\r>"
        rows = decode_dtcs_per_ecu("03", parse_reply(raw))
        self.assertEqual([(r.ecu, r.codes) for r in rows],
                         [("45", ["U0140"]), ("28", ["U0140"])])
        self.assertEqual(decode_dtcs("03", parse_reply(raw)), ["U0140"])

    def test_a_rejection_is_a_row_naming_the_reason(self):
        rows = decode_dtcs_per_ecu("03", parse_reply("18DAF128037F0322\r>"))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row.ecu, row.status, row.codes), ("28", "negative_response", []))
        self.assertEqual(row.detail, "conditionsNotCorrect")
        self.assertEqual(row.detail, negative_response_name(0x22))

    def test_rows_survive_a_reply_that_is_nothing_but_rejections(self):
        # parse_reply calls this reply "negative_response", not "ok".  Gating
        # on reply.ok — which decode_pid_per_ecu does, correctly, because a
        # rejection is not a measurement — would throw away the record that two
        # named modules refused, which is the only thing this reply says.
        reply = parse_reply("18DAF128037F0311\r18DAF145037F0312\r>")
        self.assertEqual(reply.status, "negative_response")
        rows = decode_dtcs_per_ecu("03", reply)
        self.assertEqual([(r.ecu, r.detail) for r in rows],
                         [("28", "serviceNotSupported"), ("45", "subFunctionNotSupported")])

    def test_a_rejection_of_another_service_is_not_this_requests_answer(self):
        # ECU 28 refused a service 01 request that was still in flight.  It is
        # not an answer to 03 and must not become one module's DTC row.
        rows = decode_dtcs_per_ecu("03", parse_reply("18DAF145024300\r18DAF128037F0122\r>"))
        self.assertEqual([(r.ecu, r.status) for r in rows], [("45", "ok")])

    def test_a_response_byte_with_nothing_behind_it_is_not_a_report_of_none(self):
        # 18DAF145 | 01 | 43: the count byte never arrived.  Calling this "no
        # codes" would invent the one fact the frame failed to carry.
        rows = decode_dtcs_per_ecu("03", parse_reply("18DAF1450143\r>"))
        self.assertEqual([(r.ecu, r.status, r.codes) for r in rows], [("45", "short_frame", [])])
        self.assertIn("no data bytes", rows[0].detail)

    def test_pending_and_permanent_modes_are_labelled_by_service(self):
        pending = decode_dtcs_per_ecu("07", parse_reply("18DAF145044701C123\r>"))
        permanent = decode_dtcs_per_ecu("0A", parse_reply("18DAF145044A018134\r>"))
        self.assertEqual([(r.mode, r.codes) for r in pending], [("07", ["U0123"])])
        self.assertEqual([(r.mode, r.codes) for r in permanent], [("0A", ["B0134"])])

    def test_each_row_carries_only_its_own_frame_as_evidence(self):
        rows = decode_dtcs_per_ecu("03", parse_reply(FIVE_ECUS_REPORT_NO_CODES))
        self.assertTrue(all(r.raw_hex == "024300" for r in rows))

    def test_a_reply_that_nobody_answered_yields_no_rows(self):
        for raw in (b"NO DATA\r>", b"CAN ERROR\r>", b"OK\r>", b"\r>"):
            with self.subTest(raw=raw):
                self.assertEqual(decode_dtcs_per_ecu("03", parse_reply(raw)), [])

    def test_a_reply_without_headers_names_no_module(self):
        rows = decode_dtcs_per_ecu("03", parse_reply(b"43 00\r>"))
        self.assertEqual([(r.ecu, r.status, r.codes) for r in rows], [("", "ok", [])])

    def test_an_incomplete_multi_frame_reply_yields_no_rows(self):
        # The consecutive frames never arrived, so nothing was reassembled and
        # no module can be said to have answered.
        reply = parse_reply("18DAF1451014430A01430196\r>")
        self.assertEqual(reply.status, "incomplete")
        self.assertEqual(decode_dtcs_per_ecu("03", reply), [])


#: 18DAF145 | 06 | 41 01 83 07 65 04.  Byte A 0x83: MIL commanded on, three
#: emission codes stored by this module.  Byte B 0x07: all three continuous
#: monitors supported, none of the not-complete bits set, bit 3 clear so the
#: spark table names bytes C and D.  Byte C 0x65 supports catalyst,
#: evaporative system, oxygen sensor and oxygen sensor heater; byte D 0x04
#: leaves the evaporative-system monitor not complete.
SPARK_MONITOR_STATUS = "18DAF14506410183076504\r>"

#: 18DAF117 | 06 | 41 01 00 0F 02 02.  Same shape with bit 3 of byte B set, so
#: the compression table names the identical C/D bytes differently.
COMPRESSION_MONITOR_STATUS = "18DAF117064101000F0202\r>"


class TestMonitorStatus(unittest.TestCase):
    """Service 01 PID 01: four bytes of packed flags, one row per module."""

    def readiness(self, raw):
        status = decode_monitor_status(parse_reply(raw))[0]
        return {bit.monitor: bit for bit in status.readiness}

    def test_mil_state_and_this_modules_own_code_count(self):
        status = decode_monitor_status(parse_reply(SPARK_MONITOR_STATUS))[0]
        self.assertEqual(status.ecu, "45")
        self.assertTrue(status.mil_on)
        self.assertEqual(status.dtc_count, 3)
        self.assertEqual(status.status, "ok")
        self.assertEqual(status.raw_hex, "06410183076504")

    def test_the_count_is_seven_bits_and_the_mil_bit_is_not_part_of_it(self):
        # 0xFF would read as 255 codes if the MIL bit were left in the count.
        status = decode_monitor_status(parse_reply("18DAF145064101FF000000\r>"))[0]
        self.assertTrue(status.mil_on)
        self.assertEqual(status.dtc_count, 0x7F)

    def test_the_mil_can_be_off_with_codes_stored(self):
        status = decode_monitor_status(parse_reply(COMPRESSION_MONITOR_STATUS))[0]
        self.assertFalse(status.mil_on)
        self.assertEqual(status.dtc_count, 0)

    def test_every_module_that_answered_gets_its_own_row(self):
        raw = SPARK_MONITOR_STATUS.rstrip(">") + COMPRESSION_MONITOR_STATUS
        rows = decode_monitor_status(parse_reply(raw))
        self.assertEqual([(r.ecu, r.dtc_count) for r in rows], [("45", 3), ("17", 0)])

    def test_continuous_monitors_come_from_byte_b(self):
        bits = self.readiness(SPARK_MONITOR_STATUS)
        for name, index in (("misfire", 0), ("fuel_system", 1), ("components", 2)):
            with self.subTest(monitor=name):
                self.assertEqual(bits[name].kind, "continuous")
                self.assertEqual((bits[name].src_byte, bits[name].src_bit), ("B", index))
                self.assertTrue(bits[name].supported)
                self.assertTrue(bits[name].complete)

    def test_a_set_not_complete_bit_reads_as_not_ready(self):
        # Byte B 0x17: bits 0-2 supported, bit 4 set, so the misfire monitor is
        # supported and has not completed while the other two have.
        bits = self.readiness("18DAF14506410100170000\r>")
        self.assertEqual(bits["misfire"].complete, False)
        self.assertEqual(bits["fuel_system"].complete, True)
        self.assertEqual(bits["components"].complete, True)

    def test_non_continuous_monitors_come_from_bytes_c_and_d(self):
        bits = self.readiness(SPARK_MONITOR_STATUS)
        self.assertEqual(bits["catalyst"].kind, "non_continuous")
        self.assertEqual((bits["catalyst"].src_byte, bits["catalyst"].src_bit), ("C", 0))
        self.assertTrue(bits["catalyst"].complete)
        # C bit 2 supported, D bit 2 set: running, and not finished.
        self.assertTrue(bits["evaporative_system"].supported)
        self.assertEqual(bits["evaporative_system"].complete, False)
        self.assertEqual(bits["oxygen_sensor_heater"].src_bit, 6)

    def test_an_unsupported_monitor_is_never_reported_complete(self):
        # Byte B 0x00 and bytes C/D 0x00: nothing is supported, and every
        # not-complete bit is clear.  Read as a boolean, a clear bit says
        # "complete", which would put eleven monitors the vehicle does not run
        # into the ready column.  None is the only true answer.
        bits = self.readiness("18DAF14506410100000000\r>")
        self.assertEqual(len(bits), 11)
        for name, bit in bits.items():
            with self.subTest(monitor=name):
                self.assertFalse(bit.supported)
                self.assertIsNone(bit.complete)

    def test_unsupported_and_unfinished_are_different_facts(self):
        # C 0x03, D 0x02: bit 0 supported and complete, bit 1 supported and not
        # complete, bit 2 not supported at all.  Three states, not two.
        bits = self.readiness("18DAF14506410100000302\r>")
        self.assertEqual((bits["catalyst"].supported, bits["catalyst"].complete), (True, True))
        self.assertEqual((bits["heated_catalyst"].supported, bits["heated_catalyst"].complete),
                         (True, False))
        self.assertEqual((bits["evaporative_system"].supported,
                          bits["evaporative_system"].complete), (False, None))

    def test_bit_three_of_byte_b_selects_the_name_table(self):
        spark = decode_monitor_status(parse_reply(SPARK_MONITOR_STATUS))[0]
        compression = decode_monitor_status(parse_reply(COMPRESSION_MONITOR_STATUS))[0]
        self.assertEqual(spark.ignition_type, "spark")
        self.assertEqual(compression.ignition_type, "compression")
        self.assertEqual([b.monitor for b in spark.readiness][:5],
                         ["misfire", "fuel_system", "components", "catalyst", "heated_catalyst"])
        self.assertEqual([b.monitor for b in compression.readiness][3:5],
                         ["nmhc_catalyst", "nox_scr_aftertreatment"])

    def test_the_same_cd_bytes_are_named_differently_by_ignition_type(self):
        # Identical C/D bytes, one bit of difference in B.  Bit 1 is the heated
        # catalyst on a spark engine and NOx/SCR aftertreatment on a
        # compression one: reading bit 3 backwards mislabels rather than fails.
        spark = self.readiness("18DAF14506410100070202\r>")
        compression = self.readiness("18DAF117064101000F0202\r>")
        self.assertEqual(spark["heated_catalyst"].complete, False)
        self.assertEqual(compression["nox_scr_aftertreatment"].complete, False)
        self.assertNotIn("heated_catalyst", compression)
        self.assertNotIn("nox_scr_aftertreatment", spark)

    def test_spark_bit_four_is_named_reserved_not_ac_refrigerant(self):
        # Older ELM-derived references call this the A/C refrigerant monitor;
        # J1979-DA reserves the bit.  Both cannot be printed on a stored row,
        # so the bit is reported and the claim about it is withheld.
        bits = self.readiness(SPARK_MONITOR_STATUS)
        self.assertIn("reserved_b4", bits)
        self.assertEqual(bits["reserved_b4"].src_bit, 4)
        self.assertNotIn("ac_refrigerant", bits)

    def test_the_compression_table_reserves_bits_two_and_four(self):
        bits = self.readiness(COMPRESSION_MONITOR_STATUS)
        self.assertEqual(bits["reserved_b2"].src_bit, 2)
        self.assertEqual(bits["reserved_b4"].src_bit, 4)

    def test_a_short_frame_decodes_no_bits_at_all(self):
        # 41 01 83 07: two of the four data bytes.  Byte A and byte B are
        # there, and decoding them would produce three confident continuous
        # rows about a frame that never carried C or D.
        rows = decode_monitor_status(parse_reply("18DAF1450441018307\r>"))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row.ecu, row.status), ("45", "short_frame"))
        self.assertIsNone(row.mil_on)
        self.assertIsNone(row.dtc_count)
        self.assertEqual(row.ignition_type, "")
        self.assertEqual(row.readiness, [])
        self.assertEqual(row.raw_hex, "0441018307")

    def test_three_data_bytes_are_still_a_short_frame(self):
        # 41 01 83 07 65: byte D is the one missing.  Four bytes are needed
        # before any of them is read, so this is the boundary case that says
        # so -- the alternative is an IndexError, or C decoded against a D
        # that never arrived.
        rows = decode_monitor_status(parse_reply("18DAF145054101830765\r>"))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row.ecu, row.status), ("45", "short_frame"))
        self.assertIsNone(row.mil_on)
        self.assertIsNone(row.dtc_count)
        self.assertEqual(row.ignition_type, "")
        self.assertEqual(row.readiness, [])
        self.assertEqual(row.raw_hex, "054101830765")

    def test_a_short_frame_from_one_module_does_not_hide_a_good_one(self):
        raw = "18DAF1450441018307\r" + COMPRESSION_MONITOR_STATUS
        rows = decode_monitor_status(parse_reply(raw))
        self.assertEqual([(r.ecu, r.status) for r in rows], [("45", "short_frame"), ("17", "ok")])

    def test_frames_answering_something_else_are_skipped(self):
        # A voltage reply and a rejection: neither is a readiness report.
        rows = decode_monitor_status(parse_reply("18DAF14504414235B3\r"
                                                 "18DAF128037F0122\r"
                                                 + COMPRESSION_MONITOR_STATUS))
        self.assertEqual([r.ecu for r in rows], ["17"])

    def test_no_answer_yields_no_rows(self):
        for raw in (b"NO DATA\r>", b"CAN ERROR\r>", b"OK\r>"):
            with self.subTest(raw=raw):
                self.assertEqual(decode_monitor_status(parse_reply(raw)), [])

    def test_a_reply_without_headers_names_no_module(self):
        rows = decode_monitor_status(parse_reply(b"41 01 83 07 65 04\r>"))
        self.assertEqual([(r.ecu, r.dtc_count) for r in rows], [("", 3)])

    def test_pid_01_still_decodes_as_undecoded_for_existing_callers(self):
        # PID 01 is four bytes of flags, which the scalar PidValue shape cannot
        # represent honestly, so it stays out of PID_DECODERS.  Callers on the
        # ordinary path must keep seeing "undecoded" rather than one of the
        # four bytes reported as "the value".
        self.assertNotIn("01", PID_DECODERS)
        value = decode_pid("01", parse_reply(SPARK_MONITOR_STATUS))
        self.assertIsNone(value.value)
        self.assertEqual(value.status, "undecoded")
        per_ecu = decode_pid_per_ecu("01", parse_reply(SPARK_MONITOR_STATUS))
        self.assertEqual([(v.ecu, v.status, v.value) for v in per_ecu],
                         [("45", "undecoded", None)])


class TestServiceNinePerEcu(unittest.TestCase):
    """Service 09 values tagged with their module — where the wire allows it."""

    def test_two_modules_are_kept_apart(self):
        raw = b"7E8 06 49 04 01 41 42 43\r7E9 06 49 04 01 44 45 46\r>"
        self.assertEqual(decode_ascii_items_per_ecu(parse_reply(raw), 0x04),
                         [("7E8", "ABC"), ("7E9", "DEF")])

    def test_29_bit_multi_frame_replies_are_attributed(self):
        # Both modules answer 090A across two frames each; the reassembled
        # message keeps the identifier it arrived under.
        reply = parse_reply(INTERLEAVED_090A)
        self.assertEqual(decode_ascii_items_per_ecu(reply, 0x0A),
                         [("45", "Gateway00"), ("17", "DMC2-MOD0")])

    def test_a_segment_reassembled_reply_names_no_module(self):
        # The caveat that matters on real hardware.  When the adapter prints a
        # multi-frame answer in its "0:"/"1:" segment form, the identifier is
        # gone before the payload is: _extract_frames returns [""] + the plain
        # headers for the reassembled message because there is nothing left to
        # attribute it to.  Service 09 replies are usually multi-frame, so this
        # is the common case, and "" is the honest answer — an address guessed
        # from the surrounding traffic would read as a measured one.
        reply = parse_reply(b"014\r0: 49 04 01 41 42 43\r1: 44 45 46 47 48 49\r>")
        self.assertEqual(reply.frame_headers, [""])
        self.assertEqual(decode_ascii_items_per_ecu(reply, 0x04), [("", "ABCDEFGHI")])
        # The value itself is complete; only the attribution is missing.
        self.assertEqual(decode_ascii_items(reply, 0x04), ["ABCDEFGHI"])

    def test_the_untagged_decoder_returns_the_same_values_in_the_same_order(self):
        raw = b"7E8 06 49 04 01 41 42 43\r7E9 06 49 04 01 44 45 46\r>"
        reply = parse_reply(raw)
        self.assertEqual(decode_ascii_items(reply, 0x04),
                         [value for _ecu, value in decode_ascii_items_per_ecu(reply, 0x04)])
        self.assertEqual(decode_ascii_item(reply, 0x04), "ABC / DEF")

    def test_calibration_verification_numbers_are_tagged(self):
        raw = (b"7E8 07 49 06 01 12 34 56 78\r"
               b"7E9 07 49 06 01 9A BC DE F0\r>")
        self.assertEqual(decode_cvns_per_ecu(parse_reply(raw)),
                         [("7E8", "12345678"), ("7E9", "9ABCDEF0")])

    def test_two_cvns_from_one_module_are_two_rows_with_one_address(self):
        # 49 06 02 then two four-byte numbers: eleven bytes, so a real module
        # sends it as an ISO-TP pair.  Both rows belong to module 45, and the
        # repeated address is the module's doing, not the decoder's.
        reply = parse_reply("18DAF145100B490602123456\r18DAF14521789ABCDEF000\r>")
        self.assertEqual(decode_cvns_per_ecu(reply),
                         [("45", "12345678"), ("45", "9ABCDEF0")])
        self.assertEqual(decode_cvns(reply), ["12345678", "9ABCDEF0"])

    def test_a_segment_reassembled_cvn_reply_names_no_module(self):
        reply = parse_reply(b"00B\r0: 49 06 02 12 34 56\r1: 78 9A BC DE F0\r>")
        self.assertEqual(decode_cvns_per_ecu(reply),
                         [("", "12345678"), ("", "9ABCDEF0")])

    def test_no_answer_yields_no_rows(self):
        for raw in (b"NO DATA\r>", b"OK\r>"):
            with self.subTest(raw=raw):
                self.assertEqual(decode_ascii_items_per_ecu(parse_reply(raw), 0x04), [])
                self.assertEqual(decode_cvns_per_ecu(parse_reply(raw)), [])


class TestCanStatusCounters(unittest.TestCase):
    """ATCS: the adapter's own error counters, and what may be claimed of them."""

    def test_the_reply_the_live_adapter_sends(self):
        self.assertEqual(parse_can_status("T:00 R:00"), (0, 0))

    def test_the_reply_survives_line_endings_case_and_the_prompt(self):
        for text in ("T:00 R:00\r\r>", "t:00 r:00", "  T:00  R:00  ", "ATCS / T:00 R:00"):
            with self.subTest(text=text):
                self.assertEqual(parse_can_status(text), (0, 0))

    def test_counters_are_parsed_as_hex(self):
        # Documented choice, not a verified one: every value observed on this
        # vehicle is 00, where hex and decimal agree, so nothing in the
        # evidence settles the radix.  This pins the behaviour so that
        # confirming the radix later is a deliberate edit to a failing test.
        self.assertEqual(parse_can_status("T:0A R:1F"), (10, 31))

    def test_the_only_claim_that_survives_the_radix_ambiguity_is_zero(self):
        # Whatever the radix, "00" is zero and any other two-digit field is
        # not, so callers may test a counter against zero and may not read a
        # non-zero one as a count of errors.
        self.assertEqual(parse_can_status("T:00 R:00"), (0, 0))
        for text in ("T:01 R:00", "T:00 R:10", "T:99 R:99"):
            with self.subTest(text=text):
                transmit, receive = parse_can_status(text)
                self.assertNotEqual((transmit, receive), (0, 0))

    def test_anything_that_is_not_a_counter_pair_is_refused(self):
        for text in ("", "OK", "?", "NO DATA", "13.9V", "ELM327 v1.4b", "T:R:"):
            with self.subTest(text=text):
                self.assertEqual(parse_can_status(text), (None, None))

    def test_half_a_status_is_not_a_status(self):
        # One counter without the other is not the ATCS reply this parses; the
        # pair is the observation, so a lone counter is refused rather than
        # reported beside an invented partner.
        self.assertEqual(parse_can_status("T:00"), (None, None))
        self.assertEqual(parse_can_status("R:00"), (None, None))

    def test_a_field_wider_than_the_adapter_prints_is_not_reinterpreted(self):
        # Two characters per counter is the shape ATCS prints.  A wider field
        # is some other reply, and cropping it to two digits would turn text
        # nobody parsed into a plausible error count.
        self.assertEqual(parse_can_status("T:001 R:002"), (None, None))

    def test_the_width_rule_applies_to_the_trailing_counter_too(self):
        # The transmit field is bounded by the "R:" that has to follow it; the
        # receive field is bounded by nothing but the end of the reply, so it
        # is the one that silently crops.  "T:00 R:002" must be refused for the
        # same reason as "T:001 R:002" and not reported as (0, 0).
        self.assertEqual(parse_can_status("T:00 R:002"), (None, None))
        self.assertEqual(parse_can_status("T:00 R:00A"), (None, None))
        self.assertEqual(parse_can_status("T:00 R:00"), (0, 0))


def parse_reply_from_frames(frame: bytes):
    """Wrap a single reassembled frame so a decoder can be pointed at it."""
    from hummer_obd.decode import AdapterReply

    return AdapterReply(raw="", lines=[], frames=[frame], status="ok")


if __name__ == "__main__":
    unittest.main()
