"""Keep the node's Bluetooth links alive, and say what it did.

Two devices reach this node over Bluetooth: the OBD adapter, on an RFCOMM
binding the recorder opens as a serial port, and the radar detector, read by a
sibling project. On 2026-09-09 both were paired, the controller was ``UP
RUNNING`` with zero errors, ``bluetoothd`` was active -- and neither was
connected. Nothing in the stack reported a fault, because at every layer it
inspects there wasn't one. The recorder logged "adapter still silent;
reopening the link" for an hour, and reopening could never have helped: the
RFCOMM binding had gone stale and rebinding needs privileges the recorder does
not have.

So this runs as root on a timer and does what the recorder cannot. It is
deliberately a ladder rather than a hammer: the cheapest action that could fix
the observed state is tried first, and each rung is only reached after the one
below it has failed. A watchdog that restarts the Bluetooth daemon every time
a sleeping adapter drops off would be worse than the fault it is chasing.

The whole thing is read-only with respect to the vehicle. It touches the
node's own Bluetooth stack and nothing else; no rung of the ladder sends a
byte to a vehicle module.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

#: The devices this node expects to be able to reach. Addresses only -- names
#: are for the log. A device that is not paired is not this program's problem.
OBD_ADAPTER = "00:04:3E:84:BD:82"
RADAR_DETECTOR = "E0:00:00:00:2C:A7"

#: How many consecutive unhealthy checks before each rung of the ladder. The
#: gaps matter: an OBD adapter legitimately drops when the vehicle sleeps, and
#: a watchdog that resets the controller the moment that happens would spend
#: its life fighting normal behaviour.
RECONNECT_AFTER = 1
RESET_AFTER = 3
RESTART_AFTER = 6
#: The rung that actually repairs a wedged controller, and the reason the
#: three above it are not enough. When the chip stops answering HCI_Reset --
#: ``Bluetooth: hci0: Opcode 0x0c03 failed: -110`` in the kernel log -- every
#: remedy above the driver is restarting something that has no working
#: controller to talk to. All three were tried on 2026-09-09 and all three
#: failed. Reloading the UART driver is the first rung that touches the layer
#: the fault is at.
RELOAD_AFTER = 9
#: Once the top rung has fired, repeat it at most once per this many checks.
RELOAD_EVERY = 30
#: The kernel's own words for a controller that has stopped answering: HCI
#: command transmit timeouts and opcodes failing with -110 (ETIMEDOUT).
WEDGE_PATTERN = re.compile(r"hci0: (command 0x[0-9a-f]+ tx timeout|Opcode 0x[0-9a-f]+ failed: -110)",
                           re.IGNORECASE)
WEDGE_WINDOW_S = 600

#: Nothing is attempted more often than this, whatever the timer does.
MIN_INTERVAL_S = 45.0

#: Every external command is bounded. A watchdog that hangs is not a watchdog.
COMMAND_TIMEOUT_S = 25.0


def _run(argv: list[str], timeout: float = COMMAND_TIMEOUT_S) -> tuple[int, str]:
    """Run *argv*, returning (returncode, combined output).

    Never raises: this is recovery code, and an exception here would replace a
    recoverable fault with an unrecoverable one.
    """
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s"
    except (OSError, subprocess.SubprocessError) as error:
        return 127, f"{type(error).__name__}: {error}"
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def connected(address: str) -> Optional[bool]:
    """Whether *address* is connected, or None if that cannot be determined.

    ``None`` is not ``False``. A controller that cannot be interrogated tells
    us nothing about the device, and treating "do not know" as "broken" is how
    a watchdog starts restarting a working stack.
    """
    code, out = _run(["bluetoothctl", "info", address])
    if code != 0:
        return None
    match = re.search(r"^\s*Connected:\s*(yes|no)\s*$", out, re.M)
    return None if match is None else match.group(1) == "yes"


def rfcomm_state(index: int = 0) -> Optional[str]:
    """What the RFCOMM binding is doing: connected, clean, closed, or None."""
    code, out = _run(["rfcomm", "show", str(index)], timeout=6.0)
    if code != 0:
        return None
    words = out.split()
    for state in ("connected", "clean", "closed", "listening", "connecting"):
        if state in words:
            return state
    return None


@dataclass
class Health:
    """One look at the node's Bluetooth, and whether it needs help."""

    obd: Optional[bool] = None
    radar: Optional[bool] = None
    rfcomm: Optional[str] = None

    @property
    def any_connected(self) -> bool:
        return self.obd is True or self.radar is True

    #: True when the controller itself is not up, whatever the devices say.
    controller: Optional[bool] = None
    #: True when the kernel logged HCI command timeouts in the last few minutes.
    wedged: Optional[bool] = None

    @property
    def all_known_down(self) -> bool:
        """True only when both devices are *known* to be disconnected.

        Both, not either: the OBD adapter alone dropping is ordinary -- it
        sleeps with the vehicle. Both at once, while the controller claims to
        be healthy, is the shape of the fault this exists for, and it is what
        the detector being down alongside the adapter revealed on 2026-09-09.
        """
        return self.obd is False and self.radar is False

    def describe(self) -> str:
        def say(value):
            return "?" if value is None else ("up" if value else "down")
        return (f"controller={say(self.controller)} obd={say(self.obd)} "
                f"radar={say(self.radar)} rfcomm={self.rfcomm or '?'}"
                + (" kernel=hci-timeouts" if self.wedged else ""))


