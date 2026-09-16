"""Scanner deployment uses a narrow manifest, never vehicle/network I/O here."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class TestScannerDeployment(unittest.TestCase):
    script = Path(__file__).resolve().parents[1] / "scripts" / "deploy.sh"

    def run_deploy(self, *args):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "calls.jsonl"
            for executable in ("ssh", "rsync", "getent"):
                stub = root / executable
                stub.write_text(
                    "#!/usr/bin/env python3\n"
                    "import json, os, sys\n"
                    "with open(os.environ['SCAN_TEST_LOG'], 'a') as f:\n"
                    "    f.write(json.dumps(sys.argv) + '\\n')\n"
                )
                stub.chmod(0o700)
            result = subprocess.run(
                ["bash", str(self.script), *args], capture_output=True, text=True,
                env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}",
                     "SCAN_TEST_LOG": str(log), "HOST": "operator@node.example",
                     "DEST": "/home/operator/hummer-obd"}, timeout=10,
            )
            calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            return result, calls

    def test_scan_only_copies_exact_feature_without_deletion_or_service_changes(self):
        result, calls = self.run_deploy("--scan-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        ssh = [c for c in calls if Path(c[0]).name == "ssh"]
        self.assertEqual(len(ssh), 1)
        self.assertTrue(ssh[0][-1].startswith("test -f "))
        self.assertNotIn("mkdir", ssh[0][-1])
        self.assertNotIn("systemctl", ssh[0][-1])
        self.assertNotIn("sudo", ssh[0][-1])
        rsync = [c for c in calls if Path(c[0]).name == "rsync"]
        self.assertEqual(len(rsync), 1)
        args = rsync[0][1:]
        self.assertNotIn("--delete", args)
        self.assertIn("-avR", args)
        copied = {arg.split("/./", 1)[1] for arg in args if "/./" in arg}
        self.assertEqual(copied, {
            "src/hummer_obd/scan.py", "src/hummer_obd/safety.py", "src/hummer_obd/access.py",
            "pyproject.toml", "pytest.ini", "README.md", "ROADMAP.md", "docs/DEEP_SCAN.md",
            "docs/SAFETY.md", "docs/ACCESS_MATRIX.md", "tests/test_scan.py",
            "tests/test_safety.py", "tests/test_access.py", "tests/elm_simulator.py",
            "scripts/deploy.sh", "scripts/enable_agent_service_control.sh",
            "scripts/lib/require-node.sh", "scripts/polkit/49-hummer-obd-recorder.rules",
            "tests/test_agent_service_control.py", "tests/test_scan_deploy.py",
            "tests/test_scripts_target_the_right_machine.py",
        })
        self.assertFalse(any("dashboard" in path or path.startswith(("config/", "evidence/", "logs/"))
                             for path in copied))

    def test_scan_dry_run_only_checks_existing_files_and_previews_rsync(self):
        result, calls = self.run_deploy("--scan-only", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        rsync = next(c for c in calls if Path(c[0]).name == "rsync")
        self.assertIn("--dry-run", rsync)
        ssh = next(c for c in calls if Path(c[0]).name == "ssh")
        self.assertTrue(ssh[-1].startswith("test -f "))

    def test_invalid_arguments_do_not_contact_node(self):
        for args in (("--dry-run",), ("--unknown",)):
            with self.subTest(args=args):
                result, calls = self.run_deploy(*args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(calls, [])
