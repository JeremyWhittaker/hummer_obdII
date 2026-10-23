#!/usr/bin/env bash
# Make the node's journal survive a power cut. Run as root ON THE PI.
#
# Why this exists: /etc/systemd/journald.conf on this node has said
# `Storage=persistent` since 2026-09-03, and it has never had any effect.
# Raspberry Pi OS ships /usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf,
# and a drop-in is applied AFTER the main file, so the vendor's
# `Storage=volatile` wins. /var/log/journal exists and has stayed empty since
# the image was written; every boot logs to /run and is destroyed with it.
#
# That is not a cosmetic problem. It is why the October 19 drive audit could
# not see the boot that lost the recordings, why the cause of the October 21
# 00:56 power-off cannot be established, and why an automatic reserve shutdown
# followed by a restored-input boot -- the one acceptance test still owed on
# the UPS -- leaves no evidence behind: the shutdown says
# "LOW BATTERY: graceful reserve shutdown" to a journal that is in a tmpfs,
# and /run/hummer-low-battery is in the same tmpfs.
#
# The fix is one drop-in in /etc, which sorts after the vendor's 40- file and
# therefore wins. Nothing else is changed: the retention policy already in
# /etc/systemd/journald.conf (200 MiB cap, 20 MiB per file, two weeks) is what
# bounds the write load on the SD card, and this script prints it rather than
# choosing it.
#
# It does not recover past boots. Nothing can; they were never written.
#
#   sudo bash scripts/enable-persistent-journal.sh --check   # report only
#   sudo bash scripts/enable-persistent-journal.sh           # make it persistent
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
source "$ROOT/scripts/lib/require-node.sh"
require_node

DROPIN=/etc/systemd/journald.conf.d/50-hummer-persistent.conf
VENDOR=/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf

effective_storage() {
    # The effective value is the last Storage= line across every source, which
    # is the whole point: reading /etc/systemd/journald.conf alone is what made
    # this look configured for a year.
    systemd-analyze cat-config systemd/journald.conf 2>/dev/null \
        | grep -E '^\s*Storage=' | tail -1 | tr -d ' ' | cut -d= -f2
}

journal_paths() {
    journalctl --header 2>/dev/null | awk '/^File path:/ {print $3}'
}

persistent_files() {
    journal_paths | grep -c '^/var/log/journal/' || true
}

report() {
    printf '   effective Storage : %s\n' "$(effective_storage)"
    printf '   journal files now : %s in /var/log, %s in /run\n' \
        "$(persistent_files)" "$(journal_paths | grep -c '^/run/log/journal/' || true)"
    # Count boot rows, not the IDX header: a number that is off by one here is
    # exactly the kind of claim this project does not make.
    printf '   boots on record   : %s\n' \
        "$(journalctl --list-boots --no-pager 2>/dev/null | grep -cE '^[[:space:]]*-?[0-9]+[[:space:]]' || true)"
    printf '   retention in force: %s\n' \
        "$(systemd-analyze cat-config systemd/journald.conf 2>/dev/null \
           | grep -E '^\s*(SystemMaxUse|SystemMaxFileSize|SystemMaxFiles|MaxRetentionSec)=' \
           | tr -d ' ' | paste -sd' ' -)"
    printf '   vendor drop-in    : %s\n' \
        "$([ -f "$VENDOR" ] && grep -hE '^\s*Storage=' "$VENDOR" | tr -d ' ' || echo 'absent')"
    printf '   our drop-in       : %s\n' \
        "$([ -f "$DROPIN" ] && echo "$DROPIN" || echo 'not installed')"
}

echo '== before'
report

if [ "${1:-}" = --check ]; then
    echo
    if [ "$(persistent_files)" -gt 0 ]; then
        echo 'Already persistent. Nothing to do, and nothing was changed.'
    elif [ "$(effective_storage)" = persistent ]; then
        # Half done, which is a state worth naming rather than calling it
        # volatile: the setting is right, so the next boot will be recorded,
        # but this boot is still only in /run because nothing flushed it.
        echo 'The setting is right, but this boot is still only in /run: nothing'
        echo 'has asked journald to migrate. The next boot will be recorded; this'
        echo 'one will not. Re-run without --check, as root, to flush it now.'
    else
        echo 'The journal is volatile: this boot is all there will ever be.'
        echo 'Nothing was changed. Re-run without --check, as root, to fix it.'
    fi
    exit 0
