#!/bin/bash
# Switch wlan0 to a saved NetworkManager profile, with a self-healing fallback.
#
# Autoconnect priority does not preempt a healthy active connection, so moving
# to the hotspot needs an explicit activation.  That drops the link this
# session is running over, so the script is meant to be launched detached: it
# verifies the target SSID is actually in range first, activates the profile,
# waits for real connectivity (address + DNS + internet), and puts the previous
# profile back if the target does not come up.  It never prints a Wi-Fi key.
set -uo pipefail
export PATH="$PATH:/usr/sbin:/sbin"

TARGET="${1:?usage: switch_wifi_profile.sh <saved-profile> [log-path]}"
LOG="${2:-/home/jeremy/hummer-obd/logs/wifi-switch.log}"
mkdir -p "$(dirname "$LOG")"
: > "$LOG"
# This runs as root but the log carries no secrets, so hand it to the operator
# who owns the project directory; otherwise they cannot follow their own switch.
OWNER="$(stat -c '%U:%G' "$(dirname "$LOG")" 2>/dev/null || echo root:root)"
chown "$OWNER" "$LOG" 2>/dev/null || true
chmod 640 "$LOG" 2>/dev/null || true
exec >>"$LOG" 2>&1

echo "=== $(date -Is) switch wlan0 -> '$TARGET' ==="
PREV="$(nmcli -t -f NAME,DEVICE connection show --active | awk -F: '$2=="wlan0"{print $1}' | head -1)"
echo "previous active profile: ${PREV:-<none>}"

echo "--- scan (SSID/signal only) ---"
nmcli device wifi rescan >/dev/null 2>&1
sleep 6
nmcli -t -f ACTIVE,SSID,SIGNAL,SECURITY device wifi list | head -25

TARGET_SSID="$(nmcli -t -f 802-11-wireless.ssid connection show "$TARGET" 2>/dev/null | cut -d: -f2-)"
echo "target profile SSID: ${TARGET_SSID:-<unknown>}"
if ! nmcli -t -f SSID device wifi list | grep -Fxq "${TARGET_SSID:-$TARGET}"; then
    echo "RESULT: target SSID is not visible in the scan; refusing to drop a working link"
    echo "=== $(date -Is) no change ==="
    exit 2
fi

connectivity_ok() {
    ip -4 addr show wlan0 | grep -q "inet " || return 1
    getent hosts deb.debian.org >/dev/null 2>&1 || return 1
    timeout 6 ping -c1 -W4 1.1.1.1 >/dev/null 2>&1 || return 1
    return 0
}

echo "--- activating '$TARGET' ---"
timeout 75 nmcli connection up "$TARGET"
echo "nmcli rc=$?"

OK=1
for i in $(seq 1 15); do
    if connectivity_ok; then OK=0; break; fi
    sleep 5
done
echo "connectivity check: rc=$OK after $((i * 5))s"

if [ "$OK" -ne 0 ] && [ -n "$PREV" ] && [ "$PREV" != "$TARGET" ]; then
    echo "--- '$TARGET' did not come up; restoring '$PREV' ---"
    timeout 75 nmcli connection up "$PREV"
    sleep 10
    if connectivity_ok; then echo "RESULT: fallback to '$PREV' OK"; else echo "RESULT: FALLBACK ALSO FAILED"; fi
else
    [ "$OK" -eq 0 ] && echo "RESULT: '$TARGET' active with working connectivity"
fi

echo "--- final state ---"
nmcli -t -f NAME,TYPE,DEVICE,STATE connection show --active
nmcli -t -f ACTIVE,SSID,SIGNAL device wifi list | grep '^yes' || echo "(no associated AP)"
ip -4 addr show wlan0 | grep inet
ip route | head -3
grep -v '^#' /etc/resolv.conf | grep .
getent hosts deb.debian.org >/dev/null 2>&1 && echo "dns: OK" || echo "dns: FAIL"
timeout 20 tailscale ip -4 2>&1 | head -2
timeout 20 tailscale status --self --peers=false 2>&1 | head -2
echo "services: ssh=$(systemctl is-active ssh) tailscaled=$(systemctl is-active tailscaled) NetworkManager=$(systemctl is-active NetworkManager)"
echo "=== $(date -Is) done ==="
