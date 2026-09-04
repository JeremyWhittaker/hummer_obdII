"""Comparing two passive captures, and the empty case that is the real one.

The tool exists for a measurement this vehicle has not yet produced: on
2026-09-04 a thirty-second capture at the connector returned zero bytes. So the
case these tests care most about is not the rich diff -- it is that two empty
captures compare to "there is nothing here", exit 0, and say why.

The transcripts are built with the real `RawLog` writer rather than
hand-written JSON, so a change to the record format breaks these tests instead
of being silently tolerated by a parser written against a format that moved.
"""

import ast
import json
import tempfile
import unittest
from pathlib import Path

from hummer_obd import passive_diff
from hummer_obd.passive_diff import compare, format_report, parse_frames, read_capture
from hummer_obd.rawlog import RawLog


def write_capture(path, *, label, stream=b"", residue=b"", drain=b"\r\r>",
                  elapsed=30.0):
    """A transcript shaped exactly like `monitor.capture()` produces one."""
    with RawLog(path, session_id="t", fsync=False, meta={"label": label}) as log:
        log.write_event("transmit_manifest", {"setup": ["ATZ"], "stream": "STMA"})
        log.log_tx(b"ATZ\r", note="reset")
        if residue:
            log.log_rx(residue, note="pre-capture residue")
        log.write_event("capture_start", {"command": "STMA", "max_seconds": elapsed,
                                          "max_bytes": 500000})
        log.log_tx(b"STMA\r", note="start monitoring (STMA)")
        if stream:
            log.log_rx(stream, note="capture STMA")
        log.log_tx(b"\r", note="monitor stop character")
        if drain:
            log.log_rx(drain, note="post-stop drain")
        log.write_event("capture_end", {"bytes": len(stream), "records": 1,
                                        "elapsed_s": elapsed,
                                        "stop_reason": "duration",
                                        "stop_acknowledged": True})
    return path


class TestTheEmptyCaseIsTheRealCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_the_stop_acknowledgement_is_not_counted_as_vehicle_traffic(self):
        """The bug the real 2026-09-04 transcript exposed on first run.

        `monitor.py` logs the adapter's reply to the stop character *before* it
        writes `capture_end`, so "every rx record between capture_start and
        capture_end" sweeps in ten bytes of adapter prompt -- and a genuinely
        empty capture reads as non-empty. Selecting on the note the stream
        writer actually uses is precise where a window is not.
        """
        p = write_capture(self.dir / "a.jsonl", label="baseline", stream=b"")
        cap = read_capture(str(p))
        self.assertEqual(cap.rx_bytes, 0)
        self.assertEqual(cap.drain_bytes, 3, "the prompt should be counted apart")
        self.assertTrue(cap.empty)

    def test_bytes_waiting_before_the_capture_are_counted_separately(self):
        # They are unsolicited traffic and worth keeping, but they belong to the
        # interval before this capture, not to it.
        p = write_capture(self.dir / "b.jsonl", label="x", residue=b"LEFTOVER")
        cap = read_capture(str(p))
        self.assertEqual(cap.residue_bytes, 8)
        self.assertEqual(cap.rx_bytes, 0)

    def test_two_empty_captures_say_so_and_exit_zero(self):
        a = write_capture(self.dir / "base.jsonl", label="parked baseline")
        b = write_capture(self.dir / "evt.jsonl", label="door opened")
        report = format_report(compare(read_capture(str(a)), read_capture(str(b))))
        self.assertIn("Both captures are empty", report)
        self.assertIn("measurement, not a failure", report)
        self.assertEqual(passive_diff.main([str(a), str(b), "--quiet"]), 0)

    def test_the_empty_report_states_what_it_does_not_establish(self):
        a = write_capture(self.dir / "1.jsonl", label="a")
        b = write_capture(self.dir / "2.jsonl", label="b")
        report = format_report(compare(read_capture(str(a)), read_capture(str(b))))
        self.assertIn("internal networks", report)
        self.assertIn("only", report.lower())


