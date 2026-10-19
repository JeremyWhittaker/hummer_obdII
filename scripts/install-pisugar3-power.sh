#!/usr/bin/env bash
# Run manually as root ON THE PI. Does not shut down or restart the recorder.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
source "$ROOT/scripts/lib/require-node.sh"
require_node
if [ "$EUID" -ne 0 ]; then
    echo 'Run on the Pi: sudo bash /home/jeremy/hummer-obd/scripts/install-pisugar3-power.sh' >&2
    exit 1
fi
# Late hooks need Python and its libraries on the surviving root mount.
for path in /usr /usr/local; do
    if [ "$(findmnt -n -o TARGET -T "$path")" != / ]; then
        echo "Refusing: $path must reside on the root filesystem for the shutdown hook" >&2
        exit 1
    fi
done
for unit in pisugar-server.service pisugar-poweroff.service; do
    if systemctl is-enabled --quiet "$unit" 2>/dev/null || systemctl is-active --quiet "$unit"; then
        echo "Refusing competing power manager: $unit; review its configuration first" >&2
        exit 1
    fi
done
/usr/bin/python3 -I "$ROOT/src/hummer_obd/pisugar3_power.py" check
systemctl cat hummer-battery.service >/dev/null
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP="/var/backups/hummer-power/$STAMP"
install -d -m 0700 "$BACKUP"
for path in /etc/systemd/system/hummer-battery.service.d/20-pisugar3.conf \
            /usr/local/lib/hummer-power/pisugar3_power.py \
            /usr/lib/systemd/system-shutdown/hummer-pisugar3; do
    if [ -e "$path" ] || [ -L "$path" ]; then
        cp -a --parents "$path" "$BACKUP/"
    fi
done
install -d -m 0755 /usr/local/lib/hummer-power \
    /usr/lib/systemd/system-shutdown /etc/systemd/system/hummer-battery.service.d
install -o root -g root -m 0755 "$ROOT/src/hummer_obd/pisugar3_power.py" \
    /usr/local/lib/hummer-power/pisugar3_power.py
# A full standalone copy: no symlink into a user's home or PYTHONPATH imports.
install -o root -g root -m 0755 "$ROOT/src/hummer_obd/pisugar3_power.py" \
    /usr/lib/systemd/system-shutdown/hummer-pisugar3
install -o root -g root -m 0644 "$ROOT/systemd/hummer-battery-pisugar3.conf" \
    /etc/systemd/system/hummer-battery.service.d/20-pisugar3.conf
systemctl daemon-reload
systemd-analyze verify hummer-battery.service
systemctl enable hummer-battery.service
systemctl restart hummer-battery.service
systemctl is-active --quiet hummer-battery.service
/usr/bin/python3 -I /usr/local/lib/hummer-power/pisugar3_power.py check
systemctl --no-pager --full status hummer-battery.service
printf '\nInstalled: 10%% reserve, 3 readings 30s apart, 3.40 V emergency floor.\n'
printf 'Only hummer-battery was restarted. Prior files backed up under %s\n' "$BACKUP"
printf 'Physical power-loss / shutdown / restored-input test still required.\n'
