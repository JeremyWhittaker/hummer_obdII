"""A script that configures the node must refuse to configure a workstation.

Several scripts here change system state that belongs to the vehicle node:
systemd units, /etc/sudoers.d, /etc/default, kernel modules, Wi-Fi profiles.
They call sudo directly, so they configure whichever machine invokes them --
and they live in a repository that is edited on a workstation, which is
precisely where someone will run one by mistake.

That happened: allow-places-write.sh was run from the workstation, wrote a
systemd drop-in into the workstation's /etc, and only then failed with "Unit
hummer-dashboard.service not found". It littered the wrong machine and fixed
nothing on the right one. The others are worse -- switch_wifi_profile.sh would
reconfigure the workstation's Wi-Fi and bt-recover.sh would unload its
Bluetooth driver, both of which disconnect the person running them.

A comment saying "run this on the Pi" is not a guard, and every one of these
scripts had such a comment. So this test asserts the guard is real code, and
that every script is deliberately classified rather than left to inherit
whichever behaviour its author happened to write.
"""

import pathlib
import subprocess
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
GUARD = SCRIPTS / "lib" / "require-node.sh"

#: Scripts that mutate system state on whatever machine runs them. Each MUST
#: source the guard, or it will happily configure a workstation.
MUST_GUARD = {
    "bootstrap_pi.sh",
    "bt-grant.sh",
    "bt-recover.sh",
    "enable_service_control.sh",
    "pair_obdlink.sh",
    "switch_wifi_profile.sh",
}

#: Scripts that reach the node over ssh and name their target. These must NOT
#: carry the guard: they are meant to be run from a workstation, and a guard
#: would refuse the only correct way to use them.
SSH_TARGETED = {
    "allow-places-write.sh",
    "catch-up-the-node.sh",
    "deploy.sh",
    "deploy-panel.sh",
    "enable-remote-dashboard.sh",
}

#: Scripts that change nothing on any machine -- readers, smoke tests, an
#: alias installer for the operator's own shell.
HARMLESS = {
    "collect_evidence.sh",
    "install_mark_alias.sh",
    "install_waveshare_driver.sh",
    "pi_smoke.sh",
    "run_trial.sh",
}


def scripts():
    return sorted(p for p in SCRIPTS.glob("*.sh") if p.is_file())


class GuardTests(unittest.TestCase):
    def test_the_guard_exists_and_is_valid_shell(self):
        self.assertTrue(GUARD.is_file(), "the guard itself is missing")
        done = subprocess.run(["bash", "-n", str(GUARD)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_guard_refuses_a_machine_that_is_not_a_pi(self):
        # Run it for real rather than reading it. A guard that is syntactically
        # fine and logically inverted is the failure this is here to catch, and
        # the test host is not a Pi, so the refusal path is the one exercised.
        done = subprocess.run(
            ["bash", "-c", f'. "{GUARD}"; require_node; echo REACHED'],
            capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0, "the guard allowed a non-Pi host")
        self.assertNotIn("REACHED", done.stdout)
        self.assertIn("not it", done.stderr + done.stdout)

    def test_the_guard_says_nothing_was_changed(self):
        # The message has a job beyond refusing: the person who just ran this
        # needs to know whether they have to go and clean something up.
        done = subprocess.run(
            ["bash", "-c", f'. "{GUARD}"; require_node'],
            capture_output=True, text=True)
        self.assertIn("Nothing has been changed", done.stderr + done.stdout)


class ClassificationTests(unittest.TestCase):
    def test_every_script_is_classified(self):
        # The point of the pin: a new script cannot be added without someone
        # deciding which machine it is allowed to configure.
        known = MUST_GUARD | SSH_TARGETED | HARMLESS
        found = {p.name for p in scripts()}
        self.assertEqual(found - known, set(),
                         "unclassified script -- decide which machine it configures "
                         "and add it to MUST_GUARD, SSH_TARGETED or HARMLESS")
        self.assertEqual(known - found, set(),
                         "classified script no longer exists; remove the pin")

    def test_each_local_mutating_script_calls_the_guard(self):
        for name in sorted(MUST_GUARD):
            with self.subTest(script=name):
                text = (SCRIPTS / name).read_text(encoding="utf-8")
                self.assertIn("require-node.sh", text,
                              f"{name} does not source the guard")
                self.assertIn("require_node", text,
                              f"{name} sources the guard but never calls it")

    def test_the_guard_is_called_before_anything_is_changed(self):
        # Ordering is the whole lesson. The original failure wrote its file and
        # discovered the problem afterwards, so a guard placed below the first
        # mutation would repeat it exactly.
        for name in sorted(MUST_GUARD):
            with self.subTest(script=name):
                lines = (SCRIPTS / name).read_text(encoding="utf-8").splitlines()
                call = next((i for i, line in enumerate(lines)
                             if line.strip() == "require_node"), None)
                self.assertIsNotNone(call, f"{name} never calls require_node")
                mutation = next(
                    (i for i, line in enumerate(lines)
                     if not line.lstrip().startswith("#")
                     and any(w in line for w in ("sudo ", "systemctl ", "modprobe ",
                                                 "nmcli ", "tee /etc"))),
                    None)
                if mutation is not None:
                    self.assertLess(call, mutation,
                                    f"{name} changes something at line {mutation + 1} "
                                    f"before the guard at line {call + 1}")

    def test_ssh_targeted_scripts_are_not_guarded(self):
        # These are meant to be run from a workstation. Guarding them would
        # refuse the only correct way to use them.
        for name in sorted(SSH_TARGETED):
            with self.subTest(script=name):
                text = (SCRIPTS / name).read_text(encoding="utf-8")
                self.assertNotIn("require_node", text,
                                 f"{name} reaches the node over ssh and must not "
                                 f"demand to be run on it")

    def test_ssh_targeted_scripts_actually_name_a_target(self):
        # The failure mode this whole file exists for is a script that looks
        # remote and is not. "ssh" has to appear with a host, not merely appear.
        for name in sorted(SSH_TARGETED):
            with self.subTest(script=name):
                text = (SCRIPTS / name).read_text(encoding="utf-8")
                self.assertIn("ssh ", text, f"{name} never invokes ssh")
                self.assertTrue(
                    any(tok in text for tok in ("$NODE", "$HOST", "${NODE", "${HOST",
                                                "homeassistant", "jeremy@")),
                    f"{name} invokes ssh but names no target")


class SyntaxTests(unittest.TestCase):
    def test_every_script_parses(self):
        for path in scripts():
            with self.subTest(script=path.name):
                done = subprocess.run(["bash", "-n", str(path)],
                                      capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
