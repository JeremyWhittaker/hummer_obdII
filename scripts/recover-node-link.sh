#!/usr/bin/env bash
# Run from a WORKSTATION. Drives the node's two recovery steps over ssh.
#
# Why this exists: both steps live in Pi-only scripts, so typing them on a
# workstation gives "command not found" -- the same trap as
# `systemctl restart hummer-battery` in restart-node-services.sh, one level up.
# This names the node, uses `ssh -t` so sudo can prompt, and reads back the
# behaviour rather than the exit code.
#
#   scripts/recover-node-link.sh            # read-only: what is broken right now
#   scripts/recover-node-link.sh journal    # make the node's journal persistent
#   scripts/recover-node-link.sh pair       # re-pair the OBD adapter (interactive)
#   scripts/recover-node-link.sh all        # journal, then pair
#
# `pair` is interactive at the vehicle: the adapter pairs with Secure Simple
# Pairing and a confirmation has to be answered, and its pairing button may
# have to be pressed first. Nothing here can do that part for you.
#
# Each step prompts for the node's sudo password separately, because sudo
# timestamps are per-tty and every ssh -t is a new one.
#
# The MAC address is read from the node's own /etc/default/hummer-rfcomm and
# printed masked. It is not written down here: this file is public.
set -euo pipefail

NODE="${NODE:-jeremy@100.71.118.116}"
REMOTE="${REMOTE:-/home/jeremy/hummer-obd}"
STEP="${1:-status}"

case "$STEP" in
    status|journal|pair|all) ;;
    *) sed -n '2,20p' "$0"; exit 1 ;;
esac

say() { printf '\n== %s\n' "$*"; }
ok()  { printf '   \033[32mok\033[0m    %s\n' "$*"; }
no()  { printf '   \033[31mFAIL\033[0m  %s\n' "$*"; }
hm()  { printf '   \033[33m--\033[0m    %s\n' "$*"; }

node() { ssh -o BatchMode=yes -o ConnectTimeout=15 "$NODE" "$@"; }

say "reaching the node"
if ! node true 2>/dev/null; then
    echo "error: cannot ssh to $NODE." >&2
    echo "The Pi loses power with the truck, so this is the ordinary state when" >&2
    echo "it is off. Try again when it is awake." >&2
    exit 1
fi
ok "$NODE answers"

# ---------------------------------------------------------------- status ----
# Everything here is a read. None of it needs a password on the node.
state() {
    node 'bash -s' <<'REMOTE_STATUS'
set -u
cd /home/jeremy/hummer-obd 2>/dev/null || exit 1
MAC=$(. /etc/default/hummer-rfcomm 2>/dev/null; echo "${ADAPTER_MAC:-}")
echo "mac=$MAC"
echo "mac_masked=$(printf '%s' "$MAC" | sed 's/\(..:..:..\).*/\1:XX:XX:XX/')"
# A bonded classic adapter stops advertising, so an inquiry proves nothing
# either way. A direct page does: it answers by address or it is not there.
if [ -n "$MAC" ]; then
    echo "page=$(timeout 25 hcitool name "$MAC" 2>/dev/null | head -1)"
    echo "paired=$(timeout 10 bluetoothctl devices Paired 2>/dev/null | grep -ci "$MAC" || true)"
    echo "trusted=$(timeout 10 bluetoothctl devices Trusted 2>/dev/null | grep -ci "$MAC" || true)"
fi
echo "rfcomm=$(rfcomm -a 2>/dev/null | head -1)"
echo "now=$(date +%s)"
echo "recorder=$(systemctl is-active hummer-drive 2>&1)"
echo "silent=$(journalctl -u hummer-drive --since '-3min' --no-pager -o cat 2>/dev/null \
            | grep -c 'did not answer ATRV' || true)"
NEW=$(ls -t evidence/sessions/*.csv 2>/dev/null | head -1)
echo "session=${NEW##*/}"
echo "session_mtime=$(stat -c %Y "$NEW" 2>/dev/null || echo 0)"
echo "journal_storage=$(systemd-analyze cat-config systemd/journald.conf 2>/dev/null \
                      | grep -E '^\s*Storage=' | tail -1 | tr -d ' ' | cut -d= -f2)"
echo "journal_persistent=$(journalctl --header 2>/dev/null \
                          | grep -c '^File path: /var/log/journal/' || true)"
REMOTE_STATUS
}

declare -A ST

# Values can contain spaces and '=', so split on the first '=' only, and take
# keys from a fixed list rather than from whatever the node sent.
load_state() {
    local s k v
    s=$(state) || { echo "error: could not read the node's state" >&2; exit 1; }
    ST=()
    while IFS= read -r line; do
        k=${line%%=*}
        v=${line#*=}
        case "$k" in
            mac|mac_masked|page|paired|trusted|rfcomm|now|recorder|silent|\
            session|session_mtime|journal_storage|journal_persistent) ST["$k"]=$v ;;
        esac
    done <<<"$s"
}

num() { local v=${ST[$1]:-}; case "$v" in ''|*[!0-9]*) echo 0 ;; *) echo "$v" ;; esac; }

