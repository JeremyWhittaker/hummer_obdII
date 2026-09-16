#!/bin/bash
# One-time, operator-run installation on the Pi. Never invokes sudo or changes
# service state. Unlike enable_service_control.sh, this grants no restart or
# other-service rights and does not change recorder configuration.
set -euo pipefail

fail() { echo "error: $*" >&2; exit 1; }

main() {
    # Ignore user PATH entries (the operator's sudo also sanitizes shell functions).
    PATH=/usr/sbin:/usr/bin:/sbin:/bin
    export PATH
    local check_only=false script_dir source_rule rule_dir
    case "${1:-}" in
        "") [[ $# == 0 ]] || fail "usage: $0 [--check]" ;;
        --check) [[ $# == 1 ]] || fail "usage: $0 [--check]"; check_only=true ;;
        *) fail "usage: $0 [--check]" ;;
    esac
    script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
    # shellcheck source=lib/require-node.sh
    source "$script_dir/lib/require-node.sh"
    require_node
    if ! $check_only && (( EUID != 0 )); then
        fail "one-time installation requires root; ask the operator to run this script with sudo"
    fi
    source_rule="$script_dir/polkit/49-hummer-obd-recorder.rules"
    rule_dir=/etc/polkit-1/rules.d
    check_prerequisites "$source_rule" "$rule_dir"
    if $check_only; then
        echo "Prerequisites verified; this check did not install a rule or change services."
        return
    fi
    install_rule "$source_rule" "$rule_dir/49-hummer-obd-recorder.rules"
}

# Pin the reviewed rule. Check the root-owned staged copy, not the mutable
# checkout, so a changed source cannot slip a broader policy into this install.
check_rule_bytes() {
    local digest
    digest=$(sha256sum -- "$1")
    [[ "${digest%% *}" == 0ea1f148376f03321159e1a7326ba98d36044988ed726ced2a01700e014a6f17 ]] ||
        fail "rule does not match the reviewed policy; nothing installed"
}

check_prerequisites() {
    local source_rule="$1" rule_dir="$2" dependency rule_mode recorder_uid
    for dependency in systemctl pkaction id stat cmp install mktemp ln rm sha256sum; do
        command -v "$dependency" >/dev/null || fail "missing dependency: $dependency"
    done
    recorder_uid=$(id -u jeremy) || fail "jeremy account does not exist"
    [[ "$recorder_uid" =~ ^[0-9]+$ && "$recorder_uid" != 0 ]] ||
        fail "jeremy must be a non-root account"
    [[ "$(systemctl show --property=LoadState --value hummer-drive.service)" == loaded ]] ||
        fail "hummer-drive.service is not loaded"
    [[ "$(systemctl show --property=Id --value hummer-drive.service)" == hummer-drive.service ]] ||
        fail "hummer-drive.service must be the exact unit, not an alias"
    [[ "$(systemctl show --property=User --value hummer-drive.service)" == jeremy ]] ||
        fail "hummer-drive.service must run as jeremy"
    systemctl is-active --quiet polkit.service || fail "polkit.service is not active"
    [[ "$(pkaction --action-id org.freedesktop.systemd1.manage-units)" == \
        org.freedesktop.systemd1.manage-units ]] || fail "systemd polkit action unavailable"
    [[ -f "$source_rule" && ! -L "$source_rule" ]] || fail "missing or symlinked source rule"
    check_rule_bytes "$source_rule"
    [[ -d "$rule_dir" && ! -L "$rule_dir" ]] || fail "missing or symlinked polkit rules directory"
    [[ "$(stat -c %u "$rule_dir")" == 0 ]] || fail "polkit rules directory is not root-owned"
    rule_mode=$(stat -c %a "$rule_dir")
    (( (8#$rule_mode & 0022) == 0 )) || fail "polkit rules directory is group/world writable"
}

# A separate function makes the real filesystem operations testable in a
# temporary directory without adding a destination override to the root CLI.
install_rule() (
    local source_rule="$1" destination="$2" staged_rule=""
    [[ ! -L "$destination" ]] || fail "refusing symlink at $destination"
    if [[ -e "$destination" ]]; then
        [[ -f "$destination" ]] || fail "not a regular file: $destination"
        cmp -s "$source_rule" "$destination" || fail "different rule already exists: $destination; left unchanged"
        [[ "$(stat -c '%u:%g:%a' "$destination")" == 0:0:644 ]] ||
            fail "existing rule has unexpected ownership/mode; left unchanged"
        check_rule_bytes "$destination"
        echo "Recorder permission already installed; services unchanged."
        exit 0
    fi
    # A dotfile without .rules is not loaded by polkit. Install complete bytes
    # and ownership first, then publish with an atomic, no-overwrite hard link.
    staged_rule=$(mktemp "${destination%/*}/.hummer-obd-recorder.XXXXXXXX")
    trap 'if [[ -n "$staged_rule" ]]; then rm -f -- "$staged_rule"; fi' EXIT
    install -o root -g root -m 0644 -- "$source_rule" "$staged_rule"
    [[ "$(stat -c '%u:%g:%a' "$staged_rule")" == 0:0:644 ]] ||
        fail "staged rule has unexpected ownership/mode"
    check_rule_bytes "$staged_rule"
    ln -T -- "$staged_rule" "$destination"
    echo "Installed recorder start/stop permission for jeremy; services unchanged."
    echo "Polkit reloads rules automatically; no reboot or service restart is needed."
)

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
