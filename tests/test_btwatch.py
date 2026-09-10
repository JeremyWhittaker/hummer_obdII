"""The watchdog must fix the fault it was built for and ignore everything else.

An over-eager Bluetooth watchdog is worse than none: the OBD adapter drops off
every time the vehicle sleeps, which is most of every day, and a program that
resets the controller each time would spend its life fighting normal
behaviour. So most of these tests are about restraint.

Nothing here shells out. Every external command is faked, because a test suite
that depends on a Bluetooth controller being present fails for reasons that
are not about this code.
"""

import pathlib
import unittest
from unittest.mock import patch

from hummer_obd import btwatch


def info(connected):
    """A `bluetoothctl info` transcript for a device in the given state."""
    return (0, "Device 00:04:3E:84:BD:82 (public)\n"
               "\tName: OBDLink MX+\n"
               f"\tConnected: {'yes' if connected else 'no'}\n"
               "\tPaired: yes\n")


class ReadingStateTests(unittest.TestCase):
    def test_connected_is_read_from_bluetoothctl(self):
        with patch.object(btwatch, "_run", return_value=info(True)):
            self.assertTrue(btwatch.connected("00:11:22:33:44:55"))
        with patch.object(btwatch, "_run", return_value=info(False)):
            self.assertFalse(btwatch.connected("00:11:22:33:44:55"))

    def test_an_unanswerable_controller_says_it_does_not_know(self):
        # None is not False. "Cannot tell" must never become "broken", or the
        # watchdog starts restarting a stack that was fine.
        with patch.object(btwatch, "_run", return_value=(1, "No default controller")):
            self.assertIsNone(btwatch.connected("00:11:22:33:44:55"))
        with patch.object(btwatch, "_run", return_value=(0, "no Connected line here")):
            self.assertIsNone(btwatch.connected("00:11:22:33:44:55"))

    def test_a_command_that_hangs_is_bounded_not_awaited(self):
        import subprocess
        with patch.object(btwatch.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("bluetoothctl", 25)):
            code, out = btwatch._run(["bluetoothctl", "info", "x"])
        self.assertEqual(code, 124)
        self.assertIn("timed out", out)

    def test_a_missing_tool_is_reported_not_raised(self):
        with patch.object(btwatch.subprocess, "run", side_effect=FileNotFoundError("no rfcomm")):
            code, out = btwatch._run(["rfcomm", "show", "0"])
        self.assertEqual(code, 127)
        self.assertIn("FileNotFoundError", out)

    def test_the_rfcomm_binding_state_is_read(self):
        line = "rfcomm0: 00:04:3E:84:BD:82 channel 1 closed [tty-attached]\n"
        with patch.object(btwatch, "_run", return_value=(0, line)):
            self.assertEqual(btwatch.rfcomm_state(), "closed")
        with patch.object(btwatch, "_run", return_value=(1, "")):
            self.assertIsNone(btwatch.rfcomm_state())


class RestraintTests(unittest.TestCase):
    """What it must NOT do."""

    def dog(self):
        return btwatch.Watchdog(say=lambda m: None)

    def test_a_healthy_link_does_nothing_at_all(self):
        dog = self.dog()
        dog.step(btwatch.Health(obd=True, radar=True, rfcomm="connected"))
        self.assertEqual(dog.actions, [])
        self.assertEqual(dog.strikes, 0)

    def test_the_adapter_alone_dropping_does_nothing(self):
        # This is every night of this vehicle's life: the truck sleeps, the
        # OBD adapter goes with it, the detector stays up. Not a fault.
        dog = self.dog()
        for _ in range(10):
            dog.step(btwatch.Health(obd=False, radar=True, rfcomm="clean"))
        self.assertEqual(dog.actions, [])
        self.assertEqual(dog.strikes, 0)

    def test_an_unknown_state_does_nothing(self):
        dog = self.dog()
        for _ in range(10):
            dog.step(btwatch.Health(obd=None, radar=None, rfcomm=None))
        self.assertEqual(dog.actions, [])

    def test_one_device_down_and_one_unknown_does_nothing(self):
        # Half a picture is not evidence of the fault this repairs.
        dog = self.dog()
        for _ in range(10):
            dog.step(btwatch.Health(obd=False, radar=None, rfcomm="closed"))
        self.assertEqual(dog.actions, [])

    def test_recovery_forgets_the_strikes(self):
        dog = self.dog()
        down = btwatch.Health(obd=False, radar=False, rfcomm="closed")
        with patch.object(dog, "_act", return_value=True):
            dog.step(down)
            dog.step(down)
        self.assertEqual(dog.strikes, 2)
        dog.step(btwatch.Health(obd=True, radar=False, rfcomm="connected"))
        self.assertEqual(dog.strikes, 0)


