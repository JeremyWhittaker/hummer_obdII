#!/usr/bin/env bash
# Install manually as root on the Pi. Only gpsd is restarted, never Bluetooth
# or either telemetry recorder. Existing GPS clients reconnect on their own.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
source "$ROOT/scripts/lib/require-node.sh"
require_node
if [ "$EUID" -ne 0 ]; then
    echo 'Run on the Pi: sudo bash /home/jeremy/hummer-obd/scripts/install-gps-hotplug.sh' >&2
    exit 1
fi
test -r /usr/lib/systemd/system/gpsdctl@.service
test -x /usr/sbin/gpsdctl
STAGING=$(mktemp -d)
trap 'rm -rf -- "$STAGING"' EXIT
/usr/bin/python3 -I "$ROOT/src/hummer_obd/gps_setup.py" --stage "$STAGING"
udevadm verify "$STAGING/79-hummer-gps.rules"
BACKUP="/var/backups/hummer-gps/$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 "$BACKUP"
cp -a /etc/default/gpsd "$BACKUP/gpsd.default"
if [ -e /etc/udev/rules.d/79-hummer-gps.rules ]; then
    cp -a /etc/udev/rules.d/79-hummer-gps.rules "$BACKUP/"
fi
install -o root -g root -m 0644 "$STAGING/gpsd.default" /etc/default/gpsd
install -o root -g root -m 0644 "$STAGING/79-hummer-gps.rules" \
    /etc/udev/rules.d/79-hummer-gps.rules
udevadm control --reload-rules
# Target only this receiver. No global USB/tty trigger and no serial opens.
udevadm trigger --action=change "$(cat "$STAGING/sysfs")"
udevadm settle --timeout=10
test -c /dev/hummer_gps
if [ "$(readlink -f /dev/hummer_gps)" != "$(cat "$STAGING/node")" ]; then
    echo 'Refusing: GPS alias does not resolve to the inspected receiver' >&2
    exit 1
fi
systemctl daemon-reload
systemd-analyze verify gpsdctl@hummer_gps.service
systemctl enable gpsd.socket gpsd.service
systemctl restart gpsd.service
# An already-present device need not activate new SYSTEMD_WANTS dependencies.
# Register it explicitly this time; future insertions activate this unit.
systemctl restart gpsdctl@hummer_gps.service
systemctl is-active --quiet gpsd.service
/usr/bin/python3 -I "$ROOT/src/hummer_obd/gps_setup.py" --check
printf '\nGPS now follows /dev/hummer_gps across ttyUSB renumbering.\n'
printf 'Debian gpsdctl handles removal and reinsertion. Backup: %s\n' "$BACKUP"
printf 'No satellite fix is normal indoors; device registration is checked above.\n'
