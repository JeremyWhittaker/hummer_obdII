#!/bin/bash
# Let the dashboard service write the place list, and nothing else.
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
set -euo pipefail

DIR=/etc/systemd/system/hummer-dashboard.service.d
CONF="$DIR/places.conf"

echo "# writing $CONF"
sudo mkdir -p "$DIR"
sudo tee "$CONF" >/dev/null <<'UNIT'
# Added so /api/places can save the place list. The service still cannot write
# anywhere else under $HOME: ProtectHome=read-only and ProtectSystem=strict
# remain, and this names one directory holding one small JSON file of place
# names. Remove this file and reload to put it back to read-only.
[Service]
ReadWritePaths=/home/jeremy/hummer-obd/config
UNIT

sudo systemctl daemon-reload
sudo systemctl restart hummer-dashboard
sleep 2
systemctl is-active hummer-dashboard
echo "# writable paths now:"
systemctl show hummer-dashboard -p ReadWritePaths