class TestFrameParsing(unittest.TestCase):
    def test_a_29_bit_identifier_and_its_payload(self):
        frames, unparsed = parse_frames(b"18DAF117 03 62 27 C6\r")
        self.assertEqual(frames, {"18DAF117": ["036227C6"]})
        self.assertEqual(unparsed, [])

    def test_an_11_bit_identifier(self):
        frames, _ = parse_frames(b"7E8 03 41 00\r")
        self.assertEqual(frames, {"7E8": ["034100"]})

    def test_prompts_and_status_words_are_not_frames(self):
        frames, unparsed = parse_frames(b">\rOK\r\r>\r")
        self.assertEqual(frames, {})
        self.assertEqual(unparsed, [])

    def test_a_line_the_parser_does_not_understand_is_kept(self):
        # Discarding it would throw away exactly the anomaly worth noticing.
        _frames, unparsed = parse_frames(b"BUS INIT ERROR\r")
        self.assertEqual(unparsed, ["BUSINITERROR"])

    def test_an_identifier_with_no_payload_is_not_a_frame(self):
        frames, unparsed = parse_frames(b"18DAF117\r")
        self.assertEqual(frames, {})
        self.assertIn("18DAF117", unparsed)


class TestWhatADiffReports(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def diff(self, base_stream, event_stream):
        a = write_capture(self.dir / "a.jsonl", label="baseline", stream=base_stream)
        b = write_capture(self.dir / "b.jsonl", label="event", stream=event_stream)
        return compare(read_capture(str(a)), read_capture(str(b)))

    def test_an_identifier_only_in_the_event_is_surfaced(self):
        result = self.diff(b"18DAF117 01 02\r", b"18DAF117 01 02\r1FF00001 AA BB\r")
        appeared = [r for r in result["identifiers"] if r["only_in"] == "event"]
        self.assertEqual([r["id"] for r in appeared], ["1FF00001"])
        self.assertIn("present ONLY during the event", format_report(result))

    def test_an_identifier_that_stopped_is_surfaced(self):
        result = self.diff(b"1FF00002 01\r18DAF117 01\r", b"18DAF117 01\r")
        gone = [r for r in result["identifiers"] if r["only_in"] == "baseline"]
        self.assertEqual([r["id"] for r in gone], ["1FF00002"])

    def test_a_moving_payload_byte_is_located(self):
        result = self.diff(b"1FF00003 00 11 22\r", b"1FF00003 00 99 22\r")
        row = [r for r in result["identifiers"] if r["id"] == "1FF00003"][0]
        self.assertEqual(row["changed_bytes"], [1], "byte 1 is the one that moved")
        self.assertEqual(row["new_payloads"], ["009922"])
        self.assertEqual(row["baseline_count"], 1)
        self.assertEqual(row["event_count"], 1)

    def test_identical_captures_report_nothing_changed(self):
        result = self.diff(b"1FF00004 42\r", b"1FF00004 42\r")
        self.assertFalse(result["both_empty"])
        self.assertIn("nothing distinguishes them", format_report(result))

    def test_the_report_refuses_to_call_a_lead_a_command(self):
        result = self.diff(b"18DAF117 01\r", b"18DAF117 01\r1FF00005 FF\r")
        report = format_report(result)
        self.assertIn("LEAD, not a command", report)
        self.assertIn("does not replay frames", report)
        self.assertIn("not bus load", report)

    def test_json_output_is_machine_readable(self):
        a = write_capture(self.dir / "j1.jsonl", label="a", stream=b"1FF00006 01\r")
        b = write_capture(self.dir / "j2.jsonl", label="b", stream=b"1FF00006 02\r")
        out = self.dir / "diff.json"
        self.assertEqual(
            passive_diff.main([str(a), str(b), "--quiet", "--json", str(out)]), 0)
        data = json.loads(out.read_text())
        self.assertIn("identifiers", data)
        self.assertEqual(data["baseline"]["label"], "a")


class TestItNeverTouchesTheVehicle(unittest.TestCase):
    FORBIDDEN = {"serial", "hummer_obd.transport", "hummer_obd.monitor",
                 "hummer_obd.collector", "hummer_obd.drive", "hummer_obd.probe"}

    def test_it_imports_nothing_that_can_transmit(self):
        with open(passive_diff.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    names.add(f"hummer_obd.{node.module}" if node.module else "hummer_obd")
                elif node.module:
                    names.add(node.module)
        self.assertEqual(sorted(names & self.FORBIDDEN), [])

    def test_a_missing_transcript_is_an_error_not_a_traceback(self):
        self.assertEqual(
            passive_diff.main(["/nonexistent/a.jsonl", "/nonexistent/b.jsonl"]), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
