#!/usr/bin/env bash
# Make the Home Assistant panel work from a phone away from the LAN.
#
# The panel is served by Home Assistant; the data comes from the Pi. Those two
# are on different networks -- the Pi rides a hotspot in the truck and is
# reachable only over Tailscale -- so the browser has to cross from one to the
# other, and away from the LAN that crossing fails four ways. Fixing any three
# of them still leaves a blank dashboard:
#
#   1. Mixed content. Nabu Casa serves the page over https; the panel was
#      stamped to fetch http://100.71.118.116:8765. A browser kills an http
#      fetch from an https page before it touches the network -- no request,
#      no error the page can see, just the empty state.
#   2. CORS. The node's --allow-origin list names only the http origins Home
#      Assistant uses on the LAN. Through Nabu Casa the origin is
#      https://<id>.ui.nabu.casa, which is not on it.
#   3. The Host guard. do_GET refuses any Host but the listener IP, which is
#      what stops DNS rebinding. `tailscale serve` forwards the ORIGINAL Host,
#      so requests arrive as hummer.tail335264.ts.net and every one is 403 --
#      with the certificate, the proxy and the CORS list all correct. This was
#      measured, not guessed: Host: 100.71.118.116:8765 -> 200,
#      Host: hummer.tail335264.ts.net -> 403.
#   4. Reachability. The address is on a tailnet, so the phone needs Tailscale.
#      That one is manual and is the price of this route; the alternative
#      (tailscale funnel) would publish GPS tracks and VIN to the open
#      internet unauthenticated, which is not a trade worth making.
#
# Needs sudo on the Pi. Restarts hummer-dashboard (the API) and never
# hummer-drive (the recorder), so a session in progress survives.
#
# Reverse with:  sudo tailscale serve reset   on the Pi.
set -euo pipefail

NODE_TS_IP=100.71.118.116
NODE_PORT=8765
NODE_HOST=hummer.tail335264.ts.net
OVERRIDE=/etc/systemd/system/hummer-dashboard.service.d/override.conf
HA=homeassistant
PANEL=/config/www/hummer/index.html
REPO=$(cd "$(dirname "$0")/.." && pwd)

say() { printf '\n== %s\n' "$*"; }

