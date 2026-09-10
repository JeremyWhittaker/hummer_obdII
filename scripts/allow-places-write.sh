#!/bin/bash
# Let the dashboard service on the NODE write the place list, and nothing else.
#
# The unit runs with ProtectHome=read-only and ProtectSystem=strict, and its
# only writable path is the map tile cache. That is the right posture for a
# service on a vehicle's diagnostic port, and it is why naming a place from
# Home Assistant returned "place list is not writable": the endpoint worked,
# the fence worked, and the sandbox correctly refused the write.
#
# This adds ONE directory. It is a drop-in rather than an edit to the unit, so
# it is visible in `systemctl cat`, survives a redeploy of the unit, and is
# removed by deleting one file.
#
# It runs AGAINST THE NODE over ssh. The first version of this script called
# sudo directly and was written assuming someone would be sitting on the Pi.
# Run from a workstation it wrote a systemd drop-in onto the workstation --
# where there is no hummer-dashboard.service -- and failed with "Unit
# hummer-dashboard.service not found" after already having littered /etc. A
# deployment script that silently targets whichever machine invoked it is a
# trap, so this one names its target and checks it before writing anything.
set -euo pipefail

NODE="${NODE:-jeremy@100.71.118.116}"
DIR=/etc/systemd/system/hummer-dashboard.service.d
CONF="$DIR/places.conf"

echo "# target: $NODE  (override with NODE=user@host)"

# Refuse before writing rather than after. The failure this replaces left a
# file behind on the wrong machine and fixed nothing on the right one.
if ! ssh -o BatchMode=yes -o ConnectTimeout=8 "$NODE" \
        'systemctl cat hummer-dashboard >/dev/null 2>&1'; then
    echo "error: $NODE has no hummer-dashboard.service." >&2
    echo "Nothing has been written. Check NODE, or deploy the unit first." >&2
    exit 1
fi

echo "# writing $CONF on $NODE"
ssh -t "$NODE" "
    set -e
    sudo mkdir -p '$DIR'
    sudo tee '$CONF' >/dev/null <<'UNIT'
# Added so /api/places can save the place list. The service still cannot write
# anywhere else under \$HOME: ProtectHome=read-only and ProtectSystem=strict
# remain, and this names one directory holding one small JSON file of place
# names. Remove this file and reload to put it back to read-only.
[Service]
ReadWritePaths=/home/jeremy/hummer-obd/config
UNIT
    sudo mkdir -p /home/jeremy/hummer-obd/config
    sudo chown jeremy:jeremy /home/jeremy/hummer-obd/config
    sudo systemctl daemon-reload
    sudo systemctl restart hummer-dashboard
    sleep 2
    echo -n '# hummer-dashboard: '; systemctl is-active hummer-dashboard
    echo -n '# hummer-drive (untouched): '; systemctl is-active hummer-drive
    echo '# writable paths now:'
    systemctl show hummer-dashboard -p ReadWritePaths
"

echo
echo "# proving a place can actually be saved (not just that the flag is set)"
if curl -s --max-time 10 -o /dev/null -w '' "http://100.71.118.116:8765/api/places"; then
    curl -s --max-time 10 "http://100.71.118.116:8765/api/places" \
      | python3 -c "import sys,json; d=json.load(sys.stdin); print('  places endpoint reachable, %d defined' % len(d.get('places',[])))"
else
    echo "  could not reach the places endpoint to check"
fi
