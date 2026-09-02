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

echo "### install the package"
# An *editable* install, so src/ stays the single source of truth and a deploy
# does not need a reinstall.  This is what puts the seven hummer-obd-* console
# scripts on PATH; without it every documented command fails with "command not
# found", which is exactly what happened before this step existed.
#
# --break-system-packages is deliberate.  Debian 13 marks its Python
# externally-managed (PEP 668), and the alternatives are worse here: ~/.local/bin
# is not on PATH on this image, and a venv would mean repointing all six systemd
# units away from /usr/bin/python3 on a live appliance.  This node has exactly
# one Python application and its two dependencies (Pillow, pyserial) are already
# system packages, so the isolation a venv buys is not worth the churn.
sudo python3 -m pip install -e . --break-system-packages --root-user-action=ignore \
    >/dev/null 2>&1 && echo "  installed (editable)" || echo "  install FAILED"
command -v hummer-obd-capabilities >/dev/null \
    && echo "  console scripts on PATH" || echo "  console scripts MISSING"

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
    systemd/hummer-collector-trial.service \
    systemd/hummer-rfcomm.service \
    systemd/hummer-btdiscover.service \
    /etc/systemd/system/
# Trial settings are operator-editable and must not be clobbered on re-run.
[ -f /etc/default/hummer-collector-trial ] || \
    sudo cp config/hummer-collector-trial.default /etc/default/hummer-collector-trial
sudo systemctl daemon-reload
echo "  installed (no unit was enabled by this script)"
for unit in hummer-display hummer-collector hummer-collector-trial hummer-rfcomm hummer-btdiscover; do
    printf '  %-20s %s\n' "$unit" "$(systemctl is-enabled $unit 2>&1)"
done

echo
echo "Next, deliberately and in order:"
echo "  1. render the panel once:  PYTHONPATH=src:vendor/waveshare python3 -m hummer_obd.display.status --once"
echo "  2. if the panel refreshed: sudo systemctl enable --now hummer-display.service"
echo "  3. pair the adapter:       sudo scripts/pair_obdlink.sh scan"
echo "  4. probe (read-only):      PYTHONPATH=src python3 -m hummer_obd.probe --device /dev/rfcomm0 --root ."
echo "  5. review the raw log before considering the collector."