fi

if [ "$EUID" -ne 0 ]; then
    echo 'Run on the Pi: sudo bash /home/jeremy/hummer-obd/scripts/enable-persistent-journal.sh' >&2
    exit 1
fi

# journald writes into /var/log/journal only if the directory is there; it
# creates the machine-id subdirectory itself. The group and setgid bit are what
# let adm read the journal without root, which is how the audits read it.
if [ ! -d /var/log/journal ]; then
    install -d -o root -g systemd-journal -m 2755 /var/log/journal
    echo '   created /var/log/journal'
fi

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP="/var/backups/hummer-journal/$STAMP"
if [ -e "$DROPIN" ] || [ -L "$DROPIN" ]; then
    install -d -m 0700 "$BACKUP"
    cp -a --parents "$DROPIN" "$BACKUP/"
    echo "   previous drop-in backed up under $BACKUP"
fi

install -d -m 0755 /etc/systemd/journald.conf.d
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
{
    printf '# Installed by scripts/enable-persistent-journal.sh.\n'
    printf '# Overrides %s,\n' "$VENDOR"
    printf '# which otherwise defeats Storage=persistent in the main file: a drop-in is\n'
    printf '# applied after it, and 50- sorts after 40-. Removing the vendor file instead\n'
    printf '# would be undone by the next rpi update.\n'
    printf '[Journal]\n'
    printf 'Storage=persistent\n'
} > "$TMP"
install -o root -g root -m 0644 "$TMP" "$DROPIN"

if [ "$(effective_storage)" != persistent ]; then
    echo "error: Storage is still $(effective_storage) after writing $DROPIN." >&2
    echo 'Something later in systemd-analyze cat-config is overriding it.' >&2
    exit 1
fi

systemctl restart systemd-journald

# Restarting is not enough, and this cost a day. journald re-reads the config
# and knows Storage=persistent, but it keeps writing to the runtime journal
# until something asks it to migrate. At boot that is
# systemd-journal-flush.service -- a one-shot whose ExecStart is literally
# `journalctl --flush`, which has already run and will not run again. Without
# this call the first run installed the drop-in, restarted the daemon, and left
# /var/log/journal empty: persistence would have begun only at the NEXT boot,
# which is the one event we are trying to record.
journalctl --flush
journalctl --sync 2>/dev/null || true

echo
echo '== after'
report

# An exit code proves the command ran, not that logging changed. The acceptance
# is a journal file in /var/log and a recorder that never noticed.
FAILED=0
if [ "$(persistent_files)" -gt 0 ]; then
    echo '   ok    the journal is now written to /var/log/journal'
else
    echo '   FAIL  journald was restarted and flushed, and is STILL writing only' >&2
    echo '         to /run. The drop-in is in place and the effective Storage is' >&2
    echo '         persistent, so the next boot will use /var/log/journal anyway;' >&2
    echo '         what is lost is this boot. A reboot would also settle it.' >&2
    FAILED=1
fi
for unit in hummer-drive.service hummer-dashboard.service hummer-battery.service; do
    if [ "$(systemctl is-active "$unit" 2>/dev/null || :)" = active ]; then
        echo "   ok    $unit untouched and active"
    else
        echo "   FAIL  $unit is $(systemctl is-active "$unit" 2>&1)" >&2
        FAILED=1
    fi
done

echo
if [ "$FAILED" -ne 0 ]; then
    exit 1
fi
cat <<'EOF'
Done. From the next boot onwards, `journalctl --list-boots` shows previous
boots, which is what makes these provable after the fact:

  * an automatic reserve shutdown -- "LOW BATTERY: graceful reserve shutdown"
    from hummer-battery, followed by a clean systemd power-off sequence;
  * the boot that follows it, and whether anyone pressed the button;
  * a Bluetooth controller hang that only shows up in kernel HCI errors.

This boot's own history is still only in /run and is lost at the next power
cut, as every boot before it was.
EOF
