#!/bin/bash
# Let the node's own user restart the hummer services without a password.
#
# Run once, as root:
#
#     sudo bash /home/jeremy/hummer-obd/scripts/enable_service_control.sh
#
# Why this exists: the recorder's code lives in the operator's home directory
# and can be updated without any privilege at all, but the running process only
# picks up new code on a restart -- and that needed a password.  So a fix could
# be deployed and then sit inert on disk while the vehicle drove away.  That is
# exactly what happened on 2026-09-03: the wake-detection fix was on the node
# before the drive home and was not running, because nobody was there to type a
# password.
#
# What it grants, and what it deliberately does not:
#
#   * start/stop/restart on the named hummer units, and nothing else.  Each is
#     an exact command string, so no extra arguments can be appended.
#   * NOT a wildcard on systemctl.  `systemctl` can run arbitrary code as root
#     through transient units (`systemd-run`), so a rule like
#     `systemctl *` would be a full root escalation wearing a service-manager
#     costume.  Every command here names one verb and one unit.
#   * The units already run as the operator's own user, so being able to
#     restart them grants no ability that user did not already have.
#
# The sudoers file is syntax-checked with `visudo -c` before it is installed.
# An invalid file in /etc/sudoers.d can break sudo for every user on the
# machine, and recovering from that needs physical access -- which, on a
# machine that lives in a vehicle, may mean a trip to wherever it is parked.
set -euo pipefail

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    echo "ERROR: run this with sudo" >&2
    exit 2
fi

OPERATOR="${SUDO_USER:-jeremy}"
SYSTEMCTL="$(command -v systemctl)"
UNITS=(hummer-drive hummer-rfcomm hummer-battery hummer-display)
SUDOERS=/etc/sudoers.d/hummer-node

echo "# operator: $OPERATOR"
echo "# systemctl: $SYSTEMCTL"

# --- 1. build the rule, one exact command per line -------------------------
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
{
    echo "# Installed by scripts/enable_service_control.sh."
    echo "# Restarting the vehicle recorder must not require a password: the"
    echo "# node is unattended and usually unreachable when it matters."
    for unit in "${UNITS[@]}"; do
        for verb in start stop restart; do
            echo "$OPERATOR ALL=(root) NOPASSWD: $SYSTEMCTL $verb $unit"
            echo "$OPERATOR ALL=(root) NOPASSWD: $SYSTEMCTL $verb $unit.service"
        done
    done
} > "$tmp"

# --- 2. refuse to install anything sudo cannot parse ------------------------
if ! visudo -cf "$tmp" >/dev/null; then
    echo "ERROR: generated sudoers rule is invalid; nothing was installed" >&2
    visudo -cf "$tmp" || true
    exit 3
fi

install -m 0440 -o root -g root "$tmp" "$SUDOERS"
echo "# installed $SUDOERS ($(wc -l < "$SUDOERS") rules)"

# --- 3. make the tuning files readable (they hold no secrets) --------------
# Read-only on purpose.  Diagnosing the node needs to see these values;
# changing them is rare enough to be worth a password.
for f in /etc/default/hummer-*; do
    [ -e "$f" ] || continue
    chmod 0644 "$f"
    echo "# readable: $f"
done

# --- 4. apply the sample-rate change ---------------------------------------
# DRIVE_INTERVAL_S is a gap *after* each cycle, not a sample period, so with a
# ~4.5 s cycle the shipped value of 5 produced a 9.5 s period.  1 keeps a
# courtesy pause and very nearly doubles the resolution.
if [ -f /etc/default/hummer-drive ]; then
    before="$(grep -E '^DRIVE_INTERVAL_S=' /etc/default/hummer-drive || echo 'unset')"
    sed -i 's/^DRIVE_INTERVAL_S=.*/DRIVE_INTERVAL_S=1/' /etc/default/hummer-drive
    after="$(grep -E '^DRIVE_INTERVAL_S=' /etc/default/hummer-drive || echo 'unset')"
    echo "# interval: $before -> $after"
fi

# --- 5. restart the recorder so it picks up the deployed code --------------
echo "# restarting hummer-drive ..."
systemctl restart hummer-drive
sleep 20

echo
echo "===== RESULT ====="
echo "active: $(systemctl is-active hummer-drive)"
journalctl -u hummer-drive --no-pager -n 12

echo
echo "===== PASSWORDLESS RESTART CHECK ====="
# Prove the rule works as the operator, not as root.
if sudo -u "$OPERATOR" sudo -n "$SYSTEMCTL" restart hummer-drive 2>/dev/null; then
    echo "OK: $OPERATOR can now restart hummer-drive without a password"
    sleep 10
    echo "active: $(systemctl is-active hummer-drive)"
else
    echo "WARNING: the passwordless restart did not take; a password is still needed"
fi