def controller_up() -> Optional[bool]:
    """Whether hci0 is UP RUNNING, or None if that cannot be determined."""
    code, out = _run(["hciconfig", "hci0"], timeout=6.0)
    if code != 0:
        return None
    return "UP RUNNING" in out


def controller_wedged(window_s: float = WEDGE_WINDOW_S) -> Optional[bool]:
    """Whether the kernel logged HCI command timeouts recently, or None if unreadable."""
    code, out = _run(["journalctl", "-k", "--since", f"-{int(window_s)}s",
                      "--no-pager", "-o", "cat"], timeout=10.0)
    if code != 0:
        return None
    return bool(WEDGE_PATTERN.search(out))


def look(obd: str = OBD_ADAPTER, radar: str = RADAR_DETECTOR) -> Health:
    return Health(obd=connected(obd), radar=connected(radar),
                  rfcomm=rfcomm_state(), controller=controller_up(),
                  wedged=controller_wedged())


@dataclass
class Watchdog:
    """The escalation ladder, and the memory of how far up it we are."""

    obd: str = OBD_ADAPTER
    radar: str = RADAR_DETECTOR
    say: Callable[[str], None] = lambda message: None
    strikes: int = 0
    actions: list = field(default_factory=list)

    def _act(self, name: str, argv: list[str]) -> bool:
        code, out = _run(argv)
        first = out.strip().splitlines()[0] if out.strip() else ""
        self.actions.append({"action": name, "rc": code, "detail": first[:120]})
        self.say(f"  {name}: rc={code} {first[:90]}")
        return code == 0

    def step(self, health: Optional[Health] = None) -> Health:
        """One pass: look, then climb no further than the state justifies."""
        state = look(self.obd, self.radar) if health is None else health

        if state.any_connected:
            if self.strikes:
                self.say(f"recovered after {self.strikes} unhealthy checks "
                         f"({state.describe()})")
            self.strikes = 0
            return state

        if not state.all_known_down:
            # One device down, or a controller that would not answer. Neither
            # justifies touching anything: a sleeping vehicle takes the OBD
            # adapter with it, every single night.
            self.say(f"nothing to do ({state.describe()})")
            return state

        self.strikes += 1
        self.say(f"both links down, strike {self.strikes} ({state.describe()})")

        # Both devices switched off with the truck looks exactly like this, and
        # reconnect attempts time out the same way either way (161 of 162 over
        # four days). Past the reset rung, act only on evidence that the stack
        # itself is at fault: a controller not reporting UP RUNNING, or the
        # kernel's own command timeouts. The reset rung stays unconditional --
        # harmless on a healthy chip, and on a wedged one it produces exactly
        # that kernel evidence.
        stack_fault = state.controller is not True or state.wedged is True
        if self.strikes >= RESTART_AFTER and not stack_fault:
            self.say("  no controller fault evidence; devices may simply be off -- reconnect only")
            for address in (self.obd, self.radar):
                self._act(f"connect {address}", ["bluetoothctl", "connect", address])
            return state
        if self.strikes > RELOAD_AFTER and (self.strikes - RELOAD_AFTER) % RELOAD_EVERY:
            # The top rung has fired; repeating it every minute would fight
            # the stack rather than repair it.
            self.say(f"  top rung cooling down; next driver reload at strike "
                     f"{self.strikes + RELOAD_EVERY - (self.strikes - RELOAD_AFTER) % RELOAD_EVERY}")
            return state

        if self.strikes >= RELOAD_AFTER:
            # The controller is not answering its own reset. Everything above
            # the driver has been tried and failed, repeatedly, so reload the
            # driver. Bluetooth has to be stopped first or the module is busy.
            self._act("stop-bluetoothd", ["systemctl", "stop", "bluetooth"])
            self._act("unload-hci-uart", ["modprobe", "-r", "hci_uart"])
            self._act("load-hci-uart", ["modprobe", "hci_uart"])
            self._act("start-bluetoothd", ["systemctl", "start", "bluetooth"])
            self._act("bring-up-controller", ["hciconfig", "hci0", "up"])
            self._act("rebind-rfcomm", ["systemctl", "restart", "hummer-rfcomm"])
            return state

        if self.strikes >= RESTART_AFTER:
            # Last rung. Everything below it has failed repeatedly, so the
            # daemon itself is the remaining suspect.
            self._act("restart-bluetoothd", ["systemctl", "restart", "bluetooth"])
            self._act("rebind-rfcomm", ["systemctl", "restart", "hummer-rfcomm"])
            return state

        if self.strikes >= RESET_AFTER:
            # The controller is up by its own account and still cannot carry a
            # connection, which a reset does sometimes clear.
            self._act("reset-controller", ["hciconfig", "hci0", "reset"])
            self._act("rebind-rfcomm", ["systemctl", "restart", "hummer-rfcomm"])
            return state

        if self.strikes >= RECONNECT_AFTER:
            # Cheapest thing that could possibly work, and the one that leaves
            # every other service undisturbed.
            for address in (self.obd, self.radar):
                self._act(f"connect {address}", ["bluetoothctl", "connect", address])
        return state


