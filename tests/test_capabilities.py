"""Capabilities report: it must describe the node without ever touching it.

The property under test is mostly a negative one — no serial device is opened,
no database is written, no transcript payload and no identifier escapes — so
most of these tests are traps rather than assertions about output.
"""

import builtins
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hummer_obd import capabilities
from hummer_obd.capabilities import (
    SCHEMA,
    _sanitize,
    build_report,
    main,
    open_database_readonly,
    render_text,
)
from hummer_obd.config import load_config
from hummer_obd.decode import PidValue
from hummer_obd.rawlog import RawLog
from hummer_obd.storage import Storage

CONFIG = """
[adapter]
device = "{device}"
bluetooth_address = "00:04:3E:1A:2B:3C"

[collector]
enabled = true
pids = ["010D", "011F", "0142"]
poll_interval_s = 2.0
database = "data/hummer_obd.sqlite3"
raw_log_dir = "logs/raw"

[upload]
enabled = false

[display]
enabled = true
"""

#: A byte pattern that exists only inside the raw transcript.  If it ever shows
#: up in a report, the report is leaking payload.
TRANSCRIPT_MARKER = b"MARKERPAYLOAD"

#: Realistic-looking VIN (17 characters, no I/O/Q) used to prove re-masking.
FAKE_VIN = "1GT40FDA5RU100123"


def write_config(root: Path, device: Path) -> Path:
    (root / "config").mkdir(parents=True, exist_ok=True)
    path = root / "config" / "hummer.toml"
    path.write_text(CONFIG.format(device=device))
    return path


def write_evidence(root: Path) -> None:
    """Two probe summaries of different quality, plus one unrelated JSON file."""
    evidence = root / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)

    thin = {
        "session": "probe-20260901T120000Z",
        "device": "/dev/rfcomm0",
        "adapter": {"ATI": "OBDLink MX+ r5.7", "protocol": "", "AT@2": ""},
        "supported_pids": [],
        "samples": {},
    }
    rich = {
        "session": "probe-20260901T131900Z",
        "device": "/dev/rfcomm0",
        "adapter": {
            "ATI": "OBDLink MX+ r5.7",
            "AT@2": "OBDLink MX+ SN 123456786122",
            "protocol": "ISO 15765-4 (CAN 11/500)",
            "protocol_number": "6",
        },
        "supported_pids": ["01", "0D", "1F", "20", "42"],
        "samples": {
            "0D": {"name": "vehicle speed", "value": 0.0, "unit": "km/h",
                   "status": "ok", "raw": "410d00"},
            "42": {"name": "control module voltage", "value": 14.2, "unit": "V",
                   "status": "ok", "raw": "41428ac0"},
        },
        "dtcs": {"03": {"codes": [], "status": "ok", "lines": ["43 00"]}},
        "service09": {"0A": {"value": "ECM", "status": "ok"}},
        "vin_masked": "1GT************23 (len=17)",
        "vin_status": "ok",
    }
    (evidence / "probe-thin.json").write_text(json.dumps(thin))
    (evidence / "probe-rich.json").write_text(json.dumps(rich))
    (evidence / "not-a-probe.json").write_text(json.dumps({"note": "provisioning output"}))
    # The *rich* probe is the older one on purpose.  If it were the newer file,
    # "prefer the richest answer" and "last file wins" would agree and the merge
    # test would prove nothing; this way only a merge that actually compares the
    # answers survives.
    os.utime(evidence / "probe-rich.json", (1_000_000, 1_000_000))
    os.utime(evidence / "probe-thin.json", (2_000_000, 2_000_000))


def write_raw_log(root: Path) -> Path:
    path = root / "logs" / "raw" / "probe-20260901T131900Z.jsonl"
    with RawLog(path, "probe-20260901T131900Z", fsync=False, meta={"probe": True}) as log:
        log.log_tx(b"010D\r")
        log.log_rx(TRANSCRIPT_MARKER + b"\r>")
    # A power cut truncates the final record; the report must count it, not choke.
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"seq": 9, "kind": "io"\n')
    return path


