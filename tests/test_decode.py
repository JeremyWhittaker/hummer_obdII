"""Decoder tests use real adapter response shapes, including multi-frame."""

import unittest

from hummer_obd.decode import (
    decode_ascii_item,
    decode_ascii_items,
    decode_cvns,
    decode_dtcs,
    decode_pid,
    decode_vin,
    mask_vin,
    PID_DECODERS,
    parse_reply,
    supported_pids,
    supported_service09_pids,
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


def parse_reply_from_frames(frame: bytes):
    """Wrap a single reassembled frame so a decoder can be pointed at it."""
    from hummer_obd.decode import AdapterReply

    return AdapterReply(raw="", lines=[], frames=[frame], status="ok")


if __name__ == "__main__":
    unittest.main()
