#!/usr/bin/env bash
# Make the Home Assistant panel work from a phone on cellular.
#
# The panel is served by Home Assistant but fetches its data from the Pi. Away
# from the LAN that fetch fails three times over, and fixing any two of the
# three still leaves a blank dashboard:
#
#   1. Mixed content. Nabu Casa serves the page over https; the panel was
#      stamped to fetch http://100.71.118.116:8765. A browser kills an http
#      fetch from an https page before it touches the network -- no request,
#      no error the page can see.
#   2. Scheme. Fixed here by putting the node behind Tailscale's own https
#      with a real certificate.
#   3. CORS. The node's --allow-origin list names only the two http origins
#      Home Assistant uses on the LAN. Arriving through Nabu Casa the origin
#      is https://<something>.ui.nabu.casa, which is not on the list.
#
# Needs sudo on the Pi, which is why this is a script you run rather than
# something the agent did. It restarts hummer-dashboard (the API) and never
# touches hummer-drive (the recorder), so a session in progress is safe.
#
# Reverse with:  sudo tailscale serve reset   on the Pi.
set -euo pipefail

NODE_TS_IP=100.71.118.116
NODE_PORT=8765
NODE_HOST=hummer.tail335264.ts.net
OVERRIDE=/etc/systemd/system/hummer-dashboard.service.d/override.conf
HA=homeassistant
PANEL=/config/www/hummer/index.html

say() { printf '\n== %s\n' "$*"; }

# The Nabu Casa hostname is read from Home Assistant rather than written down.
# This repository is public, and that URL is effectively a credential: HA serves
# /config/www/ through it with no authentication at all.
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

say "1/4  putting the node behind Tailscale https"
# --https is tailnet-only. Deliberately NOT --funnel, which would publish GPS
# tracks and VIN to the public internet with nothing in front of them.
ssh -t "jeremy@${NODE_TS_IP}" \
    "sudo tailscale serve --bg --https=443 http://${NODE_TS_IP}:${NODE_PORT}"

say "2/4  allowing the Nabu Casa origin on the node"
ssh -t "jeremy@${NODE_TS_IP}" "
    set -e
    if sudo grep -q -- '${NABU_ORIGIN}' '${OVERRIDE}'; then
        echo '   already present, leaving it alone'
    else
        sudo cp '${OVERRIDE}' '${OVERRIDE}.bak'
        sudo sed -i 's|^\(ExecStart=/usr/bin/python3 -m hummer_obd.dashboard .*\)\$|\1 --allow-origin ${NABU_ORIGIN}|' '${OVERRIDE}'
        echo '   added; previous file kept as override.conf.bak'
    fi
    sudo systemctl daemon-reload
    sudo systemctl restart hummer-dashboard
    sleep 2
    systemctl is-active hummer-dashboard
    echo '   recorder untouched:' \$(systemctl is-active hummer-drive)
"

say "3/4  regenerating the panel against the https base"
# PYTHONPATH=src is required: the package is not installed, and only pytest.ini
# puts src on the path. Without it this dies with ModuleNotFoundError after the
# node has already been reconfigured, which is the worst point to fail at.
REPO=$(cd "$(dirname "$0")/.." && pwd)
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
( cd "$REPO" && PYTHONPATH=src python3 -m hummer_obd.hapanel \
      --api "https://${NODE_HOST}" --out "$TMP" )
[ -s "$TMP" ] || { echo "error: panel render produced nothing" >&2; exit 1; }
grep -q "<canvas" "$TMP" || { echo "error: rendered file is not the dashboard" >&2; exit 1; }
ssh -o BatchMode=yes "$HA" "cat > ${PANEL}" < "$TMP"
# hapanel derives the version the same way; recomputed here so the value shown
# is the one actually deployed rather than the one it intended to deploy.
VERSION=$(sha256sum "$TMP" | cut -c1-12)

say "4/4  verifying"
echo -n "   node over https: "
curl -s --max-time 10 -o /dev/null -w '%{http_code}\n' \
    "https://${NODE_HOST}/api/snapshot?session=latest"
echo -n "   CORS from Nabu Casa origin: "
curl -s --max-time 10 -o /dev/null -D - \
    -H "Origin: ${NABU_ORIGIN}" \
    "https://${NODE_HOST}/api/snapshot?session=latest" \
    | grep -i '^access-control-allow-origin' || echo "NO HEADER -- still blocked"

cat <<DONE

Done. Two things left that this script cannot do:

  * Install Tailscale on the phone and sign in. ${NODE_HOST} is a tailnet
    name; without Tailscale the phone still cannot resolve it. This is the
    deliberate trade -- the alternative (tailscale funnel) would put the
    telemetry on the public internet unauthenticated.

  * Point the Home Assistant card at the new build:

        /local/hummer/index.html?v=${VERSION}

    HA serves /local/ with max-age=2678400 (31 days), so without the new
    ?v= a browser that has opened the panel before keeps showing the old one
    and this whole exercise looks like it did nothing.
DONE
