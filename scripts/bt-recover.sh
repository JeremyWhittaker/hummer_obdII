#!/usr/bin/env bash
# Bring the node's Bluetooth back when it has wedged.
#
# The Pi Zero 2 W's Bluetooth sits behind a UART, and it can reach a state
# where the controller stops answering HCI_Reset -- the very first command of
# initialisation. The kernel says so plainly:
#
#     Bluetooth: hci0: Opcode 0x0c03 failed: -110
#
# At that point nothing above the kernel can help. `bluetoothctl connect`
# fails with br-connection-adapter-not-powered, `hciconfig hci0 up` times out,
# and restarting bluetoothd restarts a daemon that has no working controller
# to talk to. All three were tried on 2026-09-09 and all three failed.
#
# So this climbs past them, cheapest first, and stops the moment the
# controller answers. Reloading hci_uart is the rung that usually works and
# costs a few seconds; rebooting is last and is never taken automatically.
#
# Safe to run when nothing is wrong: every rung is skipped if the controller
# is already up, and the script exits 0 having done nothing.
set -uo pipefail

REBOOT_IF_NEEDED="${1:-}"
OBD_MAC="00:04:3E:84:BD:82"
R8_MAC="E0:00:00:00:2C:A7"

say() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*"; }

controller_up() { hciconfig hci0 2>/dev/null | grep -q 'UP RUNNING'; }
device_up() { bluetoothctl info "$1" 2>/dev/null | grep -q 'Connected: yes'; }

report() {
  say "controller: $(hciconfig hci0 2>/dev/null | sed -n 3p | tr -d '\t' || echo unknown)"
  for mac in "$OBD_MAC" "$R8_MAC"; do
    say "  $mac $(bluetoothctl info "$mac" 2>/dev/null | grep 'Connected:' | tr -d '\t')"
  done
  say "  rfcomm0 $(rfcomm show 0 2>/dev/null | sed 's/^[^ ]* //' || echo 'not bound')"
}

healthy() { controller_up && { device_up "$OBD_MAC" || device_up "$R8_MAC"; }; }

say "=== before ==="; report

if healthy; then
  say "nothing to do: the controller is up and at least one device is connected"
  exit 0
fi

# --- rung 1: the controller is up but nothing is connected -------------------
if controller_up; then
  say "rung 1: controller is up, reconnecting devices"
  for mac in "$OBD_MAC" "$R8_MAC"; do
    timeout 20 bluetoothctl connect "$mac" >/dev/null 2>&1 || true
  done
  sleep 4
  if healthy; then say "recovered at rung 1"; report; exit 0; fi
fi

# --- rung 2: bring the controller up ----------------------------------------
say "rung 2: hciconfig hci0 up"
sudo -n hciconfig hci0 up 2>&1 | sed 's/^/    /' || true
sleep 4
if controller_up; then
  for mac in "$OBD_MAC" "$R8_MAC"; do
    timeout 20 bluetoothctl connect "$mac" >/dev/null 2>&1 || true
  done
  sleep 4
  if healthy; then say "recovered at rung 2"; report; exit 0; fi
fi

# --- rung 3: restart the daemon ---------------------------------------------
say "rung 3: restart bluetoothd"
sudo -n systemctl restart bluetooth || say "    (no permission; see scripts/bt-grant.sh)"
sleep 8
if controller_up; then
  for mac in "$OBD_MAC" "$R8_MAC"; do
    timeout 20 bluetoothctl connect "$mac" >/dev/null 2>&1 || true
  done
  sleep 4
  if healthy; then say "recovered at rung 3"; report; exit 0; fi
fi

# --- rung 4: reload the UART driver -----------------------------------------
# This is the one that fixes a wedged controller, because it is the first rung
# that touches the layer the fault is actually at.
say "rung 4: reload hci_uart"
sudo -n systemctl stop bluetooth 2>/dev/null || true
sudo -n modprobe -r hci_uart 2>&1 | sed 's/^/    /' || say "    (no permission for modprobe)"
sleep 2
sudo -n modprobe hci_uart 2>&1 | sed 's/^/    /' || true
sleep 3
sudo -n systemctl start bluetooth 2>/dev/null || true
sleep 8
sudo -n hciconfig hci0 up 2>&1 | sed 's/^/    /' || true
sleep 4
if controller_up; then
  for mac in "$OBD_MAC" "$R8_MAC"; do
    timeout 20 bluetoothctl connect "$mac" >/dev/null 2>&1 || true
  done
  sleep 4
  sudo -n systemctl restart hummer-rfcomm 2>/dev/null || true
  sleep 4
  sudo -n systemctl restart hummer-drive 2>/dev/null || true
  if healthy; then say "recovered at rung 4"; report; exit 0; fi
fi

say "=== after ==="; report
say "Bluetooth did not recover."

if [ "$REBOOT_IF_NEEDED" = "--reboot" ]; then
  say "rebooting, as asked"
  sudo -n systemctl reboot || say "    (no permission to reboot)"
  exit 0
fi

say "A reboot is the remaining fix. Re-run with --reboot to take it,"
say "or run: sudo reboot"
exit 1