def write_database(root: Path) -> Path:
    path = root / "data" / "hummer_obd.sqlite3"
    with Storage(path) as store:
        sid = store.start_session(
            "collect-20260901T140000Z",
            adapter_id="OBDLink MX+ r5.7",
            protocol="ISO 15765-4 (CAN 11/500)",
            notes="collector",
        )
        for value in (10.0, 30.0, 20.0):
            store.add_sample(sid, PidValue("0D", "vehicle speed", value, "km/h", "410d00", "ok"))
        store.add_sample(sid, PidValue("1F", "run time", 41.0, "s", "411f0029", "ok"))
        # Rewrite the timestamps so the newest reading is *not* the last row
        # inserted.  "Newest" has to mean newest, not "highest id".
        ids = [row["id"] for row in store.conn.execute(
            "SELECT id FROM samples WHERE pid='0D' ORDER BY id")]
        for row_id, ts in zip(ids, ("2026-09-01T10:00:00+00:00",
                                    "2026-09-01T12:00:00+00:00",
                                    "2026-09-01T11:00:00+00:00")):
            store.conn.execute("UPDATE samples SET ts=? WHERE id=?", (ts, row_id))
        store.add_dtc_read(sid, "03", [], "4300 4300")
        store.add_dtc_read(sid, "07", ["P0143"], "4701430143")
        # Written by an older build that did not mask: the report must still
        # refuse to publish it.
        store.add_vehicle_info(sid, "VIN", FAKE_VIN, "see raw log")
        store.end_session(sid)
    return path


def write_coverage_session(store: Storage, session_uid: str, timestamps: list, **session_kwargs) -> int:
    """A session with one sample per timestamp in *timestamps* (already ISO-8601).

    Mirrors :func:`write_database`'s own trick of writing rows and then
    rewriting their ``ts`` column directly: the sample value is irrelevant to
    coverage, only the timestamp is, so nothing here decodes anything real.
    """
    sid = store.start_session(session_uid, **session_kwargs)
    row_ids = [
        store.add_sample(sid, PidValue("0D", "vehicle speed", 0.0, "km/h", "410d00", "ok"))
        for _ in timestamps
    ]
    for row_id, ts in zip(row_ids, timestamps):
        store.conn.execute("UPDATE samples SET ts=? WHERE id=?", (ts, row_id))
    store.end_session(sid)
    return sid


