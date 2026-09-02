#!/bin/bash
# Self-supervising bounded collector trial.
#
# Prefer systemd/hummer-collector-trial.service. Use this only when that unit
# cannot be installed -- installing it needs root, and a `sudo` invocation over
# an unreliable link is exactly the fragile step this script exists to avoid.
# It mirrors the unit's guarantees without needing root:
#
#   Restart=on-failure    a non-zero exit relaunches the collector
#   RestartSec            RETRY_SLEEP_S between attempts
#   StartLimitBurst       MAX_RETRIES, then it gives up rather than looping
#   RuntimeMaxSec         HARD_MAX_S wall-clock ceiling regardless of config
#   (clean exit)          rc=0 means the trial finished its own --duration-s
#                         and must not be restarted
#
# Launch it detached, with a SHORT command, so a dropped SSH session cannot
# interrupt the launch itself:
#
#     cd ~/hummer-obd && setsid ./scripts/run_trial.sh >/dev/null 2>&1 </dev/null &
#
# The trial config must be a SEPARATE file so config/hummer.toml is never left
# modified by a trial.
set -uo pipefail

DEST="${DEST:-/home/jeremy/hummer-obd}"
TRIAL_CONFIG="${TRIAL_CONFIG:-config/hummer-trial.toml}"
LOG="${LOG:-logs/collector-trial.log}"
HARD_MAX_S="${HARD_MAX_S:-7200}"
MAX_RETRIES="${MAX_RETRIES:-5}"
RETRY_SLEEP_S="${RETRY_SLEEP_S:-30}"

cd "$DEST" || exit 1
mkdir -p "$(dirname "$LOG")"

if [ ! -f "$TRIAL_CONFIG" ]; then
    echo "$(date -Is) refusing to start: $TRIAL_CONFIG does not exist" >> "$LOG"
    exit 1
fi

HARD_END=$(( $(date +%s) + HARD_MAX_S ))
TRIES=0

echo "$(date -Is) trial starting: config=$TRIAL_CONFIG max_retries=$MAX_RETRIES hard_max=${HARD_MAX_S}s" >> "$LOG"

while [ "$TRIES" -lt "$MAX_RETRIES" ] && [ "$(date +%s)" -lt "$HARD_END" ]; do
    PYTHONPATH=src python3 -u -m hummer_obd.collector \
        --config "$TRIAL_CONFIG" --root . --force >> "$LOG" 2>&1
    RC=$?
    echo "$(date -Is) collector exited rc=$RC (restart $TRIES of $MAX_RETRIES)" >> "$LOG"
    # A clean exit is the collector honouring its own --duration-s. Restarting
    # that would turn a bounded trial into an unbounded one.
    [ "$RC" -eq 0 ] && break
    TRIES=$(( TRIES + 1 ))
    sleep "$RETRY_SLEEP_S"
done

echo "$(date -Is) trial supervisor finished after $TRIES restart(s)" >> "$LOG"
