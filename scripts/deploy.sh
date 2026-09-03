#!/bin/bash
# Deploy this repository to the Pi.
#
# Copies source, config, docs, scripts and systemd units to
# /home/jeremy/hummer-obd.  Never copies secrets, runtime data or raw logs;
# never restarts the collector (that stays a deliberate manual act).
set -euo pipefail

# hummer.local only resolves on the home LAN.  The node also lives on
# Tailscale, and a deploy that silently reaches nothing is worse than one that
# fails: a whole module was "deployed" to a host that could not be resolved,
# and the failure only surfaced as ModuleNotFoundError on the node.  Resolve
# the mDNS name first and fall back to the Tailscale address.
DEFAULT_HOST="jeremy@hummer.local"
if ! getent hosts hummer.local >/dev/null 2>&1; then
    DEFAULT_HOST="jeremy@${HUMMER_TAILSCALE_IP:-100.71.118.116}"
fi
HOST="${HOST:-$DEFAULT_HOST}"
DEST="${DEST:-/home/jeremy/hummer-obd}"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RSYNC_EXCLUDES=(--exclude '__pycache__' --exclude '*.pyc' --exclude '*.egg-info')

echo "# deploying $SRC_DIR -> $HOST:$DEST"
ssh "$HOST" "mkdir -p $DEST/{src,config,docs,scripts,systemd,logs/raw,data}"

rsync -av --delete "${RSYNC_EXCLUDES[@]}" \
    "$SRC_DIR/src/" "$HOST:$DEST/src/"
rsync -av "${RSYNC_EXCLUDES[@]}" "$SRC_DIR/scripts/" "$HOST:$DEST/scripts/"
rsync -av "${RSYNC_EXCLUDES[@]}" "$SRC_DIR/systemd/" "$HOST:$DEST/systemd/"
rsync -av "${RSYNC_EXCLUDES[@]}" "$SRC_DIR/docs/" "$HOST:$DEST/docs/"
rsync -av "${RSYNC_EXCLUDES[@]}" "$SRC_DIR/tests/" "$HOST:$DEST/tests/"
rsync -av "$SRC_DIR/pytest.ini" "$HOST:$DEST/"
rsync -av "$SRC_DIR/pyproject.toml" "$HOST:$DEST/"
rsync -av "$SRC_DIR/README.md" "$HOST:$DEST/"
# Ship every config *template*, never config/hummer.toml itself: the live
# configuration belongs to the node.  A new template that is not listed here
# simply never arrives, which is how the trial unit's defaults file went
# missing after it was added.
rsync -av "$SRC_DIR"/config/*.example.toml "$SRC_DIR"/config/*.default "$HOST:$DEST/config/"
ssh "$HOST" "test -f $DEST/config/hummer.toml || cp $DEST/config/hummer.example.toml $DEST/config/hummer.toml"

echo "# deployed; installed units are NOT enabled by this script"
