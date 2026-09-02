"""Upload is off unless it is deliberately turned on, and never loses data.

Every test here is an argument that nothing leaves the Pi by accident: the
default is off, a plaintext endpoint is refused, a missing credential stops the
batch instead of downgrading it, a VIN-shaped string aborts the batch, and rows
are stamped only against a confirmed 2xx.  Nothing here reaches off the
machine: the sender is injected everywhere except the tests of the real HTTP
sender, which talk to a throwaway loopback server on an ephemeral port, and
the one test that exercises the CLI runs it in its dry-run default.
"""

import contextlib
import io
import json
import logging
import tempfile
import threading
import traceback
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from hummer_obd.config import Config
from hummer_obd.decode import PidValue
from hummer_obd.storage import Storage
from hummer_obd.uploader import (
    FORBIDDEN_PAYLOAD_KEYS,
    UPLOADABLE_TABLES,
    UploadDisabled,
    UploadError,
    Uploader,
    _post,
    audit_payload,
    main,
)

ENDPOINT = "https://example.invalid/ingest"
# Shaped like a VIN (17 characters, no I/O/Q) but not anyone's vehicle.
FAKE_VIN = "5GTRSDE64NB123456"


class _Recorder:
    """Captures what the uploader hands to the network without going near it."""

    def __init__(self, status=200):
        self.status = status
        self.calls = []

    def __call__(self, endpoint, payload, timeout=30.0, headers=None):
        self.calls.append({"endpoint": endpoint, "payload": payload,
                           "timeout": timeout, "headers": headers or {}})
        return self.status


def _explode(exc):
    def sender(endpoint, payload, timeout=30.0, headers=None):
        raise exc
    return sender


class _LoopbackServer:
    """A throwaway HTTP server on 127.0.0.1, used to test the real sender.

    The injected sender used everywhere else cannot answer the question these
    tests ask, because the behaviour under test lives inside ``_post`` itself:
    what urllib does with a 3xx.  Asserting that an injected sender returning
    301 leaves the rows pending proves something about the fake, not about the
    code that will run on the Pi.
    """

    def __init__(self, handler_cls):
        self.server = HTTPServer(("127.0.0.1", 0), handler_cls)
        self.server.received = []
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def received(self):
        return self.server.received

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class _Sink(BaseHTTPRequestHandler):
    """Records anything that reaches it, by any method."""

    def _record(self, method):
        length = int(self.headers.get("Content-Length", 0))
        self.server.received.append({
            "method": method,
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": self.rfile.read(length).decode("utf-8") if length else "",
        })
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self):
        self._record("POST")

    def do_GET(self):
        self._record("GET")

    def log_message(self, *args):
        pass


