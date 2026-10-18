#!/usr/bin/env bash
# Restart the node's API and battery watch from a workstation, and prove it took.
#
# Why this exists: `systemctl restart hummer-battery hummer-dashboard` fails with
# "Unit hummer-battery.service not found" when it is run on a workstation,
# because those units only exist on the Pi. The node's polkit rules authorise
# start/stop of hummer-drive without a password but not `restart` of these two,
# so they need sudo ON THE NODE -- which is why this uses `ssh -t` and will
# prompt for the node's password.
#
# WHAT IT DELIBERATELY DOES NOT TOUCH: hummer-drive, the recorder. That is the
# one service whose interruption loses data that cannot be taken again. Neither
# unit here depends on it (checked: hummer-dashboard wants only
# network-online.target and mounts, hummer-battery only multi-user.target), so
# restarting them cannot cascade -- and this script asserts the recorder is still
# running afterwards rather than assuming that.
#
# An exit code is not acceptance, so the checks at the end read the behaviour:
# that the battery watch identifies the board and reads a plausible cell, and
# that the API serves the history column the 3D view draws from.
#
#   scripts/restart-node-services.sh           # restart both, then verify
#   scripts/restart-node-services.sh --check   # verify only, no sudo, no change
set -euo pipefail

NODE="${NODE:-jeremy@100.71.118.116}"
API="${API:-http://100.71.118.116:8765}"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

say() { printf '\n== %s\n' "$*"; }
ok()  { printf '   \033[32mok\033[0m    %s\n' "$*"; }
bad() { printf '   \033[31mFAIL\033[0m  %s\n' "$*"; }

say "reaching the node"
if ! ssh -o BatchMode=yes -o ConnectTimeout=10 "$NODE" true 2>/dev/null; then
    echo "error: cannot ssh to $NODE." >&2
    echo "The Pi loses power with the truck, so this is the ordinary state when" >&2
    echo "it is off. Try again when the truck is awake." >&2
    exit 1
fi
ok "$NODE answers"

say "before"
ssh -o BatchMode=yes "$NODE" '
  for u in hummer-drive hummer-rfcomm hummer-dashboard hummer-battery; do
    printf "   %-20s %s\n" "$u" "$(systemctl is-active "$u" 2>&1)"
  done
  printf "   %-20s %s\n" "battery watch says" \
    "$(journalctl -u hummer-battery -n 1 --no-pager -o cat 2>/dev/null | tail -1)"
'

if [ "$CHECK_ONLY" -eq 0 ]; then
    say "restarting the API and the battery watch (needs the node's sudo password)"
    # Both in one invocation so there is one password prompt, and the recorder is
    # named nowhere in it.
    ssh -t "$NODE" 'sudo systemctl restart hummer-battery hummer-dashboard'
    sleep 8
fi

say "after"
FAILED=0

# 1. The three services that should be up, and the recorder specifically.
while read -r unit state; do
    if [ "$state" = "active" ]; then ok "$unit is active"; else bad "$unit is $state"; FAILED=1; fi
done < <(ssh -o BatchMode=yes "$NODE" '
  for u in hummer-drive hummer-dashboard hummer-battery; do
    echo "$u $(systemctl is-active "$u" 2>&1)"
  done')

# 2. The battery watch reads the board, rather than merely running. Before the
#    PiSugar3 support landed this printed "no power IC read a plausible cell
#    voltage" every 30 s while the service sat there "active".
BATT=$(ssh -o BatchMode=yes "$NODE" '
  cd /home/jeremy/hummer-obd 2>/dev/null || exit 1
  PYTHONPATH=src python3 -c "
from hummer_obd import battery as B
try:
    read = B.open_i2c_reader(1)
except Exception as exc:
    print(\"no-i2c\", exc); raise SystemExit(0)
chip = B.identify_chip(read)
if chip is None:
    print(\"unidentified\"); raise SystemExit(0)
r = B.read_voltage(read, chip)
print(chip.name, \"%.3f\" % r.volts if r.volts is not None else \"none\", r.plausible)
" 2>&1 | tail -1')
case "$BATT" in
    *" True") ok "battery watch reads the board: $BATT" ;;
    *)        bad "battery watch cannot read the board: $BATT"; FAILED=1 ;;
esac

# 3. The API serves the column the 3D charge strip draws from. This is the whole
#    reason hummer-dashboard needed restarting: the code was deployed to the node
#    while the running process still held the old history builder in memory.
HIST=$(curl -s --max-time 25 "$API/api/snapshot?session=latest" 2>/dev/null \
  | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("no-answer"); raise SystemExit(0)
h = d.get("history") or []
rows = sum(1 for r in h if "array_2b43" in r)
# A session recorded before the column existed legitimately has none of it, so
# "absent" is distinguished from "the API cannot carry it at all".
print("stated-in-%d-of-%d" % (rows, len(h)) if h else "empty-session")
' 2>/dev/null)
case "$HIST" in
    stated-in-0-of-*) printf '   \033[33m--\033[0m    API is up; this session carries no array_2b43 yet (%s)\n' "$HIST" ;;
    stated-in-*)      ok "API serves array_2b43 ($HIST)" ;;
    empty-session)    printf '   \033[33m--\033[0m    API is up; no rows in the live session yet\n' ;;
    *)                bad "API did not answer ($HIST)"; FAILED=1 ;;
esac

# 4. Whether the board will bring the Pi back by itself. Read-only: nothing in
#    this project writes to a power-management MCU.
CTR=$(ssh -o BatchMode=yes "$NODE" '
  cd /home/jeremy/hummer-obd 2>/dev/null || exit 1
  PYTHONPATH=src python3 -c "
from hummer_obd import battery as B
try:
    read = B.open_i2c_reader(1)
    c = read(0x57, B.PISUGAR3_CTR1, True)
except Exception as exc:
    print(\"unreadable\"); raise SystemExit(0)
print(\"0x%02X restore=%s plugged=%s\" % (c, bool(c & B.PISUGAR3_RESTORE_BIT), bool(c & B.PISUGAR3_PLUGGED_BIT)))
" 2>&1 | tail -1')
case "$CTR" in
    *"restore=True"*) ok "PiSugar3 will power the Pi back up when power returns ($CTR)" ;;
    unreadable)       printf '   \033[33m--\033[0m    PiSugar3 control register not readable (no PiSugar3 fitted?)\n' ;;
    *)                bad "auto power-on is NOT set ($CTR)"; FAILED=1 ;;
esac

echo
if [ "$FAILED" -eq 0 ]; then
    echo "All checks passed."
else
    echo "Something above did not come back. The recorder is the one that matters:" >&2
    echo "if hummer-drive is not active, start it with" >&2
    echo "  ssh $NODE 'systemctl --no-ask-password start hummer-drive'" >&2
    echo "which needs no password." >&2
    exit 1
fi