# The Nabu Casa hostname is read from Home Assistant rather than written down.
# This repository is public, and that URL is effectively a credential: HA
# serves /config/www/ through it with no authentication at all.
say "reading the Nabu Casa origin from Home Assistant"
NABU=$(ssh -o BatchMode=yes "$HA" 'python3 -c "
import json
d = json.load(open(\"/config/.storage/cloud\"))
print((d.get(\"data\") or {}).get(\"remote_domain\") or \"\")
"' | tr -d '\r\n')
if [ -z "$NABU" ]; then
    echo "error: Home Assistant reports no Nabu Casa remote domain." >&2
    echo "Enable Settings -> Home Assistant Cloud -> Remote Control first." >&2
    exit 1
fi
NABU_ORIGIN="https://${NABU}"
echo "   found (…${NABU: -18}) -- not printed in full on purpose"

# FIRST, before the unit file learns a flag the node has never heard of. The
# Pi imports from a plain rsync'd /home/jeremy/hummer-obd/src, so --allow-host
# does not exist there until this runs. Getting this order wrong leaves
# hummer-dashboard crash-looping on an unrecognised argument.
say "1/5  deploying the current source to the node"
HOST="jeremy@${NODE_TS_IP}" "${REPO}/scripts/deploy.sh" >/dev/null
ssh -o BatchMode=yes "jeremy@${NODE_TS_IP}" \
    "cd /home/jeremy/hummer-obd && PYTHONPATH=src python3 -m hummer_obd.dashboard --help" \
    | grep -q -- '--allow-host' \
    || { echo "error: the node still has no --allow-host after deploying" >&2; exit 1; }
echo "   node now understands --allow-host"

say "2/5  putting the node behind Tailscale https"
# --https is tailnet-only. Deliberately NOT --funnel.
ssh -t "jeremy@${NODE_TS_IP}" \
    "sudo tailscale serve --bg --https=443 http://${NODE_TS_IP}:${NODE_PORT}"

say "3/5  allowing the Nabu Casa origin and the proxy hostname"
ssh -t "jeremy@${NODE_TS_IP}" "
    set -e
    CHANGED=0
    sudo cp '${OVERRIDE}' '${OVERRIDE}.bak'
    if sudo grep -q -- '${NABU_ORIGIN}' '${OVERRIDE}'; then
        echo '   origin already present'
    else
        sudo sed -i 's|^\(ExecStart=/usr/bin/python3 -m hummer_obd.dashboard .*\)\$|\1 --allow-origin ${NABU_ORIGIN}|' '${OVERRIDE}'
        CHANGED=1
    fi
    if sudo grep -q -- '--allow-host ${NODE_HOST}' '${OVERRIDE}'; then
        echo '   host already present'
    else
        sudo sed -i 's|^\(ExecStart=/usr/bin/python3 -m hummer_obd.dashboard .*\)\$|\1 --allow-host ${NODE_HOST}|' '${OVERRIDE}'
        CHANGED=1
    fi
    [ \$CHANGED -eq 1 ] && echo '   previous unit kept as override.conf.bak'
    sudo systemctl daemon-reload
    sudo systemctl restart hummer-dashboard
    sleep 3
    echo -n '   hummer-dashboard: '; systemctl is-active hummer-dashboard
    echo -n '   hummer-drive (untouched): '; systemctl is-active hummer-drive
"

say "4/5  regenerating the panel against the https base"
# PYTHONPATH=src is required: the package is not installed and only pytest.ini
# puts src on the path.
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
( cd "$REPO" && PYTHONPATH=src python3 -m hummer_obd.hapanel \
      --api "https://${NODE_HOST}" --out "$TMP" )
[ -s "$TMP" ] || { echo "error: panel render produced nothing" >&2; exit 1; }
grep -q "<canvas" "$TMP" || { echo "error: rendered file is not the dashboard" >&2; exit 1; }
ssh -o BatchMode=yes "$HA" "cat > ${PANEL}" < "$TMP"
VERSION=$(sha256sum "$TMP" | cut -c1-12)

say "5/5  verifying"
echo -n "   node over https:            "
curl -s --max-time 15 -o /dev/null -w '%{http_code}\n' \
    "https://${NODE_HOST}/api/snapshot?session=latest"
echo -n "   with the proxy's own Host:  "
curl -s --max-time 15 -o /dev/null -w '%{http_code}\n' \
    -H "Host: ${NODE_HOST}" "http://${NODE_TS_IP}:${NODE_PORT}/api/snapshot?session=latest"
echo -n "   CORS for the Nabu origin:   "
curl -s --max-time 15 -D - -o /dev/null \
    -H "Origin: ${NABU_ORIGIN}" \
    "https://${NODE_HOST}/api/snapshot?session=latest" \
    | grep -i '^access-control-allow-origin' || echo "NO HEADER -- still blocked"
echo -n "   an unnamed Host is still refused (want 403): "
curl -s --max-time 15 -o /dev/null -w '%{http_code}\n' \
    -H "Host: attacker.example" "http://${NODE_TS_IP}:${NODE_PORT}/api/snapshot?session=latest"

cat <<DONE

Done on this side. Two things left that this script cannot do:

  * Install Tailscale on the phone and sign in. ${NODE_HOST} is a tailnet
    name; without it the phone still cannot route there. The Pi is on a
    hotspot in the truck, not the house LAN, so this is true on home WiFi too.

  * Point the Home Assistant card at the new build:

        /local/hummer/index.html?v=${VERSION}

    HA serves /local/ with max-age=2678400 (31 days), so without the new ?v=
    a browser that has opened the panel before keeps showing the old one and
    this whole exercise looks like it did nothing.
DONE
