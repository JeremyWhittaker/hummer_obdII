"""Defensive display consumption of the Uniden R8 collector state."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hummer_obd.display.status import (
    R8_STATE_MAX_BYTES,
    StatusData,
    gather_status,
    read_r8_display_line,
)


class TestR8DisplayState(unittest.TestCase):
    NOW = datetime(2026, 9, 2, 4, 0, 30, tzinfo=timezone.utc)

    def document(self, **updates):
        document = {
            "schema": 1,
            "updated_at": "2026-09-02T04:00:00Z",
            "collector": {"status": "streaming"},
            "obd": {"healthy": True},
            "link": {"connected": True, "compatible": True},
            "telemetry": {"voltage": 13.6, "gps_locked": True, "stale": False},
            "alerts": [],
            # This value is intentionally hostile. The display never trusts it.
            "display_line": "attacker-controlled 00:11:22:33:44:55",
        }
        document.update(updates)
        return document

    def read(self, document):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            return read_r8_display_line(path, now=self.NOW)

    def test_clear_line_uses_only_typed_fields(self):
        line = self.read(self.document())
        self.assertEqual(line, "r8 13.6V GPS clear")
        self.assertNotIn("00:11", line)

    def test_allowlisted_alert_line(self):
        line = self.read(self.document(alerts=[{
            "band": "KA", "strength": 3, "frequency_ghz": 33.785,
            "direction": "front", "muted": False,
        }]))
        self.assertEqual(line, "r8 13.6V KA 3/8 front")

    def test_arbitrary_alert_text_is_never_rendered(self):
        line = self.read(self.document(alerts=[{
            "band": "secret-device-identifier", "strength": 3,
            "direction": "00:11:22:33:44:55",
        }]))
        self.assertEqual(line, "r8 13.6V alert")
        self.assertNotIn("secret", line)
        self.assertNotIn("00:11", line)

    def test_missing_malformed_and_unknown_schema_fall_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"
            self.assertEqual(read_r8_display_line(path, now=self.NOW), "")
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(read_r8_display_line(path, now=self.NOW), "")
        self.assertEqual(self.read(self.document(schema=999)), "")

    def test_stale_and_future_state_fail_closed(self):
        stale = self.document(updated_at="2026-09-02T03:00:00Z")
        future = self.document(updated_at="2026-09-02T05:00:00Z")
        self.assertEqual(self.read(stale), "r8 stale")
        self.assertEqual(self.read(future), "")

    def test_r8_status_does_not_override_the_obd_line(self):
        status = StatusData(
            hostname="hummer", tailscale_ip="100.64.0.20",
            r8_state="r8 13.6V GPS clear", obd_state="rfcomm0 bound",
        )
        lines = status.as_lines()
        self.assertEqual(len(lines), 6)
        self.assertEqual(lines[3], "r8 13.6V GPS clear")
        self.assertEqual(lines[5], "obd  rfcomm0 bound")

    def test_absent_state_preserves_the_tailscale_fallback(self):
        lines = StatusData(tailscale_ip="100.64.0.20").as_lines()
        self.assertEqual(lines[3], "ts   100.64.0.20")

    def test_oversized_document_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(" " * (R8_STATE_MAX_BYTES + 1), encoding="utf-8")
            self.assertEqual(read_r8_display_line(path, now=self.NOW), "")

    def test_operational_states_render_only_fixed_phrases(self):
        cases = (
            ({"collector": {"status": "stopped"}}, "r8 stopped"),
            ({"collector": {"status": "connecting"}}, "r8 connecting"),
            ({"collector": {"status": "degraded"}}, "r8 degraded"),
            ({"collector": {"status": "incompatible"}}, "r8 incompatible"),
            ({"obd": {"healthy": False, "reason": "untrusted"}}, "r8 paused: obd"),
        )
        for update, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(self.read(self.document(**update)), expected)

    def test_invalid_typed_shapes_fail_closed_without_echoing(self):
        cases = (
            {"collector": {"status": "secret-state"}},
            {"obd": {"healthy": "yes"}},
            {"link": ["connected", True]},
            {"telemetry": "13.6V at a private place"},
            {"alerts": "KA near private place"},
        )
        for update in cases:
            with self.subTest(update=update):
                self.assertEqual(self.read(self.document(**update)), "")

    def test_nonfinite_voltage_is_not_rendered(self):
        line = self.read(self.document(telemetry={
            "voltage": float("inf"), "gps_locked": True, "stale": False,
        }))
        self.assertEqual(line, "r8 --V GPS clear")

    def test_gather_status_calls_the_defensive_reader(self):
        with patch(
            "hummer_obd.display.status.read_r8_display_line",
            return_value="r8 13.6V GPS clear",
        ):
            status = gather_status()
        self.assertEqual(status.r8_state, "r8 13.6V GPS clear")


if __name__ == "__main__":
    unittest.main()
