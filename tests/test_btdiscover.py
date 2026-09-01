"""Candidate selection must be fail-closed: it never guesses which adapter."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hummer_obd import btdiscover
from hummer_obd.btdiscover import (
    ALLOWED_BINARIES,
    Device,
    DisallowedCommand,
    is_named,
    parse_inquiry,
    parse_sdp_channels,
    select_candidate,
    select_spp_channel,
)

INQUIRY = """Scanning ...
\t00:04:3E:AA:BB:CC\tOBDLink MX+
\t11:22:33:44:55:66\tLiving Room TV
\tB8:27:EB:11:22:33\tn/a
"""

SDP_ONE_SPP = """Browsing 00:04:3E:AA:BB:CC ...
Service Name: Serial Port
Service RecHandle: 0x10000
Service Class ID List:
  "Serial Port" (0x1101)
Protocol Descriptor List:
  "L2CAP" (0x0100)
  "RFCOMM" (0x0003)
    Channel: 1

Service Name: Handsfree Gateway
Service Class ID List:
  "Handsfree Audio Gateway" (0x111f)
Protocol Descriptor List:
  "L2CAP" (0x0100)
  "RFCOMM" (0x0003)
    Channel: 12
"""

SDP_TWO_SPP = SDP_ONE_SPP + """
Service Name: SPP Slave
Service Class ID List:
  "Serial Port" (0x1101)
Protocol Descriptor List:
  "L2CAP" (0x0100)
  "RFCOMM" (0x0003)
    Channel: 5
"""

SDP_NO_SPP = """Browsing 00:04:3E:AA:BB:CC ...
Service Name: Handsfree Gateway
Service Class ID List:
  "Handsfree Audio Gateway" (0x111f)
Protocol Descriptor List:
  "RFCOMM" (0x0003)
    Channel: 12
