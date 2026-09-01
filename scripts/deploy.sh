#!/bin/bash
# Deploy this repository to the Pi.
#
# Copies source, config, docs, scripts and systemd units to
# /home/jeremy/hummer-obd.  Never copies secrets, runtime data or raw logs;
# never restarts the collector (that stays a deliberate manual act).
set -euo pipefail

HOST="${HOST:-jeremy@hummer.local}"
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
rsync -av "$SRC_DIR/config/hummer.example.toml" "$HOST:$DEST/config/"
ssh "$HOST" "test -f $DEST/config/hummer.toml || cp $DEST/config/hummer.example.toml $DEST/config/hummer.toml"

echo "# deployed; installed units are NOT enabled by this script"
