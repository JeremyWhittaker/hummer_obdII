#!/bin/bash
# Install the official Waveshare e-Paper Python driver for the 2.13" V4 panel.
#
# Waveshare ships the driver inside a large repository rather than on PyPI, and
# a full clone is painfully slow on a Pi Zero 2 W over Wi-Fi.  This fetches the
# three files the V4 panel needs, pinned to a specific upstream commit, and
# records that commit plus each file's SHA-256 in PROVENANCE.txt.
set -euo pipefail

DEST="${1:-/home/jeremy/hummer-obd/vendor/waveshare}"
REPO="waveshareteam/e-Paper"
REF="${WAVESHARE_REF:-}"
LIBPATH="RaspberryPi_JetsonNano/python/lib/waveshare_epd"
FILES=(__init__.py epdconfig.py epd2in13_V4.py)

if [[ -z "$REF" ]]; then
    echo "# resolving $REPO HEAD"
    REF="$(curl -fsSL "https://api.github.com/repos/$REPO/commits/master" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])')"
fi
echo "# upstream commit $REF"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
for file in "${FILES[@]}"; do
    url="https://raw.githubusercontent.com/$REPO/$REF/$LIBPATH/$file"
    curl -fsSL "$url" -o "$WORK/$file"
    echo "  fetched $file ($(stat -c%s "$WORK/$file") bytes)"
done

# Refuse to install something that is not the driver we asked for.
grep -q "class EPD" "$WORK/epd2in13_V4.py" || { echo "ERROR: epd2in13_V4.py has no EPD class" >&2; exit 1; }
grep -qE "EPD_WIDTH *= *122" "$WORK/epd2in13_V4.py" || { echo "ERROR: unexpected panel width" >&2; exit 1; }
grep -qE "EPD_HEIGHT *= *250" "$WORK/epd2in13_V4.py" || { echo "ERROR: unexpected panel height" >&2; exit 1; }

mkdir -p "$DEST/waveshare_epd"
cp "${FILES[@]/#/$WORK/}" "$DEST/waveshare_epd/"
{
    printf 'source: https://github.com/%s\ncommit: %s\npath:   %s\ninstalled: %s\n\nsha256:\n' \
        "$REPO" "$REF" "$LIBPATH" "$(date -Is)"
    (cd "$DEST/waveshare_epd" && sha256sum "${FILES[@]}")
} > "$DEST/PROVENANCE.txt"

echo "# installed to $DEST"
PYTHONPATH="$DEST" python3 -c "from waveshare_epd import epd2in13_V4; print('epd2in13_V4 import OK; panel', epd2in13_V4.EPD_WIDTH, 'x', epd2in13_V4.EPD_HEIGHT)"
cat "$DEST/PROVENANCE.txt"
