#!/bin/bash
# Idempotent Pi-side setup for the hummer OBD node.
#
# Creates the runtime layout, installs the Waveshare driver, and installs the
# systemd units.  It enables nothing, never pairs Bluetooth devices, and never
# touches the vehicle.  Safe to re-run.
set -euo pipefail

DEST="${DEST:-/home/jeremy/hummer-obd}"
cd "$DEST"

echo "### layout"
mkdir -p "$DEST"/{logs/raw,data,evidence,vendor}
chmod 700 "$DEST/logs" "$DEST/data"
[ -f config/hummer.toml ] || cp config/hummer.example.toml config/hummer.toml
echo "  ok: $DEST"

echo "### python dependencies"
for module in serial spidev RPi.GPIO gpiozero PIL numpy; do
    python3 -c "import $module" 2>/dev/null && echo "  $module OK" || echo "  $module MISSING"
done

echo "### unit tests"
PYTHONPATH=src python3 -m unittest discover -s tests -t tests -q 2>&1 | grep -E '^(OK|FAILED|Ran )' | tail -2

echo "### waveshare driver"
if PYTHONPATH="$DEST/vendor/waveshare" python3 -c "from waveshare_epd import epd2in13_V4" 2>/dev/null; then
    echo "  already installed"
else
    ./scripts/install_waveshare_driver.sh "$DEST/vendor/waveshare"
fi

echo "### systemd units"
sudo cp \
    systemd/hummer-display.service \
    systemd/hummer-collector.service \
    systemd/hummer-rfcomm.service \
    systemd/hummer-btdiscover.service \
    /etc/systemd/system/
sudo systemctl daemon-reload
echo "  installed (no unit was enabled by this script)"
for unit in hummer-display hummer-collector hummer-rfcomm hummer-btdiscover; do
    printf '  %-20s %s\n' "$unit" "$(systemctl is-enabled $unit 2>&1)"
done

echo
echo "Next, deliberately and in order:"
echo "  1. render the panel once:  PYTHONPATH=src:vendor/waveshare python3 -m hummer_obd.display.status --once"
echo "  2. if the panel refreshed: sudo systemctl enable --now hummer-display.service"
echo "  3. pair the adapter:       sudo scripts/pair_obdlink.sh scan"
echo "  4. probe (read-only):      PYTHONPATH=src python3 -m hummer_obd.probe --device /dev/rfcomm0 --root ."
echo "  5. review the raw log before considering the collector."
