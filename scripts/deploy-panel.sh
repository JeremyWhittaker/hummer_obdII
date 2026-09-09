#!/usr/bin/env bash
# Build the Home Assistant panel, install it, and repoint the dashboard card.
#
# The third step is the one that matters. Home Assistant serves /local/ with
# max-age=2678400 -- thirty-one days -- so a redeployed panel is invisible to
# any browser that has seen it before. The build stamps a content hash into
# the card's URL to beat that, which works only if the CARD is updated too.
#
# It was not, for three builds. Jeremy sat on /hummer-ev/telemetry watching a
# version from an hour earlier while every deploy reported success, because
# the file on disk was current and the card pointing at it was not. Deploying
# and repointing are one operation and are done as one here.
set -euo pipefail

API="${1:-http://100.71.118.116:8765}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$REPO/dist"
HA_SSH="${HA_SSH:-homeassistant}"
DASHBOARD="${DASHBOARD:-hummer-ev}"

mkdir -p "$DIST"
echo "building against $API"
PYTHONPATH="$REPO/src" python3 -m hummer_obd.hapanel --api "$API" --out "$DIST/index.html" 2>&1 \
  | sed 's/^/  /'
VERSION=$(python3 - "$DIST/index.html" <<'PY'
import hashlib, pathlib, sys
print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest()[:12])
PY
)
echo "  version $VERSION"

echo "installing to $HA_SSH:/config/www/hummer/index.html"
ssh "$HA_SSH" 'cat > /config/www/hummer/index.html' < "$DIST/index.html"
REMOTE_SUM=$(ssh "$HA_SSH" 'md5sum /config/www/hummer/index.html | cut -d" " -f1')
LOCAL_SUM=$(md5sum "$DIST/index.html" | cut -d' ' -f1)
if [ "$REMOTE_SUM" != "$LOCAL_SUM" ]; then
  echo "  FAILED: what landed does not match what was built" >&2
  exit 1
fi
echo "  installed, checksum matches"

if [ ! -r "$HOME/.ha_token" ]; then
  echo "  no ~/.ha_token, so the card was NOT repointed -- it will keep"
  echo "  serving whatever version it currently names."
  echo "  Open this instead: /local/hummer/index.html?v=$VERSION"
  exit 0
fi

echo "repointing the $DASHBOARD card"
node "$REPO/scripts/ha-card.js" "$DASHBOARD" "/local/hummer/index.html?v=$VERSION" \
  | sed 's/^/  /'
echo
echo "done. The sidebar entry now serves this build:"
echo "  http://172.16.106.12:8123/$DASHBOARD/telemetry"
