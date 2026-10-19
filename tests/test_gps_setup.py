"""USB renumbering, narrow device selection and GPS registration checks."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from hummer_obd import gps_setup as G


class ReceiverSelectionTests(unittest.TestCase):
    def test_renumbering_changes_only_the_underlying_node(self):
        for name in ("ttyUSB0", "ttyUSB1", "ttyUSB7"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / name).mkdir()
                receiver = G.find_receiver(root, lambda _: {**G.IDENTITY, "DEVNAME": f"/dev/{name}"})
                self.assertEqual(receiver.node, Path("/dev") / name)
                self.assertEqual(receiver.sysfs, root / name)
                self.assertEqual(G.ALIAS, Path("/dev/hummer_gps"))

    def test_other_serial_adapters_are_not_guessed_to_be_gps(self):
        for key in G.IDENTITY:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "ttyUSB1").mkdir()
                props = {**G.IDENTITY, key: "different", "DEVNAME": "/dev/ttyUSB1"}
                with self.assertRaisesRegex(ValueError, "found 0"):
                    G.find_receiver(root, lambda _: props)

    def test_two_indistinguishable_receivers_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("ttyUSB0", "ttyUSB1"):
                (root / name).mkdir()
            with self.assertRaisesRegex(ValueError, "found 2"):
                G.find_receiver(root, lambda p: {**G.IDENTITY, "DEVNAME": f"/dev/{p.name}"})

    def test_no_receiver_and_unexpected_device_path_fail_before_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "found 0"):
                G.find_receiver(root)
            (root / "ttyUSB1").mkdir()
            with self.assertRaisesRegex(ValueError, "unexpected device path"):
                G.find_receiver(root, lambda _: {**G.IDENTITY, "DEVNAME": "/dev/rfcomm0"})


class ConfigurationTests(unittest.TestCase):
    def test_old_numbered_device_is_replaced_and_unrelated_options_preserved(self):
        original = '# private comment\nDEVICES="/dev/ttyUSB0"\nGPSD_OPTIONS="-n -b"\nUSBAUTO="false"\nOTHER="keep me"\n'
        changed = G.update_defaults(original)
        self.assertIn('DEVICES="/dev/hummer_gps"', changed)
        self.assertIn('USBAUTO="true"', changed)
        self.assertNotIn("ttyUSB0", changed)
        self.assertIn('GPSD_OPTIONS="-n -b"', changed)
        self.assertIn('OTHER="keep me"', changed)
        self.assertIn("# private comment", changed)

    def test_duplicate_assignments_cannot_override_the_stable_name(self):
        original = 'DEVICES="first"\nDEVICES="last"\nUSBAUTO="false"\nUSBAUTO="false"'
        changed = G.update_defaults(original)
        self.assertEqual(changed.count("DEVICES="), 1)
        self.assertEqual(changed.count("USBAUTO="), 1)
        self.assertEqual(G.update_defaults(changed), changed)
        self.assertIn('GPSD_OPTIONS="-n"', changed)

    def test_staging_does_not_accept_a_missing_or_regular_file_as_a_receiver(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "ttyUSB1"
            fake.write_text("not a device")
            with self.assertRaisesRegex(ValueError, "character device"):
                G.stage(root, G.Receiver(fake, root), "")
            self.assertFalse((root / "gpsd.default").exists())

    def test_staging_writes_only_the_planned_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receiver = G.Receiver(Path("/dev/ttyUSB7"), Path("/sys/devices/receiver"))
            with patch.object(Path, "is_char_device", return_value=True):
                G.stage(root, receiver, 'DEVICES="/dev/ttyUSB0"\n')
            self.assertEqual((root / "node").read_text(), "/dev/ttyUSB7")
            self.assertEqual((root / "sysfs").read_text(), "/sys/devices/receiver")
            self.assertIn('DEVICES="/dev/hummer_gps"', (root / "gpsd.default").read_text())
            self.assertEqual({p.name for p in root.iterdir()},
                             {"gpsd.default", "79-hummer-gps.rules", "node", "sysfs"})

    @unittest.skipUnless(shutil.which("udevadm"), "udev rule verifier unavailable")
    def test_generated_rule_passes_the_real_udev_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            rule = Path(tmp) / "79-hummer-gps.rules"
            rule.write_text(G.udev_rule())
            done = subprocess.run(["udevadm", "verify", str(rule)],
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)


class FakeSocket:
    def __init__(self, documents):
        self.chunks = [json.dumps(d).encode() + b"\n" for d in documents] + [b""]
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def sendall(self, value):
        self.sent.append(value)

    def settimeout(self, _):
        pass

    def recv(self, _):
        return self.chunks.pop(0)


class RegistrationTests(unittest.TestCase):
    def read(self, documents, target="ttyUSB7"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            node = root / target
            node.touch()
            alias = root / "hummer_gps"
            alias.symlink_to(node)
            client = FakeSocket(documents(str(alias), str(node)))
            with patch.object(G, "ALIAS", alias), \
                    patch.object(Path, "is_char_device", return_value=True), \
                    patch.object(G.socket, "create_connection", return_value=client):
                return G.gpsd_status()

    def test_registration_recognizes_the_same_device_under_its_numbered_name(self):
        result = self.read(lambda alias, node: [
            {"class": "DEVICES", "devices": [{"path": node, "driver": "SiRF"}]},
            {"class": "TPV", "device": node, "mode": 3, "lat": 33.3, "lon": -111.7},
        ])
        self.assertTrue(result["registered"])
        self.assertTrue(result["has_fix"])
        self.assertEqual(result["driver"], "SiRF")
        self.assertNotIn("lat", result)
        self.assertNotIn("lon", result)

    def test_no_satellite_fix_is_distinct_from_no_device(self):
        result = self.read(lambda alias, node: [
            {"class": "DEVICES", "devices": [{"path": alias}]},
            {"class": "TPV", "device": alias, "mode": 1},
        ])
        self.assertTrue(result["registered"])
        self.assertFalse(result["has_fix"])
        self.assertEqual(result["fix_mode"], 1)

    def test_an_empty_or_unrelated_device_list_is_not_success(self):
        for devices in ([], [{"path": "/dev/other_receiver"}], "malformed"):
            with self.subTest(devices=devices):
                result = self.read(lambda alias, node: [{"class": "DEVICES", "devices": devices}])
                self.assertFalse(result["registered"])

    def test_device_appearing_after_the_initial_empty_list_is_recognized(self):
        result = self.read(lambda alias, node: [
            {"class": "DEVICES", "devices": []},
            {"class": "DEVICE", "path": node, "driver": "SiRF"},
            {"class": "TPV", "device": node, "mode": 2},
        ])
        self.assertTrue(result["registered"])
        self.assertEqual(result["fix_mode"], 2)

    def test_gpsd_unreachable_is_reported_as_failure_by_cli(self):
        with patch.object(G, "gpsd_status", side_effect=ConnectionRefusedError("offline")):
            self.assertEqual(G.main(["--check"]), 2)


if __name__ == "__main__":
    unittest.main()
