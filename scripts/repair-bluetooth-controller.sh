#!/usr/bin/env bash
# Owner-run recovery for confirmed HCI timeouts; never reboots or touches Wi-Fi.
# The watchdog candidate is staged outside its active import path so an agent
# can prepare it without indirectly invoking the root timer's recovery actions.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
source "$ROOT/scripts/lib/require-node.sh"
require_node
CANDIDATE="$ROOT/.foreman/btwatch-next.py"
if [ ! -f "$CANDIDATE" ] || [ -L "$CANDIDATE" ]; then
    echo "Missing regular staged watchdog candidate: $CANDIDATE" >&2
    exit 1
fi
/usr/bin/python3 -I -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$CANDIDATE"
for unit in bluetooth.service hummer-rfcomm.service hummer-drive.service \
            unidenr8-collector.service hummer-btwatch.service hummer-btwatch.timer; do
    if [ "$(systemctl show "$unit" -p LoadState --value)" != loaded ]; then
        echo "Required unit is not loaded: $unit" >&2
        exit 1
    fi
done
if [ "${1:-}" = --check ]; then
    echo 'Staged watchdog parses and all recovery units are loaded; no changes made.'
    exit 0
fi
if [ "$EUID" -ne 0 ]; then
    echo 'Run on Pi: sudo bash /home/jeremy/hummer-obd/scripts/repair-bluetooth-controller.sh' >&2
    exit 1
fi

wanted() {
    case "$(systemctl is-active "$1" 2>/dev/null || :)" in
        active|activating|reloading) return 0 ;;
        *) return 1 ;;
    esac
}
RECORDER_WANTED=0; RADAR_WANTED=0; TIMER_WANTED=0
wanted hummer-drive.service && RECORDER_WANTED=1
wanted unidenr8-collector.service && RADAR_WANTED=1
wanted hummer-btwatch.timer && TIMER_WANTED=1

restore_wanted() {
    systemctl start bluetooth.service hummer-rfcomm.service || return 1
    if [ "$RECORDER_WANTED" = 1 ]; then
        systemctl start hummer-drive.service || return 1
    fi
    # Establish classic RFCOMM before restarting the BLE subscriber.
    sleep 5
    if [ "$RADAR_WANTED" = 1 ]; then
        systemctl start unidenr8-collector.service || return 1
    fi
    if [ "$TIMER_WANTED" = 1 ]; then
        systemctl start hummer-btwatch.timer || return 1
    fi
}
on_exit() {
    RECOVERY_RESULT=$?
    trap - EXIT
    if ! restore_wanted; then
        echo 'Recovery did not restore all previously running services.' >&2
        RECOVERY_RESULT=1
    fi
    exit "$RECOVERY_RESULT"
}

BACKUP="/var/backups/hummer-bluetooth/$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 "$BACKUP"
cp -a "$ROOT/src/hummer_obd/btwatch.py" "$BACKUP/btwatch.py"
trap on_exit EXIT
systemctl stop hummer-btwatch.timer hummer-btwatch.service
systemctl stop hummer-drive.service unidenr8-collector.service \
    hummer-rfcomm.service bluetooth.service
install -o jeremy -g jeremy -m 0644 "$CANDIDATE" "$ROOT/src/hummer_obd/btwatch.py"
modprobe -r hci_uart
modprobe hci_uart
systemctl start bluetooth.service
timeout 20 hciconfig hci0 up
echo "Bluetooth driver reloaded; prior watchdog source backed up under $BACKUP."
echo 'Restoring previously running services. Pi, Wi-Fi and reserve monitor stay online.'