#: Where a timer-driven ``--once`` check remembers its strikes. Each timer run
#: is a fresh process, so without this every check was "strike 1" and the
#: ladder never climbed past reconnecting -- observed 2026-09-18 while the
#: controller was wedged (``hci0: command tx timeout``, ``-110``) and every
#: reconnect timed out, minute after minute. The unit runs under
#: ProtectSystem=strict, ProtectHome=read-only and a per-run PrivateTmp, so
#: /run, the checkout and /tmp are all unusable; /dev/shm is writable there
#: and, like /run, is cleared by a reboot. It is shared and sticky, so the file
#: is only trusted when this user owns it and is never followed as a symlink.
STATE_FILE = "/dev/shm/hummer-btwatch.json"
#: Strikes count *consecutive* unhealthy checks. If the last one is older than
#: this, the timer was stopped or the node slept, and counting resumes at zero.
STATE_STALE_S = 300.0


def load_strikes(path: str, now: Optional[float] = None) -> int:
    """Strikes saved by the previous check, or 0 if absent, stale or unreadable."""
    now = time.time() if now is None else now
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding="utf-8") as handle:
            # A record another user could have planted would let them drive a
            # root process up the ladder; only our own file counts.
            if os.fstat(handle.fileno()).st_uid != os.geteuid():
                return 0
            saved = json.load(handle)
        strikes, stamp = saved["strikes"], saved["ts"]
    except (OSError, ValueError, KeyError, TypeError):
        return 0
    if (type(strikes) is not int or not 0 <= strikes <= 1000
            or not isinstance(stamp, (int, float)) or not 0 <= now - stamp <= STATE_STALE_S):
        return 0
    return strikes


def save_strikes(path: str, strikes: int, now: Optional[float] = None) -> bool:
    """Atomically record the strike count; a failure is reported, not raised."""
    now = time.time() if now is None else now
    directory, name = os.path.split(os.path.abspath(path))
    temporary = None
    try:
        # A fresh, exclusively created temporary cannot be a pre-planted
        # symlink; the atomic rename then replaces only the directory entry.
        fd, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"strikes": strikes, "ts": now}, handle)
        os.replace(temporary, path)
        temporary = None
        return True
    except OSError:
        return False
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Watch the node's Bluetooth links and restore them when both drop.")
    parser.add_argument("--once", action="store_true",
                        help="check once and exit, for a systemd timer")
    parser.add_argument("--interval-s", type=float, default=60.0,
                        help="seconds between checks when running continuously")
    parser.add_argument("--json", action="store_true",
                        help="print one JSON object per check instead of prose")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what it would do and change nothing")
    parser.add_argument("--state-file", default=STATE_FILE,
                        help="where --once remembers strikes between timer runs")
    args = parser.parse_args(argv)

    if args.interval_s < MIN_INTERVAL_S and not args.once:
        parser.error(f"--interval-s below {MIN_INTERVAL_S:.0f} would fight the "
                     f"stack rather than repair it")

    lines: list[str] = []
    dog = Watchdog(say=(lines.append if args.json else
                        (lambda m: print(m, flush=True))))
    if args.dry_run:
        dog._act = lambda name, argv_: (   # type: ignore[assignment]
            dog.actions.append({"action": name, "rc": None, "detail": "dry run"})
            or dog.say(f"  would run: {' '.join(argv_)}") or True)

    if args.once:
        dog.strikes = load_strikes(args.state_file)

    while True:
        dog.actions.clear()
        state = dog.step()
        if args.once and not args.dry_run and not save_strikes(args.state_file, dog.strikes):
            dog.say(f"  cannot record strikes in {args.state_file}; the ladder cannot climb")
        if args.json:
            print(json.dumps({
                "utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "obd": state.obd, "radar": state.radar, "rfcomm": state.rfcomm,
                "controller": state.controller,
                "strikes": dog.strikes, "actions": list(dog.actions),
                "log": list(lines),
            }, allow_nan=False), flush=True)
            lines.clear()
        if args.once:
            return 0
        time.sleep(max(MIN_INTERVAL_S, args.interval_s))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