class LadderTests(unittest.TestCase):
    """It climbs one rung at a time, and only when the rung below has failed."""

    def climb(self, steps):
        dog = btwatch.Watchdog(say=lambda m: None)
        done = []
        with patch.object(dog, "_act",
                          side_effect=lambda name, argv: done.append(name) or True):
            for _ in range(steps):
                dog.actions.clear()
                dog.step(btwatch.Health(obd=False, radar=False, rfcomm="closed"))
        return done

    def test_the_first_strike_only_tries_to_connect(self):
        done = self.climb(1)
        self.assertTrue(all(d.startswith("connect ") for d in done), done)
        self.assertEqual(len(done), 2, "both devices, nothing else")

    def test_the_controller_is_not_reset_before_connecting_has_failed(self):
        done = self.climb(btwatch.RESET_AFTER - 1)
        self.assertNotIn("reset-controller", done)

    def test_the_controller_reset_comes_next(self):
        done = self.climb(btwatch.RESET_AFTER)
        self.assertIn("reset-controller", done)
        self.assertNotIn("restart-bluetoothd", done)

    def test_the_daemon_is_restarted_before_the_driver_is_reloaded(self):
        done = self.climb(btwatch.RESTART_AFTER)
        self.assertIn("restart-bluetoothd", done)
        self.assertNotIn("unload-hci-uart", done)

    def test_the_driver_reload_is_the_top_rung(self):
        # The rung that actually repairs a controller which has stopped
        # answering HCI_Reset. Every rung below it restarts something that has
        # no working controller to talk to -- proven on 2026-09-09, when all
        # three were tried by hand and all three failed.
        done = self.climb(btwatch.RELOAD_AFTER)
        self.assertIn("unload-hci-uart", done)
        self.assertIn("load-hci-uart", done)
        self.assertLess(done.index("stop-bluetoothd"), done.index("unload-hci-uart"),
                        "the module is busy while bluetoothd holds it")
        self.assertLess(done.index("load-hci-uart"), done.index("start-bluetoothd"),
                        "starting the daemon before the driver exists is pointless")

    def test_the_ladder_is_ordered_and_spaced(self):
        self.assertLess(btwatch.RECONNECT_AFTER, btwatch.RESET_AFTER)
        self.assertLess(btwatch.RESET_AFTER, btwatch.RESTART_AFTER)
        self.assertLess(btwatch.RESTART_AFTER, btwatch.RELOAD_AFTER)

    def test_rebinding_accompanies_every_stack_level_repair(self):
        # A controller reset or a daemon restart leaves the RFCOMM binding
        # stale, which is the exact state that started all of this: a binding
        # that exists with no link behind it, which reopening cannot fix.
        for steps in (btwatch.RESET_AFTER, btwatch.RESTART_AFTER):
            with self.subTest(steps=steps):
                self.assertIn("rebind-rfcomm", self.climb(steps))


class SafetyTests(unittest.TestCase):
    def test_every_command_it_can_run_is_node_local_bluetooth(self):
        # This runs as root, so what matters is not which words appear in the
        # source but which executables it can actually invoke. Parsed from the
        # source rather than asserted by hand, so a new rung on the ladder
        # cannot smuggle in a command without this failing.
        #
        # The first version of this test grepped for the string "obd" and
        # failed on the word "OBD adapter" in a comment, which is the kind of
        # test that gets deleted rather than fixed. This one has a claim.
        import ast
        tree = ast.parse(pathlib.Path(btwatch.__file__).read_text(encoding="utf-8"))
        executables = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.List) or not node.elts:
                continue
            first = node.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                if all(isinstance(e, ast.Constant) or isinstance(e, ast.JoinedStr)
                       or isinstance(e, ast.Name) or isinstance(e, ast.Call)
                       for e in node.elts):
                    executables.add(first.value)
        allowed = {"bluetoothctl", "rfcomm", "hciconfig", "systemctl", "modprobe"}
        stray = {e for e in executables if "/" in e or e.startswith("AT")}
        self.assertEqual(stray, set(), f"non-Bluetooth executable: {stray}")
        self.assertTrue(executables <= allowed | {"connected", "clean", "closed",
                                                  "listening", "connecting"},
                        f"unexpected executables: {executables - allowed}")

    def test_the_only_services_it_touches_are_the_nodes_own(self):
        source = pathlib.Path(btwatch.__file__).read_text(encoding="utf-8")
        import re as _re
        units = set(_re.findall(r'"systemctl", "(?:restart|stop|start)", "([a-z0-9-]+)"',
                                source))
        self.assertEqual(units, {"bluetooth", "hummer-rfcomm"},
                         "the watchdog restarts a service it was not meant to")
        self.assertNotIn("hummer-drive", units,
                         "restarting the recorder would discard a session in progress")

    def test_the_continuous_mode_refuses_to_spin(self):
        with self.assertRaises(SystemExit):
            btwatch.main(["--interval-s", "1"])

    def test_a_dry_run_changes_nothing(self):
        with patch.object(btwatch, "look",
                          return_value=btwatch.Health(obd=False, radar=False, rfcomm="closed")), \
             patch.object(btwatch, "_run") as ran:
            btwatch.main(["--once", "--dry-run", "--json"])
        ran.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
