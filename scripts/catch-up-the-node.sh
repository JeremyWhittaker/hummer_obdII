#!/usr/bin/env bash
# Everything on the vehicle node that needs a password, in one run.
#
# The repository has been ahead of the node for a while. Code was committed and
# tested and never deployed, so fixes that exist in git have had no effect on
# the truck. This closes that gap and does the two permission changes that have
# each been blocking a feature.
#
# Run it from the workstation (NOT on the Pi -- it reaches the Pi over ssh):
#
#     bash scripts/catch-up-the-node.sh
#
# It will ask for the Pi's sudo password, possibly more than once.
#
# WHAT IT CHANGES, and what each one fixes:
#
#   1. Deploys the current source. The node is running a gps.py without the
#      anchor fix, which is why the position still flickers while parked and
#      why it freezes for up to 100 m while driving.
#   2. Restarts hummer-drive so the new gps.py is actually loaded. Python
#      imports once at start; deploying alone changes nothing until this.
#      *** THIS ENDS THE SESSION IN PROGRESS. *** Rows already written are kept;
#      a new session file starts. If a charge is running, it is not interrupted
#      -- only the recording of it is split in two.
#   3. Adds the config directory to ReadWritePaths so naming a stop in the
#      Places tab can actually save. Today it returns "place list is not
#      writable" and the name is lost.
#   4. Restarts hummer-dashboard to pick both up.
#
# It does NOT touch Bluetooth, Wi-Fi, sudoers, or anything outside this project.
set -euo pipefail

NODE="${NODE:-jeremy@100.71.118.116}"
REPO=$(cd "$(dirname "$0")/.." && pwd)
DROPIN=/etc/systemd/system/hummer-dashboard.service.d

say() { printf '\n== %s\n' "$*"; }

say "0/5  checking the node is reachable and is the node"
ssh -o BatchMode=yes -o ConnectTimeout=8 "$NODE" \
    'systemctl cat hummer-drive >/dev/null 2>&1' \
  || { echo "error: $NODE has no hummer-drive service. Wrong host?" >&2; exit 1; }
echo "   ok"

say "1/5  what is about to change on the node"
rsync -a --dry-run --itemize-changes --delete \
    --exclude '__pycache__' --exclude '*.pyc' --exclude '*.egg-info' \
    "$REPO/src/" "$NODE:/home/jeremy/hummer-obd/src/" | sed 's/^/   /'

say "2/5  deploying"
HOST="$NODE" "$REPO/scripts/deploy.sh" >/dev/null
ssh -o BatchMode=yes "$NODE" \
    "grep -c odo_says_still /home/jeremy/hummer-obd/src/hummer_obd/gps.py" >/dev/null \
  || { echo "error: the anchor fix is not on the node after deploying" >&2; exit 1; }
echo "   gps.py now carries the anchor fix"

say "3/5  letting the dashboard save a place name"
ssh -t "$NODE" "
    set -e
    if sudo test -f '${DROPIN}/places.conf'; then
        echo '   already present'
    else
        sudo mkdir -p '${DROPIN}'
        sudo tee '${DROPIN}/places.conf' >/dev/null <<'UNIT'
# Added so /api/places can save the place list. The service still cannot write
# anywhere else under \$HOME: ProtectHome=read-only and ProtectSystem=strict
# remain, and this names one directory holding one small JSON file of place
# names. Remove this file and reload to put it back to read-only.
[Service]
ReadWritePaths=/home/jeremy/hummer-obd/config
UNIT
        echo '   drop-in written'
    fi
    sudo mkdir -p /home/jeremy/hummer-obd/config
    sudo chown jeremy:jeremy /home/jeremy/hummer-obd/config
    sudo systemctl daemon-reload
"

say "4/5  restarting both services (this starts a new session file)"
ssh -t "$NODE" "
    set -e
    sudo systemctl restart hummer-dashboard
    sudo systemctl restart hummer-drive
    sleep 4
    echo -n '   hummer-dashboard : '; systemctl is-active hummer-dashboard
    echo -n '   hummer-drive     : '; systemctl is-active hummer-drive
    echo '   writable paths   :'
    systemctl show hummer-dashboard -p ReadWritePaths | sed 's/^/     /'
"

say "5/5  verifying from here"
echo -n "   api answers            : "
curl -s --max-time 15 -o /dev/null -w '%{http_code}\n' \
    "http://100.71.118.116:8765/api/snapshot?session=latest"
echo -n "   places endpoint        : "
curl -s --max-time 15 "http://100.71.118.116:8765/api/places" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('%d places defined' % len(d.get('places',[])))" \
  2>/dev/null || echo "unreachable"
echo -n "   recording again        : "
sleep 12
curl -s --max-time 15 "http://100.71.118.116:8765/api/snapshot?session=latest" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);s=d.get('session') or {};print('%s rows, age %ss, %s' % (s.get('rows'), round(s.get('age_s') or 0), s.get('status')))" \
  2>/dev/null || echo "no answer"

cat <<'DONE'

Done. What this did NOT fix, so you know what to still expect:

  * Sessions still split on a 2-hour timer, not on whether the truck moved.
    That is why a replay is half driveway. It needs code, not a password.

  * Charge power still shows blank in the charging card even though volts and
    amps are both present.

  * The mobile panel still needs Tailscale on the phone -- separate script,
    scripts/enable-remote-dashboard.sh.
DONE