"""


class TestInquiryParsing(unittest.TestCase):
    def test_parses_devices_and_skips_the_banner(self):
        devices = parse_inquiry(INQUIRY)
        self.assertEqual(len(devices), 3)
        self.assertEqual(devices[0], Device("00:04:3E:AA:BB:CC", "OBDLink MX+"))
        self.assertEqual(devices[2].name, "n/a")

    def test_empty_inquiry(self):
        self.assertEqual(parse_inquiry(""), [])
        self.assertEqual(parse_inquiry("Scanning ...\n"), [])

    def test_named_predicate(self):
        self.assertTrue(is_named(Device("00:11:22:33:44:55", "OBDLink MX+")))
        for blank in ("", "n/a", "N/A", "unresolved", "unknown", "  "):
            self.assertFalse(is_named(Device("00:11:22:33:44:55", blank)), blank)


class TestFailClosedSelection(unittest.TestCase):
    def test_exactly_one_obdlink_is_selectable(self):
        selection = select_candidate(parse_inquiry(INQUIRY))
        self.assertEqual(selection.status, "unique")
        self.assertTrue(selection.may_pair)
        self.assertEqual(selection.device.mac, "00:04:3E:AA:BB:CC")

    def test_no_obdlink_means_no_pairing(self):
        devices = [Device("11:22:33:44:55:66", "Living Room TV")]
        selection = select_candidate(devices)
        self.assertEqual(selection.status, "none")
        self.assertFalse(selection.may_pair)

    def test_two_obdlinks_are_refused_not_ranked(self):
        devices = [
            Device("00:04:3E:AA:BB:CC", "OBDLink MX+"),
            Device("00:04:3E:DD:EE:FF", "OBDLink MX+ (spare)"),
        ]
        selection = select_candidate(devices)
        self.assertEqual(selection.status, "ambiguous")
        self.assertFalse(selection.may_pair)
        self.assertIsNone(selection.device)
        self.assertIn("human must confirm", selection.reason)

    def test_unnamed_device_is_never_a_candidate(self):
        for blank in ("n/a", "", "unresolved"):
            selection = select_candidate([Device("00:04:3E:AA:BB:CC", blank)])
            with self.subTest(name=blank):
                self.assertEqual(selection.status, "none")
                self.assertFalse(selection.may_pair)

    def test_unnamed_devices_do_not_block_a_unique_named_one(self):
        devices = [
            Device("11:11:11:11:11:11", "n/a"),
            Device("00:04:3E:AA:BB:CC", "OBDLink MX+"),
            Device("22:22:22:22:22:22", ""),
        ]
        selection = select_candidate(devices)
        self.assertEqual(selection.status, "unique")
        self.assertEqual(len(selection.ignored), 2)

    def test_the_same_adapter_seen_twice_is_one_adapter(self):
        devices = [
            Device("00:04:3E:AA:BB:CC", "OBDLink MX+"),
            Device("00:04:3E:AA:BB:CC", "OBDLink MX+"),
        ]
        self.assertEqual(select_candidate(devices).status, "unique")

    def test_lookalike_dongles_do_not_match(self):
        for name in ("ELM327 v1.5", "OBDII Adapter", "Vgate iCar Pro", "MX+", "OBD2"):
            with self.subTest(name=name):
                selection = select_candidate([Device("00:04:3E:AA:BB:CC", name)])
                self.assertEqual(selection.status, "none")

    def test_match_is_case_insensitive(self):
        selection = select_candidate([Device("00:04:3E:AA:BB:CC", "obdlink mx plus")])
        self.assertEqual(selection.status, "unique")

    def test_empty_scan(self):
        self.assertEqual(select_candidate([]).status, "none")


class TestSppChannelSelection(unittest.TestCase):
    def test_single_serial_port_record(self):
        self.assertEqual(parse_sdp_channels(SDP_ONE_SPP), [1])
        channel, reason = select_spp_channel(SDP_ONE_SPP)
        self.assertEqual(channel, 1)
        self.assertIn("channel 1", reason)

    def test_non_serial_profiles_are_ignored(self):
        self.assertNotIn(12, parse_sdp_channels(SDP_ONE_SPP))

    def test_several_serial_channels_are_refused(self):
        channel, reason = select_spp_channel(SDP_TWO_SPP)
        self.assertIsNone(channel)
        self.assertIn("refusing to guess", reason)

    def test_no_serial_port_is_refused(self):
        channel, reason = select_spp_channel(SDP_NO_SPP)
        self.assertIsNone(channel)
        self.assertIn("no Serial Port", reason)

    def test_empty_sdp_output(self):
        self.assertEqual(select_spp_channel("")[0], None)


class TestCannotTouchTheVehicle(unittest.TestCase):
    def test_command_allowlist_contains_no_serial_tooling(self):
        self.assertEqual(
            ALLOWED_BINARIES,
            frozenset({"hcitool", "bluetoothctl", "sdptool", "rfcomm", "systemctl"}),
        )

    def test_running_anything_else_is_refused(self):
        for argv in (["python3", "-m", "hummer_obd.probe"], ["cat", "/dev/rfcomm0"],
                     ["/usr/bin/minicom"], []):
            with self.subTest(argv=argv):
                with self.assertRaises(DisallowedCommand):
                    btdiscover._run(argv)

    def test_module_does_not_import_the_serial_transport(self):
        source = Path(btdiscover.__file__).read_text()
        for forbidden in ("import serial", "from .transport", "hummer_obd.transport",
                          "/dev/rfcomm0\", \"w", "open(\"/dev/rfcomm"):
            self.assertNotIn(forbidden, source)


class TestWatchLoop(unittest.TestCase):
    """The loop drives recovery only: it binds a bonded adapter, refuses
    ambiguity, and asks for a human when nothing is bonded."""

    MAC = "00:04:3E:AA:BB:CC"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def _watch(self, ready, not_bonded=(), visible=(), once=True, dry_run=False):
        with mock.patch.object(btdiscover, "known_ready_obdlinks",
                               return_value=(list(ready), list(not_bonded))), \
                mock.patch.object(btdiscover, "inquiry", return_value=list(visible)), \
                mock.patch.object(btdiscover, "bind_known_adapter", return_value=True) as bind:
            rc = btdiscover.watch(self.root, interval=0, once=once, dry_run=dry_run,
                                  log=lambda *_: None)
        return rc, bind

    def test_one_bonded_adapter_is_bound_and_the_watcher_stops(self):
        device = Device(self.MAC, "OBDLink MX+ 56122")
        rc, bind = self._watch([device], once=False)
        self.assertEqual(rc, 0)
        self.assertEqual(bind.call_count, 1)
        self.assertEqual(bind.call_args[0][0].mac, self.MAC)

    def test_two_bonded_adapters_are_refused_not_ranked(self):
        devices = [Device(self.MAC, "OBDLink MX+"), Device("00:04:3E:DD:EE:FF", "OBDLink MX+")]
        rc, bind = self._watch(devices)
        self.assertEqual(rc, 0)
        bind.assert_not_called()
        self.assertIn("AMBIGUOUS", (self.root / "evidence" / "obdlink-pairing.txt").read_text())

    def test_nothing_bonded_asks_for_a_human_and_never_pairs(self):
        seen = Device(self.MAC, "OBDLink MX+ 56122")
        rc, bind = self._watch([], not_bonded=[seen])
        self.assertEqual(rc, 0)
        bind.assert_not_called()
        evidence = (self.root / "evidence" / "obdlink-pairing.txt").read_text()
        self.assertIn("NEEDS A HUMAN", evidence)
        self.assertIn("KeyboardDisplay", evidence)

    def test_dry_run_stops_before_touching_sdp_or_rfcomm(self):
        rc, bind = self._watch([Device(self.MAC, "OBDLink MX+")], dry_run=True)
        self.assertEqual(rc, 0)
        bind.assert_not_called()

    def test_quiet_radio_keeps_watching_without_acting(self):
        rc, bind = self._watch([])
        self.assertEqual(rc, 0)
        bind.assert_not_called()


class FakeCompleted:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


class TestRecoveryOfAPairedAdapter(unittest.TestCase):
    """The MX+ refuses unattended pairing (NoInputNoOutput gives
    org.bluez.Error.AuthenticationFailed); the association that works is
    interactive SSP with a KeyboardDisplay agent. So the durable watcher only
    recovers an adapter a human already bonded — and never pairs anything."""

    MAC = "00:04:3E:AA:BB:CC"
    DEVICES = "Device 00:04:3E:AA:BB:CC OBDLink MX+\nDevice 11:22:33:44:55:66 Living Room TV\n"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.recorder = btdiscover.Recorder(self.root / "evidence" / "pairing.txt")
        self.device = Device(self.MAC, "OBDLink MX+")

    def _fake_run(self, script):
        calls = []

        def runner(argv, timeout=30.0):
            calls.append(list(argv))
            for match, result in script:
                if match(argv):
                    return result
            return FakeCompleted()

        return runner, calls

    # -- known-device parsing ------------------------------------------
    def test_parses_bluetoothctl_devices_output(self):
        devices = btdiscover.parse_devices_list(self.DEVICES)
        self.assertEqual(devices[0], Device(self.MAC, "OBDLink MX+"))
        self.assertEqual(len(devices), 2)

    def test_bond_requires_paired_bonded_and_trusted(self):
        self.assertTrue(btdiscover.is_bonded_ready("Paired: yes\nBonded: yes\nTrusted: yes"))
        for text in ("Paired: yes\nBonded: no\nTrusted: yes",
                     "Paired: no\nBonded: no\nTrusted: yes",
                     "Paired: yes\nBonded: yes\nTrusted: no",
                     ""):
            with self.subTest(text=text.replace("\n", " | ")):
                self.assertFalse(btdiscover.is_bonded_ready(text))

    def test_known_selection_is_fail_closed(self):
        one = [Device(self.MAC, "OBDLink MX+")]
        self.assertEqual(btdiscover.select_known_candidate(one).status, "unique")
        self.assertEqual(btdiscover.select_known_candidate([]).status, "none")
        two = one + [Device("00:04:3E:11:22:33", "OBDLink MX+ spare")]
        selection = btdiscover.select_known_candidate(two)
        self.assertEqual(selection.status, "ambiguous")
        self.assertFalse(selection.may_pair)
        self.assertIn("human must confirm", selection.reason)

    def test_unbonded_known_device_is_not_recoverable(self):
        script = [
            (lambda a: "devices" in a, FakeCompleted(self.DEVICES)),
            (lambda a: "info" in a, FakeCompleted("Paired: no\nBonded: no\nTrusted: yes")),
        ]
        runner, _ = self._fake_run(script)
        with mock.patch.object(btdiscover, "_run", runner):
            ready, not_ready = btdiscover.known_ready_obdlinks()
        self.assertEqual(ready, [])
        self.assertEqual([d.mac for d in not_ready], [self.MAC])

    def test_bonded_known_device_is_recoverable_and_tvs_are_ignored(self):
        script = [
            (lambda a: "devices" in a, FakeCompleted(self.DEVICES)),
            (lambda a: "info" in a, FakeCompleted("Paired: yes\nBonded: yes\nTrusted: yes")),
        ]
        runner, calls = self._fake_run(script)
        with mock.patch.object(btdiscover, "_run", runner):
            ready, not_ready = btdiscover.known_ready_obdlinks()
        self.assertEqual([d.mac for d in ready], [self.MAC])
        self.assertNotIn("11:22:33:44:55:66", " ".join(" ".join(c) for c in calls))

    # -- binding --------------------------------------------------------
    def test_bind_uses_the_single_spp_channel(self):
        script = [(lambda a: a[0] == "sdptool", FakeCompleted(SDP_ONE_SPP))]
        runner, calls = self._fake_run(script)
        written = {}
        with mock.patch.object(btdiscover, "_run", runner), \
                mock.patch.object(btdiscover.Path, "exists", return_value=True), \
                mock.patch.object(btdiscover.Path, "write_text",
                                  lambda self, text: written.update(text=text)):
            ok = btdiscover.bind_known_adapter(self.device, self.recorder, root=self.root)
        self.assertTrue(ok)
        self.assertIn("SPP_CHANNEL=1", written["text"])
        self.assertIn(f"ADAPTER_MAC={self.MAC}", written["text"])

    def test_bind_refuses_without_exactly_one_spp_channel(self):
        for sdp in (SDP_TWO_SPP, SDP_NO_SPP, ""):
            with self.subTest(sdp=sdp[:20]):
                recorder = btdiscover.Recorder(self.root / f"ev{len(sdp)}.txt")
                script = [(lambda a: a[0] == "sdptool", FakeCompleted(sdp))]
                runner, calls = self._fake_run(script)
                with mock.patch.object(btdiscover, "_run", runner):
                    ok = btdiscover.bind_known_adapter(self.device, recorder, root=self.root)
                self.assertFalse(ok)
                self.assertFalse([c for c in calls if c[0] == "rfcomm"])
                self.assertFalse([c for c in calls if c[0] == "systemctl"])

    def test_unwritable_default_file_reports_the_sudo_step_instead_of_failing_silently(self):
        script = [(lambda a: a[0] == "sdptool", FakeCompleted(SDP_ONE_SPP))]
        runner, calls = self._fake_run(script)

        def denied(self, text):
            raise PermissionError("Operation not permitted")

        with mock.patch.object(btdiscover, "_run", runner), \
                mock.patch.object(btdiscover.Path, "write_text", denied):
            ok = btdiscover.bind_known_adapter(self.device, self.recorder, root=self.root)
        self.assertFalse(ok)
        evidence = self.recorder.path.read_text()
        self.assertIn("sudo sh -c", evidence)
        self.assertIn("SPP_CHANNEL=1", evidence)
        self.assertFalse([c for c in calls if c[0] == "rfcomm"])

    # -- the watcher never pairs ----------------------------------------
    def test_the_watcher_never_issues_a_pair_command(self):
        script = [
            (lambda a: "devices" in a, FakeCompleted(self.DEVICES)),
            (lambda a: "info" in a, FakeCompleted("Paired: no\nBonded: no\nTrusted: yes")),
            (lambda a: a[0] == "hcitool", FakeCompleted("Scanning ...\n\t%s\tOBDLink MX+ 56122\n" % self.MAC)),
        ]
        runner, calls = self._fake_run(script)
        with mock.patch.object(btdiscover, "_run", runner):
            rc = btdiscover.watch(self.root, interval=0, once=True, dry_run=False,
                                  log=lambda *_: None)
        self.assertEqual(rc, 0)
        for call in calls:
            self.assertNotIn("pair", call)
            self.assertNotIn("trust", call)
            self.assertNotIn("remove", call)
        evidence = (self.root / "evidence" / "obdlink-pairing.txt").read_text()
        self.assertIn("NEEDS A HUMAN", evidence)
        self.assertIn("KeyboardDisplay", evidence)

    def test_no_code_path_can_pair_trust_or_unpair(self):
        self.assertFalse(hasattr(btdiscover, "pair_argv"))
        self.assertFalse(hasattr(btdiscover, "pair_trust_bind"))
        source = Path(btdiscover.__file__).read_text()
        # The prose explains why unattended pairing fails; what must not exist
        # is an argv literal that would attempt it.
        for literal in ('"--agent"', '"pair"', '"trust"', '"remove"'):
            self.assertNotIn(literal, source, f"{literal} is still used as a command argument")