class CoverageFixture(unittest.TestCase):
    """A bare project root (config only) that individual tests fill with a
    hand-built database, so each test controls its own session/sample layout
    exactly instead of inheriting the shared fixture's timestamps.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.device = self.root / "fake-rfcomm0"
        self.device.write_bytes(b"")
        self.config_path = write_config(self.root, self.device)
        self.cfg = load_config(self.config_path, root=self.root)
        self.db_path = self.root / "data" / "hummer_obd.sqlite3"

    def tearDown(self):
        self._tmp.cleanup()

    def build(self, **kwargs):
        with mock.patch.object(capabilities.shutil, "which", return_value=None):
            return build_report(self.cfg, **kwargs)


class TestCoverageSection(CoverageFixture):
    def test_two_sessions_report_the_gap_between_them(self):
        with Storage(self.db_path) as store:
            write_coverage_session(store, "sess-a", [
                "2026-09-01T10:00:00+00:00", "2026-09-01T10:01:00+00:00",
            ])
            write_coverage_session(store, "sess-b", [
                "2026-09-01T10:06:00+00:00", "2026-09-01T10:07:00+00:00",
            ])
        coverage = self.build()["sections"]["coverage"]
        self.assertEqual(coverage["sessions"]["count"], 2)
        self.assertEqual(len(coverage["gaps"]), 1)
        gap = coverage["gaps"][0]
        self.assertEqual(gap["after_session"], "sess-a")
        self.assertEqual(gap["before_session"], "sess-b")
        self.assertAlmostEqual(gap["seconds"], 300.0)
        self.assertEqual(gap["start"], "2026-09-01T10:01:00+00:00")
        self.assertEqual(gap["end"], "2026-09-01T10:06:00+00:00")
        self.assertAlmostEqual(coverage["longest_gap_s"], 300.0)
        self.assertNotIn("note", coverage)  # a real gap was found; nothing to caveat

    def test_a_gap_shorter_than_the_threshold_is_not_reported(self):
        with Storage(self.db_path) as store:
            write_coverage_session(store, "sess-a", [
                "2026-09-01T10:00:00+00:00", "2026-09-01T10:00:10+00:00",
            ])
            write_coverage_session(store, "sess-b", [
                "2026-09-01T10:00:40+00:00", "2026-09-01T10:00:50+00:00",
            ])
        coverage = self.build()["sections"]["coverage"]  # default threshold: 60s
        self.assertEqual(coverage["gaps"], [])
        self.assertIn("no gaps longer than 60.0s", coverage["note"])

    def test_the_gap_threshold_is_configurable(self):
        with Storage(self.db_path) as store:
            write_coverage_session(store, "sess-a", [
                "2026-09-01T10:00:00+00:00", "2026-09-01T10:00:10+00:00",
            ])
            write_coverage_session(store, "sess-b", [
                "2026-09-01T10:00:40+00:00", "2026-09-01T10:00:50+00:00",
            ])
        coverage = self.build(gap_threshold_s=10.0)["sections"]["coverage"]
        self.assertEqual(len(coverage["gaps"]), 1)
        self.assertAlmostEqual(coverage["gaps"][0]["seconds"], 30.0)
        self.assertEqual(coverage["gap_threshold_s"], 10.0)

    def test_coverage_ratio_for_a_known_layout(self):
        with Storage(self.db_path) as store:
            # session a: 100s of samples, then a 50s gap, then 50s of session b.
            write_coverage_session(store, "a", [
                "2026-09-01T09:00:00+00:00", "2026-09-01T09:01:40+00:00",
            ])
            write_coverage_session(store, "b", [
                "2026-09-01T09:02:30+00:00", "2026-09-01T09:03:20+00:00",
            ])
        coverage = self.build()["sections"]["coverage"]
        self.assertAlmostEqual(coverage["observed_seconds"], 150.0)
        self.assertAlmostEqual(coverage["total_span_seconds"], 200.0)
        self.assertAlmostEqual(coverage["coverage_ratio"], 0.75)

    def test_a_single_session_reports_no_gaps(self):
        with Storage(self.db_path) as store:
            write_coverage_session(store, "only", [
                "2026-09-01T09:00:00+00:00", "2026-09-01T09:00:10+00:00",
            ])
        coverage = self.build()["sections"]["coverage"]
        self.assertEqual(coverage["sessions"]["count"], 1)
        self.assertEqual(coverage["gaps"], [])
        self.assertIsNone(coverage["longest_gap_s"])
        self.assertIn("only one session", coverage["note"])

    def test_zero_and_one_sample_sessions_have_zero_duration(self):
        with Storage(self.db_path) as store:
            write_coverage_session(store, "empty", [])
            write_coverage_session(store, "single", ["2026-09-01T09:00:00+00:00"])
        coverage = self.build()["sections"]["coverage"]
        recent = {item["session_uid"]: item for item in coverage["sessions"]["recent"]}
        self.assertEqual(recent["empty"]["sample_count"], 0)
        self.assertEqual(recent["single"]["sample_count"], 1)
        self.assertAlmostEqual(coverage["observed_seconds"], 0.0)

    def test_an_empty_root_with_no_database_still_produces_the_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(root=root)
            cfg.adapter.device = str(root / "no-such-device")
            with mock.patch.object(capabilities.shutil, "which", return_value=None):
                report = build_report(cfg)
        coverage = report["sections"]["coverage"]
        self.assertEqual(coverage["sessions"], {"count": 0, "recent": [], "omitted": 0})
        self.assertEqual(coverage["gaps"], [])
        self.assertIsNone(coverage["coverage_ratio"])
        self.assertIn("no collector database", coverage["note"])
        self.assertIn("### collection coverage", render_text(report))

    def test_overlapping_sessions_do_not_produce_a_negative_gap(self):
        with Storage(self.db_path) as store:
            write_coverage_session(store, "a", [
                "2026-09-01T09:00:00+00:00", "2026-09-01T09:03:20+00:00",  # 0..200s
            ])
            write_coverage_session(store, "b", [
                "2026-09-01T09:01:40+00:00", "2026-09-01T09:05:00+00:00",  # 100..300s, overlaps a
            ])
        coverage = self.build()["sections"]["coverage"]
        self.assertEqual(coverage["gaps"], [])
        self.assertIsNone(coverage["longest_gap_s"])
        for gap in coverage["gaps"]:
            self.assertGreaterEqual(gap["seconds"], 0.0)

    def test_an_unparsable_timestamp_does_not_crash_the_report(self):
        with Storage(self.db_path) as store:
            sid = store.start_session("bad-ts")
            row_id = store.add_sample(
                sid, PidValue("0D", "vehicle speed", 0.0, "km/h", "410d00", "ok")
            )
            store.conn.execute("UPDATE samples SET ts=? WHERE id=?", ("not-a-timestamp", row_id))
            store.end_session(sid)
        coverage = self.build()["sections"]["coverage"]  # must not raise
        self.assertEqual(coverage["sessions"]["count"], 1)
        self.assertEqual(coverage["sessions"]["recent"][0]["sample_count"], 1)
        self.assertIsNone(coverage["first_sample"])
        self.assertIsNone(coverage["last_sample"])
        self.assertAlmostEqual(coverage["observed_seconds"], 0.0)

    def test_a_timezone_naive_timestamp_does_not_crash_the_report(self):
        """A naive value parses cleanly but cannot be compared to an aware one.

        ``datetime.fromisoformat`` accepts ``"...T10:00:00"`` -- no ``+00:00``
        -- without raising, so this is not caught by the plain "unparsable"
        case; only mixing it into a comparison against an aware timestamp
        raises, later and non-obviously.  It has to be excluded before that.
        """
        with Storage(self.db_path) as store:
            write_coverage_session(store, "mixed", [
                "2026-09-01T09:00:00+00:00",  # aware
                "2026-09-01T09:00:10",        # naive: no offset
            ])
        coverage = self.build()["sections"]["coverage"]  # must not raise
        self.assertEqual(coverage["sessions"]["recent"][0]["sample_count"], 2)
        self.assertEqual(coverage["first_sample"], "2026-09-01T09:00:00+00:00")
        self.assertEqual(coverage["last_sample"], "2026-09-01T09:00:00+00:00")
        self.assertAlmostEqual(coverage["observed_seconds"], 0.0)

    def test_the_recent_session_list_is_capped_at_ten_with_an_honest_omitted_count(self):
        with Storage(self.db_path) as store:
            for i in range(12):
                write_coverage_session(store, f"sess-{i:02d}", [
                    f"2026-09-01T{9 + i:02d}:00:00+00:00",
                ])
        coverage = self.build()["sections"]["coverage"]
        self.assertEqual(coverage["sessions"]["count"], 12)
        recent = coverage["sessions"]["recent"]
        self.assertEqual(len(recent), 10)
        self.assertEqual(coverage["sessions"]["omitted"], 2)
        # the two oldest sessions are the ones left out, never a silent drop.
        self.assertEqual(recent[0]["session_uid"], "sess-02")
        self.assertEqual(recent[-1]["session_uid"], "sess-11")

    def test_ended_at_reports_open_for_a_session_never_closed(self):
        with Storage(self.db_path) as store:
            store.start_session("still-running")
        coverage = self.build()["sections"]["coverage"]
        self.assertEqual(coverage["sessions"]["recent"][0]["ended_at"], "open")


class CapabilitiesFixture(unittest.TestCase):
    """A project root with a config, evidence, a transcript and a database."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.device = self.root / "fake-rfcomm0"
        self.device.write_bytes(b"")
        self.config_path = write_config(self.root, self.device)
        write_evidence(self.root)
        self.raw_log = write_raw_log(self.root)
        self.database = write_database(self.root)
        self.cfg = load_config(self.config_path, root=self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def build(self, **kwargs):
        """Build a report with systemd stubbed out, for speed and determinism."""
        with mock.patch.object(capabilities.shutil, "which", return_value=None):
            return build_report(self.cfg, **kwargs)


class TestNoSerialAccess(CapabilitiesFixture):
    def test_the_report_never_opens_the_adapter_device(self):
        device = str(self.device)
        attempts = []
        seen = []
        real_open = builtins.open

        def guarded_open(file, *args, **kwargs):
            seen.append(str(file))
            if str(file) == device:
                attempts.append(str(file))
                raise AssertionError(f"the capabilities report opened {file}")
            return real_open(file, *args, **kwargs)

        def explode(*args, **kwargs):
            raise AssertionError("the capabilities report constructed a serial port")

        patches = [mock.patch("builtins.open", guarded_open), mock.patch("io.open", guarded_open)]
        try:
            import serial  # pyserial is a project dependency; poison it if present
        except ImportError:
            serial = None
        if serial is not None:
            patches.append(mock.patch.object(serial, "Serial", explode))
            if hasattr(serial, "serial_for_url"):
                patches.append(mock.patch.object(serial, "serial_for_url", explode))

        with mock.patch.object(capabilities.shutil, "which", return_value=None):
            for patcher in patches:
                patcher.start()
            try:
                report = build_report(self.cfg)
            finally:
                for patcher in reversed(patches):
                    patcher.stop()

        self.assertEqual(report["schema"], SCHEMA)
        self.assertEqual(attempts, [])
        # The guard has to have been live for that emptiness to mean anything:
        # the report does open the transcript and the evidence files.
        self.assertTrue(any(name.endswith(".jsonl") for name in seen), seen)
        self.assertTrue(any(name.endswith(".json") for name in seen), seen)
        device_info = report["sections"]["node"]["adapter_device"]
        self.assertTrue(device_info["exists"])      # it did stat the path
        self.assertFalse(device_info["opened"])     # and it did not open it

    def test_the_module_source_does_not_reach_for_the_serial_stack(self):
        # "serial" not in sys.modules is NOT a valid check: pyserial may already
        # have been imported by another test module in the same interpreter, and
        # a module this one imports could pull it in later.  The honest check is
        # what this file is allowed to contain.
        source = Path(capabilities.__file__).read_text()
        self.assertIsNone(re.search(r"^\s*(import serial|from serial)", source, re.M))
        self.assertNotIn("SerialTransport", source)
        self.assertNotIn("AdapterSession", source)

    def test_transcript_payload_never_reaches_the_report(self):
        report = self.build()
        blob = json.dumps(report) + render_text(report)
        self.assertNotIn(TRANSCRIPT_MARKER.decode(), blob)
        self.assertNotIn(TRANSCRIPT_MARKER.hex(), blob)


class TestSanitize(unittest.TestCase):
    def test_redaction_table(self):
        cases = [
            ("adapter 00:04:3E:1A:2B:3C paired", "adapter 00:04:3E:XX:XX:XX paired"),
            # An adapter's own device-description string reports it this way.
            ("OBDLink MX BT 00-04-3E-1A-2B-3C", "OBDLink MX BT 00-04-3E-XX-XX-XX"),
            # A hyphen before a colon-separated MAC must not defeat the match.
            ("bt-00:04:3E:1A:2B:3C", "bt-00:04:3E:XX:XX:XX"),
            ("lan 192.168.1.42 up", "lan [redacted-ipv4] up"),
            ("tailnet 100.64.0.15", "tailnet [redacted-ipv4]"),
            ("v6 fe80::1ff:fe23:4567:890a", "v6 [redacted-ipv6]"),
            ("host hummer-pi.tail9f2c.ts.net", "host [redacted-tailnet-host]"),
            ("host HUMMER-PI.TAIL9F2C.TS.NET", "host [redacted-tailnet-host]"),
            ("serial 123456786122", "serial ********6122"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(_sanitize(raw), expected)

    def test_a_vin_is_masked_not_printed(self):
        out = _sanitize(f"VIN {FAKE_VIN} read")
        self.assertNotIn(FAKE_VIN, out)
        self.assertIn("1GT", out)
        self.assertIn("*", out)

    def test_harmless_text_survives(self):
        for text in ("python 3.11.2", "ISO 15765-4 (CAN 11/500)", "010D", "collect-20260901T140000Z"):
            with self.subTest(text=text):
                self.assertEqual(_sanitize(text), text)

    def test_a_sha256_digest_is_not_mangled(self):
        digest = hashlib.sha256(b"raw log").hexdigest()
        self.assertEqual(_sanitize(digest), digest)

    def test_a_credential_in_the_upload_endpoint_is_never_published(self):
        """A token belongs in ``upload.token_file``, but a URL can carry one.

        The report is meant to be pasted into a handover note and is un-ignored
        by ``.gitignore``, so a misconfigured endpoint must not turn it into a
        credential leak.  Nothing in :func:`_sanitize` recognises a secret by
        shape, so the endpoint itself has to be reduced.
        """
        secret = "sk-live-ABCDEF0123456789"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root, root / "fake-rfcomm0")
            cfg = load_config(root / "config" / "hummer.toml", root=root)
            cfg.upload.endpoint = f"https://svc:hunter2@ingest.example.com/v1?token={secret}"
            with mock.patch.object(capabilities.shutil, "which", return_value=None):
                report = build_report(cfg)
        blob = json.dumps(report) + render_text(report)
        self.assertNotIn(secret, blob)
        self.assertNotIn("hunter2", blob)
        endpoint = report["sections"]["configuration"]["upload"]["endpoint"]
        # Still enough to answer "does anything leave the Pi, and to where".
        self.assertIn("ingest.example.com/v1", endpoint)
        self.assertIn("[redacted-credentials]", endpoint)

    def test_an_ordinary_endpoint_is_reported_as_written(self):
        for raw, expected in (
            ("https://ingest.example.com/v1", "https://ingest.example.com/v1"),
            ("https://ingest.example.com:8443/v1", "https://ingest.example.com:8443/v1"),
            ("", ""),
            ("not-a-url", "not-a-url"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(capabilities._safe_endpoint(raw), expected)

    def test_sanitization_is_applied_to_the_whole_report(self):
        # The configured Bluetooth address is the one identifier guaranteed to
        # be in the fixture's config, so it proves the final pass runs.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root, root / "fake-rfcomm0")
            cfg = load_config(root / "config" / "hummer.toml", root=root)
            cfg.upload.endpoint = "https://192.0.2.10/ingest"
            with mock.patch.object(capabilities.shutil, "which", return_value=None):
                report = build_report(cfg)
        blob = json.dumps(report)
        self.assertNotIn("192.0.2.10", blob)
        self.assertIn("[redacted-ipv4]", report["sections"]["configuration"]["upload"]["endpoint"])


class TestSections(CapabilitiesFixture):
    def test_safety_gate_is_interrogated_not_described(self):
        gate = self.build()["sections"]["safety_gate"]
        self.assertEqual(gate["allowed_obd_modes"], ["01", "03", "07", "09", "0A"])
        self.assertTrue(gate["all_samples_accepted"])
        self.assertTrue(gate["all_samples_refused"])
        refused = {check["command"]: check for check in gate["checked_refused"]}
        self.assertFalse(refused["04"]["accepted"])
        self.assertFalse(refused["22F190"]["accepted"])
        self.assertFalse(refused["010D;04"]["accepted"])
        self.assertTrue(refused["04"]["detail"])

    def test_evidence_merge_prefers_the_probe_that_learned_something(self):
        evidence = self.build()["sections"]["evidence"]
        self.assertEqual(evidence["summaries"], 2)  # the third file is not a summary
        self.assertNotIn("not-a-probe.json", evidence["sources"].values())
        merged = evidence["merged"]
        # The empty list belongs to the newer file; picking it would mean the
        # merge is ordering by mtime rather than by how much was learned.
        self.assertEqual(merged["supported_pids"], ["01", "0D", "1F", "20", "42"])
        self.assertEqual(merged["protocol"], "ISO 15765-4 (CAN 11/500)")
        self.assertEqual(evidence["sources"]["supported_pids"], "probe-20260901T131900Z")
        # Raw hex from the summary is dropped on the way through.
        self.assertNotIn("raw", merged["samples"]["0D"])
        self.assertEqual(merged["samples"]["0D"]["value"], 0.0)

    def test_raw_log_metadata_is_hashed_and_counted(self):
        logs = self.build()["sections"]["raw_logs"]
        self.assertEqual(logs["totals"]["files"], 1)
        entry = logs["files"][0]
        self.assertEqual(entry["sha256"], hashlib.sha256(self.raw_log.read_bytes()).hexdigest())
        self.assertEqual(entry["tx"], 1)
        self.assertEqual(entry["rx"], 1)
        self.assertEqual(entry["events"], 2)   # session_start and session_end
        self.assertEqual(entry["corrupt"], 1)  # the truncated final line

    def test_latest_value_is_the_newest_row_not_the_last_written(self):
        latest = self.build()["sections"]["database"]["latest_values"]
        self.assertEqual(latest["0D"]["value"], 30.0)
        self.assertEqual(latest["0D"]["ts"], "2026-09-01T12:00:00+00:00")
        self.assertTrue(latest["1F"]["recorded"])
        # Configured, polled, never answered: the most useful line in the report.
        self.assertFalse(latest["42"]["recorded"])
        self.assertEqual(latest["42"]["request"], "0142")

    def test_database_summary_counts_dtcs_sessions_and_the_upload_queue(self):
        db = self.build()["sections"]["database"]
        self.assertTrue(db["present"])
        self.assertEqual(db["schema_version"], 1)
        self.assertEqual(db["tables"]["samples"], 4)
        self.assertEqual(db["upload_queue_depth"], 4)  # upload is off; nothing is stamped
        self.assertEqual(db["dtc_summary"]["03"]["reads"], 1)
        self.assertFalse(db["dtc_summary"]["03"]["ever_reported_codes"])
        self.assertTrue(db["dtc_summary"]["07"]["ever_reported_codes"])
        self.assertEqual(db["dtc_summary"]["03"]["distinct_reply_frames"], 1)  # "4300 4300"
        self.assertEqual(len(db["sessions"]), 1)
        self.assertEqual(db["sessions"][0]["protocol"], "ISO 15765-4 (CAN 11/500)")

    def test_an_unmasked_vin_in_the_database_is_masked_on_the_way_out(self):
        report = self.build()
        info = report["sections"]["database"]["vehicle_info"]
        self.assertEqual(info[0]["item"], "VIN")
        self.assertNotIn(FAKE_VIN, json.dumps(report))
        self.assertNotIn(FAKE_VIN, render_text(report))
        self.assertIn("*", info[0]["value"])

    def test_services_are_unknown_when_systemctl_is_absent(self):
        with mock.patch.object(capabilities.shutil, "which", return_value=None):
            services = build_report(self.cfg)["sections"]["services"]
        self.assertFalse(services["systemctl"])
        for unit in capabilities.SERVICE_UNITS:
            self.assertEqual(services["units"][unit], {"enabled": "unknown", "active": "unknown"})

    def test_systemctl_is_queried_read_only_and_never_with_sudo(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(stdout="enabled\n", stderr="", returncode=0)

        with mock.patch.object(capabilities.shutil, "which", return_value="/usr/bin/systemctl"), \
                mock.patch.object(capabilities.subprocess, "run", fake_run):
            services = build_report(self.cfg)["sections"]["services"]
        self.assertEqual(services["units"]["hummer-collector"]["enabled"], "enabled")
        for argv in calls:
            self.assertEqual(argv[0], "/usr/bin/systemctl")
            self.assertIn(argv[1], ("is-enabled", "is-active"))
            self.assertNotIn("sudo", argv)

    def test_a_failing_systemctl_reports_unknown(self):
        with mock.patch.object(capabilities.shutil, "which", return_value="/usr/bin/systemctl"), \
                mock.patch.object(capabilities.subprocess, "run", side_effect=OSError("boom")):
            services = build_report(self.cfg)["sections"]["services"]
        self.assertEqual(services["units"]["hummer-display"]["active"], "unknown")


class TestDeferred(CapabilitiesFixture):
    def test_the_deferred_list_always_names_the_big_absences(self):
        deferred = {item["capability"]: item for item in self.build()["sections"]["deferred"]}
        for capability in ("gps_location", "onstar_cloud", "mode22_enhanced_pids",
                           "remote_commands", "collector_autostart", "raw_log_upload"):
            self.assertIn(capability, deferred)
            self.assertTrue(deferred[capability]["reason"])

    def test_collector_autostart_is_derived_from_config_and_systemd(self):
        with mock.patch.object(capabilities.shutil, "which", return_value=None):
            report = build_report(self.cfg)
        item = next(i for i in report["sections"]["deferred"] if i["capability"] == "collector_autostart")
        self.assertEqual(item["status"], "deferred")  # enabled in config, unit state unknown
        self.assertIn("collector.enabled is true", item["reason"])

        def fake_run(argv, **kwargs):
            answer = "enabled" if argv[1] == "is-enabled" else "active"
            return mock.Mock(stdout=answer + "\n", stderr="", returncode=0)

        with mock.patch.object(capabilities.shutil, "which", return_value="/usr/bin/systemctl"), \
                mock.patch.object(capabilities.subprocess, "run", fake_run):
            report = build_report(self.cfg)
        item = next(i for i in report["sections"]["deferred"] if i["capability"] == "collector_autostart")
        self.assertEqual(item["status"], "available")
        self.assertIn("enabled/active", item["reason"])


class TestReadOnlyDatabase(CapabilitiesFixture):
    def test_the_reports_own_connection_cannot_write(self):
        conn = open_database_readonly(self.database)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO events(ts, kind, detail) VALUES ('now','x','y')")
        finally:
            conn.close()

    def test_reading_the_database_leaves_it_byte_identical(self):
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        self.build()
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)


class TestEmptyRoot(unittest.TestCase):
    """A brand new node has no evidence, no logs and no database."""

    def test_report_is_still_produced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(root=root)
            # Point at a device inside the fixture.  The default is
            # /dev/rfcomm0, which really exists on a deployed node, so leaving
            # it alone would make this assertion a statement about the host
            # rather than about the report.
            cfg.adapter.device = str(root / "no-such-device")
            with mock.patch.object(capabilities.shutil, "which", return_value=None):
                report = build_report(cfg)
            sections = report["sections"]
            for name in ("node", "safety_gate", "configuration", "evidence",
                         "raw_logs", "database", "coverage", "services", "deferred"):
                self.assertIn(name, sections)
            self.assertEqual(sections["evidence"]["summaries"], 0)
            self.assertEqual(sections["raw_logs"]["files"], [])
            self.assertFalse(sections["database"]["present"])
            self.assertIn("no collector database", sections["coverage"]["note"])
            self.assertFalse(sections["node"]["adapter_device"]["exists"])
            self.assertIn("### not available / deferred", render_text(report))

    def test_an_existing_device_is_reported_present_but_still_never_opened(self):
        """The deployed node is the case that matters: the device is there.

        Reporting ``exists`` has to come from a stat, never from an open, so
        this asserts both halves at once.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = root / "rfcomm0-stand-in"
            device.write_bytes(b"")
            cfg = load_config(root=root)
            cfg.adapter.device = str(device)
            real_open = builtins.open

            def guard(path, *args, **kwargs):
                if str(path) == str(device):
                    raise AssertionError("the report opened the adapter device")
                return real_open(path, *args, **kwargs)

            with mock.patch.object(capabilities.shutil, "which", return_value=None), \
                    mock.patch.object(builtins, "open", guard):
                report = build_report(cfg)
            device_section = report["sections"]["node"]["adapter_device"]
            self.assertTrue(device_section["exists"])
            self.assertFalse(device_section["opened"])

    def test_cli_exits_zero_on_an_empty_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(capabilities.shutil, "which", return_value=None), \
                    mock.patch("sys.stdout", new=io.StringIO()):
                rc = main(["--root", tmp, "--no-json"])
        self.assertEqual(rc, 0)


class TestCommandLine(CapabilitiesFixture):
    def test_json_is_written_parses_and_carries_the_schema(self):
        with mock.patch.object(capabilities.shutil, "which", return_value=None), \
                mock.patch("sys.stdout", new=io.StringIO()):
            rc = main(["--root", str(self.root), "--quiet"])
        self.assertEqual(rc, 0)
        written = self.root / "evidence" / "capabilities-latest.json"
        self.assertTrue(written.exists())
        report = json.loads(written.read_text())
        self.assertEqual(report["schema"], SCHEMA)
        self.assertTrue(report["generated_at"])
        self.assertIn("safety_gate", report["sections"])
        # The config on disk is preferred over built-in defaults, and named.
        self.assertEqual(report["sections"]["node"]["config_source"], str(self.config_path))
        # Its own output must not be mistaken for probe evidence next time.
        self.assertEqual(report["sections"]["evidence"]["summaries"], 2)

    def test_a_second_run_ignores_the_previous_report(self):
        with mock.patch.object(capabilities.shutil, "which", return_value=None), \
                mock.patch("sys.stdout", new=io.StringIO()):
            main(["--root", str(self.root), "--quiet"])
            main(["--root", str(self.root), "--quiet"])
        report = json.loads((self.root / "evidence" / "capabilities-latest.json").read_text())
        self.assertEqual(report["sections"]["evidence"]["summaries"], 2)
        self.assertNotIn("capabilities-latest.json", report["sections"]["evidence"]["sources"].values())

    def test_console_output_is_grouped_and_plain(self):
        stdout = io.StringIO()
        with mock.patch.object(capabilities.shutil, "which", return_value=None), \
                mock.patch("sys.stdout", new=stdout):
            rc = main(["--root", str(self.root), "--no-json"])
        text = stdout.getvalue()
        self.assertEqual(rc, 0)
        for heading in ("### node", "### safety gate", "### configuration",
                        "### database (read-only)", "### collection coverage",
                        "### not available / deferred"):
            self.assertIn(heading, text)
        self.assertNotIn("\x1b[", text)  # no colour escapes

    def test_no_json_writes_nothing(self):
        with mock.patch.object(capabilities.shutil, "which", return_value=None), \
                mock.patch("sys.stdout", new=io.StringIO()):
            main(["--root", str(self.root), "--no-json"])
        self.assertFalse((self.root / "evidence" / "capabilities-latest.json").exists())

    def test_an_unreadable_config_exits_two(self):
        broken = self.root / "broken.toml"
        broken.write_text("[collector\n")
        with mock.patch("sys.stderr", new=io.StringIO()):
            rc = main(["--config", str(broken), "--root", str(self.root), "--no-json"])
        self.assertEqual(rc, 2)

    def test_a_missing_config_exits_two(self):
        with mock.patch("sys.stderr", new=io.StringIO()):
            rc = main(["--config", str(self.root / "nope.toml"), "--no-json"])
        self.assertEqual(rc, 2)


class TestRootIsInferredFromTheConfig(unittest.TestCase):
    """``--config`` without ``--root`` must not describe the wrong node.

    Defaulting the root to the working directory made
    ``--config /opt/hummer/config/hummer.toml`` report "nothing has been
    recorded on this node" from anywhere else -- an answer that is wrong in the
    most misleading possible direction, because it looks like a finding.
    """

    def test_the_project_root_comes_from_the_config_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "opt" / "hummer"
            (project / "config").mkdir(parents=True)
            (project / "config" / "hummer.toml").write_text(
                '[collector]\ndatabase = "data/hummer_obd.sqlite3"\n'
            )
            cfg, source = capabilities._load(
                str(project / "config" / "hummer.toml"), None)
            self.assertEqual(Path(cfg.root).resolve(), project.resolve())
            self.assertTrue(source.endswith("hummer.toml"))

    def test_an_explicit_root_still_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "opt" / "hummer"
            (project / "config").mkdir(parents=True)
            (project / "config" / "hummer.toml").write_text("[collector]\n")
            override = Path(tmp) / "elsewhere"
            override.mkdir()
            cfg, _ = capabilities._load(
                str(project / "config" / "hummer.toml"), str(override))
            self.assertEqual(Path(cfg.root), override)



if __name__ == "__main__":
    unittest.main()
