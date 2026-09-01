#!/bin/bash
# Discover, pair, trust and bind an OBDLink adapter — one deliberate step at a
# time, with a hard rule: if the scan does not produce exactly one unambiguous
# OBDLink-looking device, the script stops and prints what it saw.
#
#   sudo ./pair_obdlink.sh scan          # inquiry only, no pairing
#   sudo ./pair_obdlink.sh pair <MAC>    # interactive SSP pair + trust
#   sudo ./pair_obdlink.sh sdp  <MAC>    # print the Serial Port Profile record
#   sudo ./pair_obdlink.sh bind <MAC> <CHANNEL>
set -euo pipefail

PATH="$PATH:/usr/sbin:/sbin"
NAME_PATTERN='obdlink|obd ?ii|obd2|stn[0-9]|scantool'

usage() { sed -n '2,12p' "$0"; exit 1; }

validate_mac() {
    [[ "$1" =~ ^([[:xdigit:]]{2}:){5}[[:xdigit:]]{2}$ ]] || {
        echo "REFUSING: invalid Bluetooth MAC address: $1" >&2
        exit 2
    }
}

validate_channel() {
    [[ "$1" =~ ^[0-9]+$ ]] && ((10#$1 >= 1 && 10#$1 <= 30)) || {
        echo "REFUSING: RFCOMM channel must be an integer from 1 through 30." >&2
        exit 2
    }
}

scan() {
    echo "# classic inquiry (20s) — the adapter must be in pairing mode"
    hcitool scan --flush | tail -n +2 | sed 's/^\s*//'
}

candidates() {
    scan | grep -Ei "$NAME_PATTERN" || true
}

case "${1:-}" in
scan)
    echo "# all devices seen:"
    scan
    echo
    echo "# OBDLink-looking candidates:"
    MATCHES="$(candidates)"
    if [[ -z "$MATCHES" ]]; then
        echo "(none — leave the adapter in pairing mode and rerun)"
        exit 2
    fi
    echo "$MATCHES"
    COUNT="$(echo "$MATCHES" | wc -l)"
    if [[ "$COUNT" -ne 1 ]]; then
        echo
        echo "REFUSING TO CONTINUE: $COUNT candidate devices. A human must confirm which"
        echo "one is the vehicle's adapter before anything is paired."
        exit 3
    fi
    ;;
pair)
    MAC="${2:?usage: pair <MAC>}"
    validate_mac "$MAC"
    NAME="$(hcitool name "$MAC" || true)"
    echo "# device $MAC reports name: ${NAME:-<none>}"
    if ! grep -qiE "$NAME_PATTERN" <<<"$NAME"; then
        echo "REFUSING: $MAC does not identify as an OBDLink adapter." >&2
        exit 3
    fi
    # This adapter refuses unattended association: a NoInputNoOutput agent gets
    # org.bluez.Error.AuthenticationFailed.  It pairs with Secure Simple
    # Pairing against an agent that can confirm, so this is interactive.
    echo "# pairing interactively - ANSWER 'yes' TO THE SIX-DIGIT CONFIRMATION"
    echo "# (press the adapter's pairing button first; the window is short)"
    bluetoothctl --agent KeyboardDisplay --timeout 40 pair "$MAC"
    bluetoothctl --timeout 10 trust "$MAC"
    bluetoothctl --timeout 10 info "$MAC" | grep -E "Name|Paired|Bonded|Trusted"
    ;;
sdp)
    MAC="${2:?usage: sdp <MAC>}"
    validate_mac "$MAC"
    sdptool browse --tree "$MAC" || sdptool records "$MAC"
    ;;
bind)
    MAC="${2:?usage: bind <MAC> <CHANNEL>}"
    CHANNEL="${3:?usage: bind <MAC> <CHANNEL>}"
    validate_mac "$MAC"
    validate_channel "$CHANNEL"
    rfcomm release /dev/rfcomm0 2>/dev/null || true
    rfcomm bind /dev/rfcomm0 "$MAC" "$CHANNEL"
    ls -l /dev/rfcomm0
    printf 'ADAPTER_MAC=%s\nSPP_CHANNEL=%s\n' "$MAC" "$CHANNEL" > /etc/default/hummer-rfcomm
    echo "# wrote /etc/default/hummer-rfcomm (used by hummer-rfcomm.service)"
    ;;
*)
    usage
    ;;
esac