report() {
    load_state
    local page=${ST[page]:-} mac_masked=${ST[mac_masked]:-?} paired trusted age
    paired=$(num paired); trusted=$(num trusted)

    say "the OBD adapter"
    if [ -n "$page" ]; then
        ok "answers a direct page as '$page' ($mac_masked), so it is powered and in range"
    else
        no "does not answer a direct page: unplugged, out of range, or asleep"
    fi
    if [ "$paired" -gt 0 ] && [ "$trusted" -gt 0 ]; then
        ok "paired and trusted"
    elif [ "$paired" -gt 0 ]; then
        hm "paired but NOT trusted; it will not reconnect unattended"
    else
        no "NOT paired -- this is what stops the recorder opening a link"
    fi
    printf '   rfcomm: %s\n' \
        "$(printf '%s' "${ST[rfcomm]:-<none>}" | sed 's/\([0-9A-Fa-f:]\{8\}\)[0-9A-Fa-f:]\{9\}/\1:XX:XX:XX/')"

    say "the recorder"
    if [ "${ST[recorder]:-}" = active ]; then
        ok "hummer-drive is active"
    else
        no "hummer-drive is ${ST[recorder]:-unknown}"
    fi
    # The link state in `rfcomm -a` is the authority on whether there is a peer
    # at all. /dev/rfcomm0's mtime is NOT: the recorder's own write attempts
    # touch it even when nothing is connected, so "written 61s ago" was
    # reported as "bytes are flowing" while the link was closed and unpaired.
    # It survives only as a weak signal, and never as the thing that passes.
    local rows_age=$(( $(num now) - $(num session_mtime) ))
    if [ "$(num silent)" -gt 0 ]; then
        no "it has logged $(num silent) unanswered ATRV probes in the last 3 minutes"
    fi
    case "${ST[rfcomm]:-}" in
        *" connected"*)
            if [ "$(num session_mtime)" -gt 0 ] && [ "$rows_age" -lt 180 ]; then
                ok "the link is open and rows were written ${rows_age}s ago"
            else
                hm "the link is open, but no rows for ${rows_age}s; the truck may be asleep"
            fi
            ;;
        *" closed"*)
            no "the link is closed: nothing is being recorded"
            ;;
        *)
            hm "link state unknown: ${ST[rfcomm]:-<none>}"
            ;;
    esac
    printf '   newest session: %s (written %ss ago)\n' "${ST[session]:-<none>}" "$rows_age"

    say "the journal"
    if [ "$(num journal_persistent)" -gt 0 ]; then
        ok "persistent (Storage=${ST[journal_storage]:-?}); past boots survive a power cut"
    else
        no "volatile (Storage=${ST[journal_storage]:-?}); every boot is erased with its power"
    fi
}

report
if [ "$STEP" = status ]; then
    echo
    echo 'Read-only. Nothing was changed.'
    exit 0
fi

# --------------------------------------------------------------- journal ----
if [ "$STEP" = journal ] || [ "$STEP" = all ]; then
    say "making the journal persistent (needs the node's sudo password)"
    # Deliberately first: once it is on, the pairing attempt below is itself
    # recorded, instead of vanishing with the next power cut.
    ssh -t "$NODE" "sudo bash $REMOTE/scripts/enable-persistent-journal.sh"
fi

# ------------------------------------------------------------------ pair ----
if [ "$STEP" = pair ] || [ "$STEP" = all ]; then
    # report() above already loaded the state; reuse it rather than paging the
    # adapter a second time.
    MAC=${ST[mac]:-}
    PAGE=${ST[page]:-}
    if [ -z "$MAC" ]; then
        echo 'error: the node has no ADAPTER_MAC in /etc/default/hummer-rfcomm.' >&2
        echo 'That is a first-time setup, not a re-pair: run pair_obdlink.sh scan' >&2
        echo 'on the Pi and work through its steps.' >&2
        exit 1
    fi
    if [ -z "$PAGE" ]; then
        echo 'error: the adapter does not answer. Nothing was changed.' >&2
        echo 'Check it is seated in the OBD port and that you are near the truck,' >&2
        echo 'then run this again. Pairing a device that cannot be reached only' >&2
        echo 'produces a confusing failure.' >&2
        exit 1
    fi

    say "pairing (needs the node's sudo password, and you at the vehicle)"
    cat <<'NOTE'
   The adapter uses Secure Simple Pairing, so this cannot be done unattended:
   answer the confirmation when it appears, and press the adapter's pairing
   button first if the first attempt fails -- its window is short.

   The recorder is stopped for the attempt and started again afterwards, even
   if this fails. It is recording nothing right now, so that costs no data,
   and its repeated attempts to open the port would otherwise land in the
   middle of the handshake.
NOTE
    # start/stop of the recorder is authorised by the node's polkit rules
    # without a password; restart of anything else is not.
    node 'systemctl --no-ask-password stop hummer-drive' || true
    trap 'ssh -o BatchMode=yes '"$NODE"' "systemctl --no-ask-password start hummer-drive" || true' EXIT
    ssh -t "$NODE" "sudo bash $REMOTE/scripts/pair_obdlink.sh pair $MAC" || true
    trap - EXIT
    node 'systemctl --no-ask-password start hummer-drive' || true

    say "did it take?"
    # The recorder needs a moment to reopen the link before this means anything.
    node 'sleep 45' || true
    report

    echo
    if [ "$(num paired)" -gt 0 ] && [ "$(num trusted)" -gt 0 ]; then
        echo 'Paired and trusted. If the serial device is still quiet, the truck may'
        echo 'simply be asleep -- the recorder waits for it and starts a session on'
        echo 'its own. Check again in a few minutes with no arguments.'
    else
        echo 'Still not paired and trusted. Press the adapter button and run `pair`' >&2
        echo 'again; if it keeps refusing, work through scripts/pair_obdlink.sh on' >&2
        echo 'the Pi, which prints every step it takes.' >&2
        exit 1
    fi
fi
