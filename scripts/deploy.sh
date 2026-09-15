#!/bin/bash
# Deploy this repository to the Pi.
#
# Copies source, config, docs, scripts and systemd units to
# /home/jeremy/hummer-obd.  Never copies secrets, runtime data or raw logs;
# never restarts the collector (that stays a deliberate manual act).
set -euo pipefail

# The scanner is independent of the dashboard. This narrow mode preserves
# node-local UI/config changes and cannot delete files or restart services.
SCAN_ONLY=false
SCAN_DRY_RUN=false
for deploy_arg in "$@"; do
    case "$deploy_arg" in
        --scan-only) SCAN_ONLY=true ;;
        --dry-run) SCAN_DRY_RUN=true ;;
        *) echo "usage: scripts/deploy.sh [--scan-only [--dry-run]]" >&2; exit 2 ;;
    esac
done
if $SCAN_DRY_RUN && ! $SCAN_ONLY; then
    echo "--dry-run requires --scan-only" >&2
    exit 2
fi

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

if $SCAN_ONLY; then
    # This is an update to an existing node, not a partial bootstrap. Reuse
    # its already-deployed transport/decoder/addressing and run a dry-run smoke
    # check after deployment before any operator-authorized vehicle request.
    ssh -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=10 -o ServerAliveCountMax=2 \
        "$HOST" "test -f $DEST/src/hummer_obd/drive.py && test -f $DEST/src/hummer_obd/enhanced.py && test -f $DEST/src/hummer_obd/transport.py && test -f $DEST/src/hummer_obd/rawlog.py"
    SCAN_FILES=(
        src/hummer_obd/scan.py src/hummer_obd/safety.py src/hummer_obd/access.py
        pyproject.toml pytest.ini README.md
        docs/DEEP_SCAN.md docs/SAFETY.md docs/ACCESS_MATRIX.md
        tests/test_scan.py tests/test_safety.py tests/test_access.py tests/elm_simulator.py
        scripts/deploy.sh
    )
    SCAN_SOURCES=()
    for deploy_file in "${SCAN_FILES[@]}"; do
        SCAN_SOURCES+=("$SRC_DIR/./$deploy_file")
    done
    SCAN_OPTIONS=(-avR)
    if $SCAN_DRY_RUN; then SCAN_OPTIONS+=(--dry-run); fi
    echo "# scanner-only deployment; dashboard, config, data and services untouched"
    rsync "${SCAN_OPTIONS[@]}" --timeout=30 \
        -e 'ssh -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=10 -o ServerAliveCountMax=2' \
        "${RSYNC_EXCLUDES[@]}" "${SCAN_SOURCES[@]}" "$HOST:$DEST/"
    exit 0
fi

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
