"""The export is a read-only local file writer, and says what it contains."""

import ast
import contextlib
import csv
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hummer_obd import export, storage
from hummer_obd.decode import PidValue, mask_vin
from hummer_obd.export import main
from hummer_obd.storage import Storage

VIN = "1GT40FDA3RU100234"
MAC = "00:04:3E:AA:BB:CC"
IPV4 = "192.168.7.42"
IPV6 = "fd7a:115c:a1e0::53"

EXPORT_TIME = "2026-09-02T00:00:00+00:00"


class ExportFixture(unittest.TestCase):
    """A real Storage database with timestamps a test can reason about."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.db = self.root / "data" / "hummer_obd.sqlite3"
        self.tick = 0

        def fake_now():
            # One minute per write, so every row below has a known timestamp.
            self.tick += 1
            return f"2026-09-01T00:{self.tick:02d}:00+00:00"

        patcher = mock.patch.object(storage, "_now", fake_now)
        patcher.start()
        self.addCleanup(patcher.stop)

        with Storage(self.db) as store:
            a = store.start_session(                                     # 00:01
                "collect-a",
                adapter_id="OBDLink MX+ 56122",
                protocol="ISO 15765-4 (CAN 11/500)",
                raw_log_path=str(self.root / "logs" / "raw" / "collect-a.jsonl"),
                notes=f"bench run against {IPV4}",
            )
            store.add_sample(a, PidValue("0D", "stale name", 61.0, "mph", "410d3d", "ok"))
            store.add_sample(a, PidValue("42", "control module voltage", 13.8, "V", "41423600", "ok"))
            store.add_sample(a, PidValue("99", "PID 99", None, "", "", "no_data"))
            store.add_dtc_read(a, "03", ["P0143", "C0561"], "4302 0143 0561")   # 00:05
            store.add_event("connected", f"adapter {MAC}", session_id=a)        # 00:06
            store.end_session(a)                                                # 00:07
            b = store.start_session("collect-b", notes=f"vin {VIN}; tailnet {IPV6}")  # 00:08
            store.add_sample(b, PidValue("0D", "vehicle speed", 0.0, "km/h", "410d00", "ok"))
            store.add_event("idle_backoff", "no data this cycle", session_id=b)  # 00:10
            store.end_session(b)                                                # 00:11

    def run_export(self, *args, fmt="jsonl", expect=0):
        out = self.root / f"out.{fmt}"
        argv = ["--root", str(self.root), "--format", fmt, "--output", str(out),
                "--export-time", EXPORT_TIME, *args]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = export.main(argv)
        self.assertEqual(code, expect, err.getvalue())
        return out.read_text(encoding="utf-8")

    @staticmethod
    def lines(text):
        return [json.loads(line) for line in text.splitlines()]


class TestJsonlShape(ExportFixture):
    def test_meta_record_comes_first_and_describes_the_file(self):
        records = self.lines(self.run_export())
        meta = records[0]
        self.assertEqual(meta["kind"], "meta")
        self.assertEqual(meta["schema"], "hummer-obd/export/1")
        self.assertEqual(meta["exported_at"], EXPORT_TIME)
        # Basename only: the node's directory layout is not telemetry.
        self.assertEqual(meta["source_database"], "hummer_obd.sqlite3")
        self.assertNotIn("/", meta["source_database"])
        self.assertEqual(meta["counts"],
                         {"session": 2, "sample": 4, "dtc_read": 1, "event": 2})
        self.assertEqual(meta["records"], 9)
        for kind in ("session", "sample", "dtc_read", "event"):
            self.assertIn(kind, meta["fields"])
        # Every field of every emitted record is described in the meta block.
        for record in records[1:]:
            described = meta["fields"][record["kind"]]
            self.assertEqual(sorted(record), sorted(described), record["kind"])
        self.assertIn("raw_hex", meta["privacy"])
        self.assertEqual([r["kind"] for r in records].count("meta"), 1)

    def test_records_are_flat_self_describing_and_sorted(self):
        records = self.lines(self.run_export())[1:]
        self.assertEqual(len(records), 9)
        for record in records:
            self.assertIn(record["kind"], ("session", "sample", "dtc_read", "event"))
            self.assertTrue(record["ts"].endswith("+00:00"))
            for key, value in record.items():
                self.assertIsInstance(value, (str, int, float, bool, type(None)),
                                      f"{record['kind']}.{key} is not a scalar")
        keys = [(r["kind"], r["ts"], r["id"]) for r in records]
        self.assertEqual(keys, sorted(keys))

    def test_dtc_read_carries_its_codes_and_the_meaning_of_the_mode(self):
        dtc = [r for r in self.lines(self.run_export())[1:] if r["kind"] == "dtc_read"][0]
        self.assertEqual(dtc["mode"], "03")
        self.assertEqual(dtc["codes"], "P0143,C0561")
        self.assertEqual(dtc["code_count"], 2)
        self.assertIn("confirmed", dtc["mode_meaning"])
        self.assertEqual(dtc["session_uid"], "collect-a")


class TestOtherFormats(ExportFixture):
    def test_csv_is_one_table_with_a_leading_kind_column(self):
        text = self.run_export(fmt="csv")
        reader = csv.DictReader(io.StringIO(text))
        header = reader.fieldnames
        self.assertEqual(header[0], "kind")
        self.assertEqual(header[1:], sorted(header[1:]))
        rows = list(reader)
        self.assertEqual(len(rows), 9)
        sample = [r for r in rows if r["kind"] == "sample" and r["pid"] == "42"][0]
        self.assertEqual(sample["unit"], "V")
        self.assertEqual(float(sample["value"]), 13.8)
        # A column that does not apply to this kind is blank, never missing.
        session = [r for r in rows if r["kind"] == "session"][0]
        self.assertEqual(session["pid"], "")
        self.assertEqual(session["session_uid"], "collect-a")

    def test_json_is_one_grouped_object_with_sorted_keys(self):
        text = self.run_export(fmt="json")
        document = json.loads(text)
        self.assertEqual(list(document), sorted(document))
        self.assertIn('\n  "meta": {', text)
        self.assertEqual(document["meta"]["schema"], "hummer-obd/export/1")
        self.assertEqual(len(document["samples"]), 4)
        self.assertEqual(len(document["dtc_reads"]), 1)
        self.assertEqual(len(document["sessions"]), 2)
        self.assertEqual(len(document["events"]), 2)


class TestDeterminism(ExportFixture):
    def test_two_exports_of_one_database_are_byte_identical(self):
        for fmt in ("jsonl", "csv", "json"):
            self.assertEqual(self.run_export(fmt=fmt), self.run_export(fmt=fmt), fmt)

    def test_only_the_export_stamp_differs_without_a_fixed_time(self):
        out = self.root / "unstamped.jsonl"
        runs = []
        for _ in range(2):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(export.main(
                    ["--root", str(self.root), "--output", str(out)]), 0)
            runs.append(out.read_text(encoding="utf-8").splitlines())
        self.assertEqual(runs[0][1:], runs[1][1:])


class TestFiltering(ExportFixture):
    def test_since_keeps_the_later_window_and_the_session_that_explains_it(self):
        records = self.lines(self.run_export("--since", "2026-09-01T00:08:00Z"))[1:]
        self.assertEqual([r["ts"] for r in records if r["kind"] == "sample"],
                         ["2026-09-01T00:09:00+00:00"])
        self.assertEqual([r["kind"] for r in records if r["kind"] == "dtc_read"], [])
        self.assertEqual([r["session_uid"] for r in records if r["kind"] == "session"],
                         ["collect-b"])

    def test_until_is_inclusive_and_keeps_the_overlapping_session(self):
        records = self.lines(self.run_export("--until", "2026-09-01T00:05:00+00:00"))[1:]
        self.assertEqual(len([r for r in records if r["kind"] == "sample"]), 3)
        self.assertEqual(len([r for r in records if r["kind"] == "dtc_read"]), 1)
        self.assertEqual([r["session_uid"] for r in records if r["kind"] == "session"],
                         ["collect-a"])
        self.assertEqual([r for r in records if r["kind"] == "event"], [])

    def test_limit_keeps_the_most_recent_rows_of_each_kind(self):
        records = self.lines(self.run_export("--limit", "1", "--include", "samples"))[1:]
        self.assertEqual([r["ts"] for r in records], ["2026-09-01T00:09:00+00:00"])

    def test_session_filter_is_repeatable_and_excludes_everything_else(self):
        records = self.lines(self.run_export("--session", "collect-a"))[1:]
        self.assertEqual({r["session_uid"] for r in records}, {"collect-a"})
        self.assertEqual(len(records), 6)
        both = self.lines(self.run_export("--session", "collect-a", "--session", "collect-b"))[1:]
        self.assertEqual(len(both), 9)

    def test_include_selects_kinds(self):
        records = self.lines(self.run_export("--include", "samples", "--include", "dtcs"))[1:]
        self.assertEqual({r["kind"] for r in records}, {"sample", "dtc_read"})
        every = self.lines(self.run_export("--include", "all"))[1:]
        self.assertEqual(len(every), 9)

    def test_an_unusable_timestamp_is_refused_before_anything_is_written(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = export.main(["--root", str(self.root), "--since", "last tuesday"])
        self.assertEqual(code, 2)
        self.assertIn("--since", err.getvalue())

    def test_a_negative_limit_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(export.main(["--root", str(self.root), "--limit", "-1"]), 2)


class TestEnrichment(ExportFixture):
    def test_the_decoder_table_supplies_name_unit_and_meaning(self):
        samples = [r for r in self.lines(self.run_export())[1:] if r["kind"] == "sample"]
        speed = [s for s in samples if s["pid"] == "0D"][0]
        # The database row said "stale name" in mph; the decoder table wins.
        self.assertEqual(speed["name"], "vehicle speed")
        self.assertEqual(speed["unit"], "km/h")
        self.assertEqual(speed["request"], "010D")
        self.assertIn("vehicle speed", speed["meaning"])
        self.assertIn("km/h", speed["meaning"])

    def test_an_undecoded_pid_says_so_rather_than_guessing(self):
        samples = [r for r in self.lines(self.run_export())[1:] if r["kind"] == "sample"]
        unknown = [s for s in samples if s["pid"] == "99"][0]
        self.assertEqual(unknown["name"], "PID 99")
        self.assertIsNone(unknown["value"])
        self.assertEqual(unknown["status"], "no_data")
        self.assertIn("no decoder", unknown["meaning"])

    def test_raw_hex_is_kept_verbatim(self):
        samples = [r for r in self.lines(self.run_export())[1:] if r["kind"] == "sample"]
        self.assertEqual([s for s in samples if s["pid"] == "42"][0]["raw_hex"], "41423600")


class TestPrivacy(ExportFixture):
    def test_identifiers_never_reach_the_export(self):
        for fmt in ("jsonl", "csv", "json"):
            text = self.run_export(fmt=fmt)
            self.assertNotIn(VIN, text, fmt)
            self.assertNotIn(MAC, text, fmt)
            self.assertNotIn(IPV4, text, fmt)
            self.assertNotIn(IPV6, text, fmt)
            self.assertIn(mask_vin(VIN), text, fmt)
            self.assertIn("(mac redacted)", text, fmt)
            self.assertIn("(ip redacted)", text, fmt)

    def test_the_raw_log_path_is_reduced_to_a_file_name(self):
        sessions = [r for r in self.lines(self.run_export())[1:] if r["kind"] == "session"]
        first = [s for s in sessions if s["session_uid"] == "collect-a"][0]
        self.assertEqual(first["raw_log_file"], "collect-a.jsonl")
        self.assertNotIn(str(self.root), self.run_export())

    def test_a_vin_is_masked_whatever_case_it_was_typed_in(self):
        # uploader._refuse_if_vin_shaped upper-cases before it scans; an export
        # that only matched upper case would write a lower-case VIN out whole.
        masked = export.redact(f"note: vin {VIN.lower()} recorded")
        self.assertNotIn(VIN.lower(), masked)
        self.assertIn(mask_vin(VIN).lower(), masked.lower())

    def test_both_written_forms_of_a_mac_are_redacted(self):
        for text in (f"adapter {MAC}",
                     f"adapter mac:{MAC}",
                     "adapter " + MAC.replace(":", "-"),
                     "eui 00:04:3E:FF:FE:AA:BB:CC"):
            with self.subTest(text=text):
                out = export.redact(text)
                self.assertNotIn("3E", out, out)
                self.assertNotIn("AA:BB", out, out)

    def test_a_session_filter_is_not_echoed_back_in_the_clear(self):
        text = self.run_export("--session", VIN)
        self.assertNotIn(VIN, text)
        self.assertEqual(json.loads(text.splitlines()[0])["filters"]["sessions"],
                         [mask_vin(VIN)])

    def test_redaction_leaves_ordinary_text_and_timestamps_alone(self):
        self.assertEqual(export.redact("connected at 2026-09-01T00:06:00+00:00"),
                         "connected at 2026-09-01T00:06:00+00:00")
        self.assertEqual(export.redact("410d3d 41423600"), "410d3d 41423600")


class TestReadOnly(ExportFixture):
    def test_the_connection_refuses_writes(self):
        conn = export.open_readonly(self.db)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("INSERT INTO events(ts, kind, detail) VALUES ('x','y','z')")

    def test_exporting_does_not_touch_the_database(self):
        before = self.db.read_bytes()
        self.run_export()
        self.assertEqual(self.db.read_bytes(), before)
        # SQLite creates the WAL sidecars to read a WAL database, but a
        # read-only connection never puts a byte of its own in the log.
        wal = Path(str(self.db) + "-wal")
        self.assertEqual(wal.stat().st_size if wal.exists() else 0, 0)

    def test_a_missing_database_is_a_message_not_a_traceback(self):
        empty = Path(tempfile.mkdtemp())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = export.main(["--root", str(empty)])
        self.assertEqual(code, 1)
        self.assertIn("no collector database", err.getvalue())

    def test_a_failed_export_does_not_destroy_the_previous_one(self):
        # The destination is opened for truncation; if that happens before the
        # database has been read, a failed export deletes the last good one.
        empty = Path(tempfile.mkdtemp())
        out = empty / "previous.jsonl"
        out.write_text("earlier export\n", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            code = export.main(["--root", str(empty), "--output", str(out)])
        self.assertEqual(code, 1)
        self.assertEqual(out.read_text(encoding="utf-8"), "earlier export\n")

    def test_the_module_cannot_reach_the_serial_device(self):
        tree = ast.parse(Path(export.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertNotIn("serial", imported)
        self.assertNotIn("transport", imported)
        self.assertNotIn("session", imported)


class TestOutput(ExportFixture):
    def test_stdout_is_the_default_destination(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(export.main(["--root", str(self.root),
                                          "--export-time", EXPORT_TIME]), 0)
        self.assertEqual(buf.getvalue(), self.run_export())

    def test_a_dash_also_means_stdout(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(export.main(["--root", str(self.root), "--output", "-",
                                          "--export-time", EXPORT_TIME]), 0)
        self.assertEqual(buf.getvalue(), self.run_export())

    def test_a_config_file_chooses_the_database(self):
        # The config names a *different* database from the default path, so
        # this fails if --config is ignored instead of silently agreeing with
        # the default.
        other = self.root / "data" / "other.sqlite3"
        with Storage(other) as store:
            store.start_session("config-only")
        config_dir = self.root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config = config_dir / "hummer.toml"
        config.write_text('[collector]\ndatabase = "data/other.sqlite3"\n')
        out = self.root / "via-config.jsonl"
        with contextlib.redirect_stderr(io.StringIO()):
            code = export.main(["--config", str(config), "--root", str(self.root),
                                "--output", str(out), "--export-time", EXPORT_TIME])
        self.assertEqual(code, 0)
        records = self.lines(out.read_text(encoding="utf-8"))
        self.assertEqual(records[0]["source_database"], "other.sqlite3")
        self.assertEqual([r["session_uid"] for r in records[1:]], ["config-only"])


class TestPerModuleAttributionSurvivesTheExport(unittest.TestCase):
    """Several modules answer one request, each with its own value.

    An export that dropped the module address would turn a distribution into an
    unattributed list of numbers -- eight different voltages with no way to say
    which module reported which.  That is the specific loss the ``ecu`` column
    was added to the schema to prevent, so it has to reach the export too.
    """

    def test_the_module_address_reaches_every_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "data" / "hummer_obd.sqlite3"
            with Storage(db) as store:
                sid = store.start_session("probe-per-ecu")
                for ecu, volts in (("45", 13.747), ("28", 13.910), ("1D", 13.500)):
                    store.add_sample(sid, PidValue(
                        pid="42", name="control module voltage", value=volts,
                        unit="V", raw_hex="0441423675", status="ok", ecu=ecu))
                store.end_session(sid)

            out = root / "e.jsonl"
            self.assertEqual(main(["--root", str(root), "--format", "jsonl",
                                   "--include", "samples", "--output", str(out)]), 0)
            records = [json.loads(line) for line in out.read_text().splitlines()]
            samples = [r for r in records if r["kind"] == "sample"]
            self.assertEqual({s["ecu"] for s in samples}, {"45", "28", "1D"})
            self.assertEqual({(s["ecu"], s["value"]) for s in samples},
                             {("45", 13.747), ("28", 13.910), ("1D", 13.500)})

            out_csv = root / "e.csv"
            self.assertEqual(main(["--root", str(root), "--format", "csv",
                                   "--include", "samples", "--output", str(out_csv)]), 0)
            rows = list(csv.DictReader(io.StringIO(out_csv.read_text())))
            self.assertIn("ecu", rows[0])
            self.assertEqual({r["ecu"] for r in rows}, {"45", "28", "1D"})

            out_json = root / "e.json"
            self.assertEqual(main(["--root", str(root), "--format", "json",
                                   "--include", "samples", "--output", str(out_json)]), 0)
            payload = json.loads(out_json.read_text())
            self.assertEqual({s["ecu"] for s in payload["samples"]}, {"45", "28", "1D"})
            # The meta record has to describe the field, or an ingesting reader
            # has no way to know what "ecu" means.
            self.assertIn("ecu", json.dumps(payload["meta"]))



if __name__ == "__main__":  # pragma: no cover
    unittest.main()