class TestRealSenderRefusesRedirects(unittest.TestCase):
    """``_post`` must not chase a Location header.

    urllib's default opener follows a redirect, copies every non-content header
    (the bearer token included) onto the new request, and rewrites the POST as
    a GET.  Left alone that hands the credential to whatever host the Location
    names, downgrades to plaintext straight past ``require_https``, and returns
    a 200 for a batch that was never delivered — which the uploader would read
    as a receipt and stamp the rows against.
    """

    def setUp(self):
        self.sink = _LoopbackServer(_Sink)
        self.addCleanup(self.sink.close)
        sink_url = self.sink.url

        class Redirector(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", f"{sink_url}/somewhere-else")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.redirector = _LoopbackServer(Redirector)
        self.addCleanup(self.redirector.close)

    def test_redirect_is_refused_and_the_token_is_not_forwarded(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _post(
                f"{self.redirector.url}/ingest",
                {"schema": "hummer-obd/sample-batch/1", "samples": [{"pid": "0C"}]},
                timeout=5.0,
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer must-not-be-forwarded"},
            )
        self.assertEqual(ctx.exception.code, 302)
        self.assertEqual(self.sink.received, [])
        self.assertNotIn("must-not-be-forwarded", str(ctx.exception))

    def test_a_redirected_batch_stays_in_the_local_buffer(self):
        root = Path(tempfile.mkdtemp())
        store = Storage(root / "db.sqlite3")
        self.addCleanup(store.close)
        sid = store.start_session("uid")
        for i in range(3):
            store.add_sample(sid, PidValue("0C", "engine speed", i, "rpm", "410c", "ok"))
        cfg = Config()
        cfg.root = root
        cfg.upload.enabled = True
        cfg.upload.require_https = False
        cfg.upload.endpoint = f"{self.redirector.url}/ingest"
        cfg.upload.timeout_s = 5.0
        with self.assertRaises(UploadError):
            Uploader(cfg, store).run_once()
        self.assertEqual(store.pending_count(), 3)
        self.assertEqual(self.sink.received, [])

    def test_an_ordinary_2xx_through_the_real_sender_still_posts(self):
        status = _post(
            f"{self.sink.url}/ingest",
            {"schema": "hummer-obd/sample-batch/1", "samples": []},
            timeout=5.0,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.sink.received[0]["method"], "POST")


class TestUploader(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.store = Storage(self.root / "db.sqlite3")
        self.sid = self.store.start_session("uid")
        for i in range(5):
            self.store.add_sample(self.sid, PidValue("0C", "engine speed", i, "rpm", "410c", "ok"))
        self.cfg = Config()
        self.cfg.root = self.root

    def tearDown(self):
        self.store.close()

    def _turn_on(self, endpoint=ENDPOINT):
        self.cfg.upload.enabled = True
        self.cfg.upload.endpoint = endpoint

    def _token_file(self, contents):
        path = self.root / "token"
        path.write_text(contents, encoding="utf-8")
        self.cfg.upload.token_file = str(path)
        return path

    # -- rule 1: off unless deliberately turned on -----------------------
    def test_disabled_by_default(self):
        uploader = Uploader(self.cfg, self.store, sender=lambda *a, **k: 200)
        self.assertFalse(uploader.enabled())
        with self.assertRaises(UploadDisabled):
            uploader.run_once()
        self.assertEqual(self.store.pending_count(), 5)

    def test_enabled_without_endpoint_stays_off(self):
        self.cfg.upload.enabled = True
        uploader = Uploader(self.cfg, self.store, sender=lambda *a, **k: 200)
        self.assertFalse(uploader.enabled())
        with self.assertRaises(UploadDisabled):
            uploader.run_once()

    # -- rule 2: https unless https is explicitly waived -----------------
    def test_plaintext_endpoint_is_refused(self):
        self._turn_on("http://192.0.2.10:8080/ingest")
        sender = _Recorder()
        with self.assertRaises(UploadDisabled) as ctx:
            Uploader(self.cfg, self.store, sender=sender).run_once()
        message = str(ctx.exception)
        self.assertIn("http://192.0.2.10:8080/ingest", message)
        self.assertIn("require_https", message)
        self.assertEqual(sender.calls, [])
        self.assertEqual(self.store.pending_count(), 5)

    def test_plaintext_endpoint_allowed_only_when_waived(self):
        self._turn_on("http://127.0.0.1:8080/ingest")
        self.cfg.upload.require_https = False
        sender = _Recorder()
        result = Uploader(self.cfg, self.store, sender=sender).run_once()
        self.assertEqual(result.sent, 5)
        self.assertEqual(len(sender.calls), 1)

    def test_uploader_checks_https_even_when_config_validate_never_ran(self):
        # A Config built in code never passes through load_config(), so the
        # uploader cannot rely on UploadConfig.validate() having run.
        self._turn_on("http://example.invalid/ingest")
        with self.assertRaises(ValueError):
            self.cfg.upload.validate()
        with self.assertRaises(UploadDisabled):
            Uploader(self.cfg, self.store, sender=_Recorder()).run_once()

    def test_allow_raw_logs_is_refused_by_the_uploader_too(self):
        self._turn_on()
        self.cfg.upload.allow_raw_logs = True
        sender = _Recorder()
        with self.assertRaises(UploadDisabled):
            Uploader(self.cfg, self.store, sender=sender).run_once()
        self.assertEqual(sender.calls, [])

    # -- rule 3: bearer token ---------------------------------------------
    def test_bearer_token_is_sent_when_a_token_file_is_configured(self):
        self._turn_on()
        self._token_file("  s3cret-token\n")
        sender = _Recorder()
        Uploader(self.cfg, self.store, sender=sender).run_once()
        self.assertEqual(sender.calls[0]["headers"]["Authorization"], "Bearer s3cret-token")

    def test_no_authorization_header_without_a_token_file(self):
        self._turn_on()
        sender = _Recorder()
        Uploader(self.cfg, self.store, sender=sender).run_once()
        self.assertNotIn("Authorization", sender.calls[0]["headers"])

    def test_token_is_read_at_send_time_so_rotation_is_picked_up(self):
        self._turn_on()
        self.cfg.upload.batch_size = 2
        path = self._token_file("first-token")
        sender = _Recorder()
        uploader = Uploader(self.cfg, self.store, sender=sender)
        uploader.run_once()
        path.write_text("second-token", encoding="utf-8")
        uploader.run_once()
        self.assertEqual(sender.calls[0]["headers"]["Authorization"], "Bearer first-token")
        self.assertEqual(sender.calls[1]["headers"]["Authorization"], "Bearer second-token")

    def test_missing_token_file_refuses_rather_than_posting_anonymously(self):
        self._turn_on()
        self.cfg.upload.token_file = str(self.root / "absent")
        sender = _Recorder()
        with self.assertRaises(UploadDisabled):
            Uploader(self.cfg, self.store, sender=sender).run_once()
        self.assertEqual(sender.calls, [])
        self.assertEqual(self.store.pending_count(), 5)

    def test_empty_token_file_refuses_rather_than_posting_anonymously(self):
        self._turn_on()
        self._token_file("   \n")
        sender = _Recorder()
        with self.assertRaises(UploadDisabled):
            Uploader(self.cfg, self.store, sender=sender).run_once()
        self.assertEqual(sender.calls, [])

    def test_token_never_reaches_an_exception_message_or_any_log(self):
        secret = "kf9Qz-token-must-not-appear"
        self._turn_on()
        self._token_file(secret)
        captured = io.StringIO()
        handler = logging.StreamHandler(captured)
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                uploader = Uploader(self.cfg, self.store, sender=_explode(OSError("no route")))
                preview = uploader.preview()
                with self.assertRaises(UploadError) as ctx:
                    uploader.run_once()
                chain = "".join(traceback.format_exception(
                    type(ctx.exception), ctx.exception, ctx.exception.__traceback__))
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)
        self.assertNotIn(secret, str(ctx.exception))
        self.assertNotIn(secret, repr(ctx.exception))
        self.assertNotIn(secret, chain)
        self.assertNotIn(secret, captured.getvalue())
        self.assertNotIn(secret, json.dumps(preview))

    # -- rule 4: decoded samples only, and no VIN riding along ------------
    def test_raw_transcripts_are_never_uploaded(self):
        self._turn_on()
        sender = _Recorder()
        Uploader(self.cfg, self.store, sender=sender).run_once()
        payload = sender.calls[0]["payload"]
        self.assertEqual(set(payload["samples"][0]),
                         {"ts", "pid", "name", "value", "unit", "status", "raw_hex"})
        self.assertTrue(FORBIDDEN_PAYLOAD_KEYS.isdisjoint(payload))

    def test_only_the_samples_table_is_ever_read(self):
        self.assertEqual(UPLOADABLE_TABLES, ("samples",))

        class RecordingStore:
            def __init__(self, inner):
                self.inner = inner
                self.used = []

            def __getattr__(self, name):
                self.used.append(name)
                return getattr(self.inner, name)

        self._turn_on()
        store = RecordingStore(self.store)
        Uploader(self.cfg, store, sender=_Recorder()).run_once()
        self.assertEqual(set(store.used), {"pending_samples", "mark_uploaded", "pending_count"})

    def test_forbidden_keys_are_refused_at_any_depth(self):
        for key in sorted(FORBIDDEN_PAYLOAD_KEYS):
            with self.subTest(key=key):
                with self.assertRaises(UploadError):
                    audit_payload({"schema": "x", "samples": [{"ts": "t", key: "anything"}]})

    def test_vin_shaped_string_in_a_field_aborts_the_batch(self):
        self._turn_on()
        self.store.add_sample(self.sid, PidValue("02", FAKE_VIN, None, "", "4902", "ok"))
        sender = _Recorder()
        with self.assertRaises(UploadError) as ctx:
            Uploader(self.cfg, self.store, sender=sender).run_once()
        self.assertNotIn(FAKE_VIN, str(ctx.exception))
        self.assertIn("VIN", str(ctx.exception))
        self.assertEqual(sender.calls, [])
        self.assertEqual(self.store.pending_count(), 6)

    def test_vin_spelled_out_inside_raw_hex_aborts_the_batch(self):
        self._turn_on()
        smuggled = FAKE_VIN.encode("ascii").hex()
        self.store.add_sample(self.sid, PidValue("02", "vehicle id", None, "", smuggled, "ok"))
        sender = _Recorder()
        with self.assertRaises(UploadError):
            Uploader(self.cfg, self.store, sender=sender).run_once()
        self.assertEqual(sender.calls, [])
        self.assertEqual(self.store.pending_count(), 6)

    def test_ordinary_frame_hex_is_not_mistaken_for_a_vin(self):
        self._turn_on()
        for raw in ("410c1af8", "410d00 410d01 410d02", "0123456789abcdef0123456789abcdef"):
            self.store.add_sample(self.sid, PidValue("0D", "speed", 0.0, "km/h", raw, "ok"))
        sender = _Recorder()
        result = Uploader(self.cfg, self.store, sender=sender).run_once()
        self.assertEqual(result.sent, 8)

    # -- rule 5: rows are stamped only against a confirmed 2xx ------------
    def test_successful_batch_marks_rows(self):
        self._turn_on()
        self.cfg.upload.batch_size = 3
        sender = _Recorder()
        result = Uploader(self.cfg, self.store, sender=sender).run_once()
        self.assertEqual(result.sent, 3)
        self.assertEqual(result.remaining, 2)
        self.assertEqual(sender.calls[0]["endpoint"], ENDPOINT)
        self.assertEqual(len(sender.calls[0]["payload"]["samples"]), 3)
        self.assertEqual(sender.calls[0]["payload"]["schema"], "hummer-obd/sample-batch/1")

    def test_failure_leaves_the_buffer_intact(self):
        self._turn_on()
        with self.assertRaises(UploadError):
            Uploader(self.cfg, self.store,
                     sender=_explode(OSError("no route to host"))).run_once()
        self.assertEqual(self.store.pending_count(), 5)

    def test_non_2xx_statuses_leave_the_buffer_intact(self):
        self._turn_on()
        for status in (301, 302, 400, 401, 404, 429, 500, 503):
            with self.subTest(status=status):
                with self.assertRaises(UploadError):
                    Uploader(self.cfg, self.store, sender=_Recorder(status)).run_once()
                self.assertEqual(self.store.pending_count(), 5)

    def test_unexpected_exception_mid_send_leaves_the_buffer_intact(self):
        self._turn_on()
        with self.assertRaises(RuntimeError):
            Uploader(self.cfg, self.store,
                     sender=_explode(RuntimeError("serializer blew up"))).run_once()
        self.assertEqual(self.store.pending_count(), 5)

    def test_nothing_is_sent_when_there_is_nothing_pending(self):
        self._turn_on()
        sender = _Recorder()
        uploader = Uploader(self.cfg, self.store, sender=sender)
        uploader.run_once()
        self.assertEqual(uploader.run_once().sent, 0)
        self.assertEqual(len(sender.calls), 1)

    # -- rule 6: the configured timeout is the one used -------------------
    def test_configured_timeout_is_passed_to_the_sender(self):
        self._turn_on()
        self.cfg.upload.timeout_s = 7.5
        sender = _Recorder()
        Uploader(self.cfg, self.store, sender=sender).run_once()
        self.assertEqual(sender.calls[0]["timeout"], 7.5)

    # -- rule 7: dry run shows exactly what would leave the Pi ------------
    def test_dry_run_builds_the_payload_without_sending_or_stamping(self):
        self._turn_on()
        self.cfg.upload.batch_size = 3
        sender = _Recorder()
        uploader = Uploader(self.cfg, self.store, sender=sender)
        result = uploader.run_once(dry_run=True)
        self.assertTrue(result.dry_run)
        self.assertEqual(result.sent, 0)
        self.assertEqual(len(result.payload["samples"]), 3)
        self.assertEqual(result.payload, uploader.preview())
        self.assertEqual(sender.calls, [])
        self.assertEqual(self.store.pending_count(), 5)

    def test_dry_run_still_refuses_everything_a_real_run_refuses(self):
        uploader = Uploader(self.cfg, self.store, sender=_Recorder())
        with self.assertRaises(UploadDisabled):
            uploader.run_once(dry_run=True)
        with self.assertRaises(UploadDisabled):
            uploader.preview()

        self._turn_on("http://example.invalid/ingest")
        with self.assertRaises(UploadDisabled):
            Uploader(self.cfg, self.store, sender=_Recorder()).run_once(dry_run=True)

        self._turn_on()
        self.cfg.upload.token_file = str(self.root / "absent")
        with self.assertRaises(UploadDisabled):
            Uploader(self.cfg, self.store, sender=_Recorder()).preview()

        self.cfg.upload.token_file = ""
        self.store.add_sample(self.sid, PidValue("02", FAKE_VIN, None, "", "4902", "ok"))
        with self.assertRaises(UploadError):
            Uploader(self.cfg, self.store, sender=_Recorder()).run_once(dry_run=True)

    def test_cli_defaults_to_a_dry_run(self):
        config_dir = self.root / "config"
        config_dir.mkdir()
        config_path = config_dir / "hummer.toml"
        config_path.write_text(
            "[upload]\n"
            "enabled = true\n"
            f'endpoint = "{ENDPOINT}"\n'
            "\n[collector]\n"
            f'database = "{self.root / "db.sqlite3"}"\n',
            encoding="utf-8",
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--config", str(config_path), "--root", str(self.root)])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["schema"], "hummer-obd/sample-batch/1")
        self.assertEqual(len(payload["samples"]), 5)
        self.assertEqual(self.store.pending_count(), 5)
    def test_cli_does_not_create_a_local_buffer_it_only_meant_to_read(self):
        empty = self.root / "elsewhere"
        empty.mkdir()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(["--root", str(empty)])
        self.assertEqual(code, 1)
        self.assertIn("no local buffer", err.getvalue())
        self.assertEqual(list(empty.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
