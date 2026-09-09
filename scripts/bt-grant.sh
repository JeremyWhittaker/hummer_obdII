#!/usr/bin/env bash
# Widen the node's passwordless sudo just far enough to repair Bluetooth.
#
# Run this ON THE PI. It asks for a password once, validates the sudoers file
# BEFORE installing it -- a malformed one can lock you out of sudo entirely --
# and refuses to install anything that does not validate.
set -euo pipefail

TARGET=/etc/sudoers.d/hummer-bluetooth
STAGE=$(mktemp)
trap 'rm -f "$STAGE"' EXIT

cat > "$STAGE" <<'RULES'
# Bluetooth link recovery for the Hummer telemetry node.
# Narrow on purpose: exact binaries, exact arguments, no wildcards.
# Nothing here can reach the vehicle. The read-only safety gate that forbids
# writes, ECU reset, SecurityAccess and the rest lives in the Python and is
# untouched by any of these.
jeremy ALL=(root) NOPASSWD: /usr/bin/systemctl restart bluetooth
jeremy ALL=(root) NOPASSWD: /usr/bin/systemctl restart bluetooth.service
jeremy ALL=(root) NOPASSWD: /usr/bin/systemctl stop bluetooth
jeremy ALL=(root) NOPASSWD: /usr/bin/systemctl start bluetooth
jeremy ALL=(root) NOPASSWD: /usr/bin/hciconfig hci0 reset
jeremy ALL=(root) NOPASSWD: /usr/bin/hciconfig hci0 up
jeremy ALL=(root) NOPASSWD: /usr/bin/hciconfig hci0 down
jeremy ALL=(root) NOPASSWD: /usr/bin/rfcomm release 0
jeremy ALL=(root) NOPASSWD: /usr/sbin/modprobe -r hci_uart
jeremy ALL=(root) NOPASSWD: /usr/sbin/modprobe hci_uart
jeremy ALL=(root) NOPASSWD: /sbin/modprobe -r hci_uart
jeremy ALL=(root) NOPASSWD: /sbin/modprobe hci_uart
jeremy ALL=(root) NOPASSWD: /usr/bin/systemctl restart hummer-btwatch.timer
jeremy ALL=(root) NOPASSWD: /usr/bin/systemctl start hummer-btwatch.timer
jeremy ALL=(root) NOPASSWD: /usr/bin/systemctl stop hummer-btwatch.timer
jeremy ALL=(root) NOPASSWD: /usr/bin/systemctl start hummer-btwatch.service
RULES

echo "validating..."
sudo visudo -cf "$STAGE"
echo "installing $TARGET"
sudo install -m 0440 -o root -g root "$STAGE" "$TARGET"
sudo visudo -c >/dev/null
echo "sudoers still valid overall"
echo
echo "checking the grant works:"
sudo -n systemctl restart bluetooth && echo "  bluetooth restart: OK (no password)" \
  || echo "  bluetooth restart: STILL ASKS FOR A PASSWORD"
