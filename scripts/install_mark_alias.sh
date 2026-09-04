#!/bin/bash
# Install a short `mark` command on the node.
#
# `hummer-obd-experiment mark "..."` is the real interface and this changes
# nothing about it.  The point is length: this gets typed at the vehicle, on a
# phone, one-handed, in the dark, while something is happening that you want
# timestamped now rather than in thirty seconds.  A command you will not type is
# a measurement you will not take.
#
# Safe to re-run.  Writes nothing outside the user's home directory and touches
# no vehicle.
set -euo pipefail

DEST="${DEST:-$HOME/hummer-obd}"

cat > "$HOME/mark" <<SH
#!/bin/bash
# Record that something just happened to the vehicle, with a UTC timestamp.
cd "$DEST" && PYTHONPATH=src exec python3 -m hummer_obd.experiment mark "\$@"
SH
chmod +x "$HOME/mark"

if ! grep -q 'alias mark=' "$HOME/.bashrc" 2>/dev/null; then
    echo 'alias mark="$HOME/mark"' >> "$HOME/.bashrc"
fi

echo "# installed $HOME/mark -> $DEST"
echo "# usage:  mark \"hvac max A/C\""
echo "# marks land in $DEST/evidence/experiments/marks.jsonl, append-only"
