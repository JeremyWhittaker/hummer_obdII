#!/usr/bin/env bash
#
# Safely patch an existing Raspberry Pi OS SD card for the Hummer OBD-II Pi.
# This script never reflashes, repartitions, formats, or writes an image.
# Run it interactively as root, for example:
#   sudo ./patch_hummer_pi_sd.sh
#
set -Eeuo pipefail
IFS=$'\n\t'
umask 077

readonly PI_HOSTNAME="hummer"
readonly PI_USERNAME="jeremy"
readonly WIFI_COUNTRY="US"
readonly PI_TIMEZONE="America/Phoenix"
readonly BOOT_MOUNT="/mnt/pi-boot"
readonly ROOT_MOUNT="/mnt/pi-root"
readonly BACKUP_STAMP="$(date +%Y%m%d%H%M%S)"

declare -a TEMP_FILES=()
declare -a CHANGES=()
declare -a BACKUP_RECORDS=()
declare -A BACKED_UP=()

TARGET_DISK=""
BOOT_PART=""
ROOT_PART=""
BOOT_WAS_MOUNTED=0
ROOT_WAS_MOUNTED=0
WIFI_PASSWORD=""
WIFI_SSID=""
PI_PASSWORD=""
TAILSCALE_AUTH_KEY=""
LAST_TEMP_FILE=""
SCRIPT_DIR=""
ENV_FILE=""
ENV_FILE_WAS_USED=0

cleanup() {
    local status=$?
    local temporary
    set +e

    for temporary in "${TEMP_FILES[@]}"; do
        [[ -e "$temporary" ]] && rm -f -- "$temporary"
    done

    sync
    if (( ROOT_WAS_MOUNTED )); then
        if ! umount -- "$ROOT_MOUNT"; then
            printf 'ERROR: could not unmount %s; inspect it before removing the card.\n' "$ROOT_MOUNT" >&2
            status=1
        fi
    fi
    if (( BOOT_WAS_MOUNTED )); then
        if ! umount -- "$BOOT_MOUNT"; then
            printf 'ERROR: could not unmount %s; inspect it before removing the card.\n' "$BOOT_MOUNT" >&2
            status=1
        fi
    fi
    exit "$status"
}

trap cleanup EXIT

usage() {
    cat <<'USAGE'
Usage:
  sudo ./patch_hummer_pi_sd.sh [--device /dev/sdX] [--env-file /path/to/file]

The optional device argument must be a whole removable disk that contains
one small FAT boot partition and one larger ext4 Raspberry Pi root partition.
The script prints the detected target and requires an exact confirmation
before it mounts or modifies anything on the card.

If ./hummer_pi_sd.env exists, it is read as a simple KEY=VALUE file (never
executed as shell code). It must contain WIFI_SSID, WIFI_PASSWORD and
PI_PASSWORD; TAILSCALE_AUTH_KEY is optional. Otherwise the script prompts
securely.
USAGE
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

need_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

need_command lsblk
printf '%s\n' 'Read-only disk inventory (required initial inspection):'
lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINTS,MODEL
printf '\n'

for required_command in \
    lsblk findmnt mount umount mountpoint sync openssl mktemp cp cmp install \
    stat readlink awk sed date chmod chown mkdir rm tr head find ln dirname; do
    need_command "$required_command"
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ENV_FILE="$SCRIPT_DIR/hummer_pi_sd.env"

if (( EUID != 0 )); then
    die "run this script as root, for example: sudo $0"
fi

if [[ ! -t 0 || ! -t 1 ]]; then
    die "an interactive terminal is required for device, Wi-Fi, and password confirmation"
fi

REQUESTED_DEVICE=""
while (($# > 0)); do
    case "$1" in
        --device)
            (($# >= 2)) || die "--device requires a whole-disk path"
            REQUESTED_DEVICE="$2"
            shift 2
            ;;
        --env-file)
            (($# >= 2)) || die "--env-file requires a path"
            ENV_FILE="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            usage >&2
            die "unknown argument: $1"
            ;;
    esac
done

is_system_disk() {
    local candidate="$1"
    local system_source system_parent

    system_source="$(findmnt -no SOURCE / 2>/dev/null || true)"
    [[ "$system_source" == /dev/* ]] || return 1
    system_parent="$(lsblk -ndo PKNAME "$system_source" 2>/dev/null | head -n 1 || true)"
    [[ -n "$system_parent" && "/dev/$system_parent" == "$candidate" ]]
}

is_removable_transport() {
    local rm_flag="$1"
    local transport="${2,,}"
    [[ "$rm_flag" == "1" || "$transport" == "usb" || "$transport" == "mmc" || "$transport" == "sd" ]]
}

partition_field() {
    local field="$1"
    local partition="$2"
    case "$field" in
        FSTYPE) lsblk -dnro FSTYPE "$partition" 2>/dev/null | sed 's/[[:space:]]*$//' ;;
        LABEL) lsblk -dnro LABEL "$partition" 2>/dev/null | sed 's/[[:space:]]*$//' ;;
        SIZE) lsblk -bdnro SIZE "$partition" 2>/dev/null | tr -d '[:space:]' ;;
        *) die "internal error: unsupported partition field $field" ;;
    esac
}

discover_candidates() {
    local disk disk_type disk_rm disk_transport removable
    local partition partition_type fstype label label_lc size
    local boot_score boot_size root_size
    local disk_boot disk_root

    CANDIDATE_DISKS=()
    CANDIDATE_BOOTS=()
    CANDIDATE_ROOTS=()

    mapfile -t ALL_DISKS < <(lsblk -dnpo NAME,TYPE | awk '$2 == "disk" {print $1}')
    for disk in "${ALL_DISKS[@]}"; do
        [[ -b "$disk" ]] || continue
        disk_type="$(lsblk -dnro TYPE "$disk" 2>/dev/null | tr -d '[:space:]')"
        [[ "$disk_type" == "disk" ]] || continue
        disk_rm="$(lsblk -dnro RM "$disk" 2>/dev/null | tr -d '[:space:]')"
        disk_transport="$(lsblk -dnro TRAN "$disk" 2>/dev/null | tr -d '[:space:]')"
        removable=0
        if is_removable_transport "$disk_rm" "$disk_transport"; then
            removable=1
        fi

        if is_system_disk "$disk"; then
            continue
        fi

        disk_boot=""
        boot_score=0
        boot_size=0
        disk_root=""
        root_size=0

        mapfile -t DISK_PARTITIONS < <(
            lsblk -nrpo NAME,TYPE "$disk" | awk '$2 == "part" {print $1}'
        )
        for partition in "${DISK_PARTITIONS[@]}"; do
            [[ -b "$partition" ]] || continue
            partition_type="$(lsblk -dnro TYPE "$partition" 2>/dev/null | tr -d '[:space:]')"
            [[ "$partition_type" == "part" ]] || continue
            fstype="$(partition_field FSTYPE "$partition")"
            label="$(partition_field LABEL "$partition")"
            label_lc="${label,,}"
            size="$(partition_field SIZE "$partition")"
            [[ "$size" =~ ^[0-9]+$ ]] || size=0

            case "${fstype,,}" in
                vfat|fat|fat16|fat32)
                    local this_boot_score=0
                    if [[ "$label_lc" == "bootfs" ]]; then
                        this_boot_score=3
                    elif [[ "$label_lc" == "boot" ]]; then
                        this_boot_score=2
                    elif (( removable )) && (( size > 0 && size <= 2147483648 )); then
                        # A small FAT partition on a removable SD reader is a
                        # useful fallback when the boot label is absent.
                        this_boot_score=1
                    fi
                    if (( this_boot_score > boot_score )); then
                        disk_boot="$partition"
                        boot_score="$this_boot_score"
                        boot_size="$size"
                    fi
                    ;;
                ext4)
                    if (( size > root_size )); then
                        disk_root="$partition"
                        root_size="$size"
                    fi
                    ;;
            esac
        done

        if [[ -n "$disk_boot" && -n "$disk_root" && "$root_size" -gt "$boot_size" ]]; then
            CANDIDATE_DISKS+=("$disk")
            CANDIDATE_BOOTS+=("$disk_boot")
            CANDIDATE_ROOTS+=("$disk_root")
        fi
    done
}

discover_candidates

if [[ -n "$REQUESTED_DEVICE" ]]; then
    [[ -b "$REQUESTED_DEVICE" ]] || die "requested device is not a block device: $REQUESTED_DEVICE"
    REQUESTED_DEVICE="$(readlink -f -- "$REQUESTED_DEVICE")"
    [[ "$(lsblk -dnro TYPE "$REQUESTED_DEVICE" 2>/dev/null | tr -d '[:space:]')" == "disk" ]] \
        || die "--device must name a whole disk, not a partition: $REQUESTED_DEVICE"

    TARGET_INDEX=-1
    for index in "${!CANDIDATE_DISKS[@]}"; do
        if [[ "${CANDIDATE_DISKS[$index]}" == "$REQUESTED_DEVICE" ]]; then
            TARGET_INDEX="$index"
            break
        fi
    done
    (( TARGET_INDEX >= 0 )) || die "requested disk does not match a safe FAT+ext4 Raspberry Pi layout: $REQUESTED_DEVICE"
else
    ((${#CANDIDATE_DISKS[@]} > 0)) || die "no safe Raspberry Pi SD candidate found; insert/reconnect the card and rerun"
    ((${#CANDIDATE_DISKS[@]} == 1)) || {
        printf '%s\n' 'Multiple possible Raspberry Pi SD cards were found:' >&2
        for index in "${!CANDIDATE_DISKS[@]}"; do
            printf '  %s  boot=%s  root=%s\n' \
                "${CANDIDATE_DISKS[$index]}" "${CANDIDATE_BOOTS[$index]}" "${CANDIDATE_ROOTS[$index]}" >&2
        done
        die 'rerun with --device /dev/... after selecting the intended card'
    }
    TARGET_INDEX=0
fi

TARGET_DISK="${CANDIDATE_DISKS[$TARGET_INDEX]}"
BOOT_PART="${CANDIDATE_BOOTS[$TARGET_INDEX]}"
ROOT_PART="${CANDIDATE_ROOTS[$TARGET_INDEX]}"

printf '%s\n' 'Proposed target (no SD-card changes have been made):'
printf '  Whole disk: %s\n  Boot:       %s\n  Root:       %s\n\n' "$TARGET_DISK" "$BOOT_PART" "$ROOT_PART"
lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINTS,MODEL "$TARGET_DISK"
printf '\n'
printf 'Type exactly PATCH %s to authorize mounting and patching this device: ' "$TARGET_DISK"
read -r TARGET_CONFIRMATION
[[ "$TARGET_CONFIRMATION" == "PATCH $TARGET_DISK" ]] || die 'target was not confirmed; nothing was changed'

for partition in "$BOOT_PART" "$ROOT_PART"; do
    existing_mounts="$(findmnt -rn -S "$partition" -o TARGET 2>/dev/null || true)"
    [[ -z "$existing_mounts" ]] || die "$partition is already mounted at: $existing_mounts; unmount it and rerun"
done

for mount_dir in "$BOOT_MOUNT" "$ROOT_MOUNT"; do
    if mountpoint -q "$mount_dir"; then
        die "$mount_dir is already a mount point; unmount it and rerun"
    fi
    if [[ -e "$mount_dir" && ! -d "$mount_dir" ]]; then
        die "$mount_dir exists but is not a directory"
    fi
    if [[ -d "$mount_dir" ]] && [[ -n "$(find "$mount_dir" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
        die "$mount_dir exists and is not empty; refusing to hide its contents"
    fi
done

validate_config_values() {
    [[ -n "$WIFI_SSID" ]] || die 'WIFI_SSID is empty'
    if [[ "$WIFI_SSID" == *$'\n'* || "$WIFI_SSID" == *$'\r'* || "$WIFI_SSID" == *$'\t'* ]]; then
        die 'WIFI_SSID may not contain control characters'
    fi
    if ((${#WIFI_SSID} > 32)); then
        die 'WIFI_SSID may not exceed 32 characters'
    fi
    [[ -n "$WIFI_PASSWORD" ]] || die 'WIFI_PASSWORD is empty'
    if [[ "$WIFI_PASSWORD" == *$'\r'* || "$WIFI_PASSWORD" == *$'\t'* ]]; then
        die 'Wi-Fi password may not contain carriage returns or tabs'
    fi
    if ((${#WIFI_PASSWORD} < 8 || ${#WIFI_PASSWORD} > 63)); then
        die 'Wi-Fi password must be 8 to 63 characters for WPA-PSK'
    fi
    [[ -n "$PI_PASSWORD" ]] || die 'PI_PASSWORD is empty'
    if [[ "$TAILSCALE_AUTH_KEY" == *[[:space:]]* ]]; then
        die 'TAILSCALE_AUTH_KEY may not contain whitespace'
    fi
}

load_env_file() {
    local line trimmed key value

    [[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || die "env file is not a regular file: $ENV_FILE"
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        trimmed="${line#"${line%%[![:space:]]*}"}"
        [[ -z "$trimmed" || "${trimmed:0:1}" == '#' ]] && continue
        [[ "$trimmed" == *=* ]] || die "invalid env-file line (expected KEY=VALUE): $ENV_FILE"
        key="${trimmed%%=*}"
        value="${trimmed#*=}"
        case "$key" in
            WIFI_SSID) WIFI_SSID="$value" ;;
            WIFI_PASSWORD) WIFI_PASSWORD="$value" ;;
            PI_PASSWORD) PI_PASSWORD="$value" ;;
            TAILSCALE_AUTH_KEY) TAILSCALE_AUTH_KEY="$value" ;;
            hostname) [[ "$value" == "$PI_HOSTNAME" ]] || die "hostname in $ENV_FILE must be $PI_HOSTNAME" ;;
            username) [[ "$value" == "$PI_USERNAME" ]] || die "username in $ENV_FILE must be $PI_USERNAME" ;;
            'Wi-Fi SSID') WIFI_SSID="$value" ;;
            'Wi-Fi country') [[ "$value" == "$WIFI_COUNTRY" ]] || die "Wi-Fi country in $ENV_FILE must be $WIFI_COUNTRY" ;;
            timezone) [[ "$value" == "$PI_TIMEZONE" ]] || die "timezone in $ENV_FILE must be $PI_TIMEZONE" ;;
            *) die "unsupported key '$key' in $ENV_FILE" ;;
        esac
    done < "$ENV_FILE"
}

if [[ -e "$ENV_FILE" || -L "$ENV_FILE" ]]; then
    ENV_FILE_WAS_USED=1
    load_env_file
    validate_config_values
    printf 'Using credentials from %s (values will not be displayed).\n' "$ENV_FILE"
else
    read -r -p 'Wi-Fi SSID: ' WIFI_SSID
    read -r -s -p "Wi-Fi password for $WIFI_SSID (input hidden): " WIFI_PASSWORD
    printf '\n'
    read -r -s -p 'Repeat Wi-Fi password (input hidden): ' WIFI_PASSWORD_CONFIRMATION
    printf '\n'
    [[ "$WIFI_PASSWORD" == "$WIFI_PASSWORD_CONFIRMATION" ]] || die 'Wi-Fi passwords did not match'
    unset WIFI_PASSWORD_CONFIRMATION

    read -r -s -p 'Initial password for jeremy (input hidden): ' PI_PASSWORD
    printf '\n'
    read -r -s -p 'Repeat initial jeremy password (input hidden): ' PI_PASSWORD_CONFIRMATION
    printf '\n'
    [[ "$PI_PASSWORD" == "$PI_PASSWORD_CONFIRMATION" ]] || die 'jeremy passwords did not match'
    unset PI_PASSWORD_CONFIRMATION

    read -r -s -p 'Optional Tailscale auth key (press Enter to skip; input hidden): ' TAILSCALE_AUTH_KEY
    printf '\n'
    validate_config_values
fi

if [[ ! -d "$BOOT_MOUNT" ]]; then
    mkdir -p -- "$BOOT_MOUNT"
fi
if [[ ! -d "$ROOT_MOUNT" ]]; then
    mkdir -p -- "$ROOT_MOUNT"
fi

mount -o rw -- "$BOOT_PART" "$BOOT_MOUNT"
BOOT_WAS_MOUNTED=1
mount -o rw -- "$ROOT_PART" "$ROOT_MOUNT"
ROOT_WAS_MOUNTED=1

[[ -d "$ROOT_MOUNT/etc" && -d "$ROOT_MOUNT/usr" ]] \
    || die "mounted root partition does not look like a Linux root filesystem"
if [[ ! -e "$BOOT_MOUNT/config.txt" && ! -e "$BOOT_MOUNT/firmware/config.txt" && ! -d "$BOOT_MOUNT/overlays" ]]; then
    die "mounted FAT partition does not look like a Raspberry Pi boot partition"
fi
[[ -f "$BOOT_MOUNT/cmdline.txt" && ! -L "$BOOT_MOUNT/cmdline.txt" ]] \
    || die "Raspberry Pi boot cmdline.txt is missing or is not a regular file"

temporary_file() {
    LAST_TEMP_FILE="$(mktemp /tmp/patch_hummer_pi_sd.XXXXXX)"
    TEMP_FILES+=("$LAST_TEMP_FILE")
}

record_change() {
    CHANGES+=("$1")
}

backup_before_modify() {
    local path="$1"
    local backup_path="$path.bak.$BACKUP_STAMP"
    local suffix=0

    [[ -e "$path" || -L "$path" ]] || return 0
    [[ -n "${BACKED_UP[$path]+present}" ]] && return 0

    while [[ -e "$backup_path" || -L "$backup_path" ]]; do
        suffix=$((suffix + 1))
        backup_path="$path.bak.$BACKUP_STAMP.$suffix"
    done
    cp -a -- "$path" "$backup_path"
    BACKED_UP["$path"]="$backup_path"
    BACKUP_RECORDS+=("$path -> $backup_path")
}

safe_replace_from_temp() {
    local destination="$1"
    local source="$2"
    local mode="$3"
    local filesystem_kind="$4"
    local destination_mode destination_owner

    if [[ -d "$destination" && ! -L "$destination" ]]; then
        die "refusing to replace directory: $destination"
    fi

    if [[ -f "$destination" && ! -L "$destination" ]] && cmp -s "$source" "$destination"; then
        if [[ "$filesystem_kind" != "posix" ]]; then
            return 0
        fi
        destination_mode="$(stat -c '%a' -- "$destination")"
        destination_owner="$(stat -c '%u:%g' -- "$destination")"
        if [[ "$destination_mode" == "$mode" && "$destination_owner" == "0:0" ]]; then
            return 0
        fi
    fi

    if [[ -e "$destination" || -L "$destination" ]]; then
        backup_before_modify "$destination"
        if [[ -L "$destination" ]]; then
            rm -f -- "$destination"
        fi
    fi

    if [[ "$filesystem_kind" == "posix" ]]; then
        install -o root -g root -m "$mode" -- "$source" "$destination"
    else
        # FAT does not reliably support Unix ownership/mode metadata. The
        # mount's ownership policy applies; chmod is only best effort.
        cp -- "$source" "$destination"
        chmod "$mode" -- "$destination" 2>/dev/null || true
    fi
    record_change "$destination"
}

safe_replace_symlink() {
    local destination="$1"
    local target="$2"

    if [[ -L "$destination" ]] && [[ "$(readlink -- "$destination")" == "$target" ]]; then
        return 0
    fi
    if [[ -d "$destination" && ! -L "$destination" ]]; then
        die "refusing to replace directory with symlink: $destination"
    fi
    if [[ -e "$destination" || -L "$destination" ]]; then
        backup_before_modify "$destination"
        rm -f -- "$destination"
    fi
    ln -s -- "$target" "$destination"
    record_change "$destination -> $target"
}

ensure_directory() {
    local directory="$1"
    if [[ -e "$directory" || -L "$directory" ]]; then
        [[ -d "$directory" && ! -L "$directory" ]] || die "expected directory but found another file type: $directory"
        return 0
    fi
    mkdir -p -- "$directory"
}

write_literal_file() {
    local destination="$1"
    local mode="$2"
    local filesystem_kind="$3"
    local contents="$4"
    local temporary

    temporary_file
    temporary="$LAST_TEMP_FILE"
    printf '%s' "$contents" > "$temporary"
    safe_replace_from_temp "$destination" "$temporary" "$mode" "$filesystem_kind"
}

write_hosts_file() {
    local destination="$ROOT_MOUNT/etc/hosts"
    local temporary

    if [[ -L "$destination" ]]; then
        die "refusing to read or edit symlinked hosts file: $destination"
    fi
    temporary_file
    temporary="$LAST_TEMP_FILE"
    if [[ -e "$destination" ]]; then
        [[ -f "$destination" ]] || die "expected regular hosts file: $destination"
        awk -v hostname="$PI_HOSTNAME" '
            BEGIN { replaced = 0 }
            $1 == "127.0.1.1" {
                if (!replaced) {
                    print "127.0.1.1 " hostname
                    replaced = 1
                }
                next
            }
            { print }
            END {
                if (!replaced) print "127.0.1.1 " hostname
            }
        ' "$destination" > "$temporary"
    else
        printf '127.0.0.1 localhost\n127.0.1.1 %s\n' "$PI_HOSTNAME" > "$temporary"
    fi
    safe_replace_from_temp "$destination" "$temporary" 644 posix
}

write_userconf_file() {
    local destination="$BOOT_MOUNT/userconf.txt"
    local temporary hash

    if [[ -L "$destination" ]]; then
        die "refusing to read or edit symlinked userconf.txt: $destination"
    fi
    temporary_file
    temporary="$LAST_TEMP_FILE"
    hash="$(printf '%s' "$PI_PASSWORD" | openssl passwd -6 -stdin)"
    [[ "$hash" == '$6$'* ]] || die 'openssl did not produce a SHA-512 password hash'

    if [[ -e "$destination" ]]; then
        [[ -f "$destination" ]] || die "expected regular userconf.txt: $destination"
        # Keep any other bootstrap entries in the backup-derived file, but
        # put jeremy first so images that consume only the first line still
        # create the requested account.
        printf '%s:%s\n' "$PI_USERNAME" "$hash" > "$temporary"
        awk -F: -v username="$PI_USERNAME" '$1 != username { print }' "$destination" >> "$temporary"
    else
        printf '%s:%s\n' "$PI_USERNAME" "$hash" >> "$temporary"
    fi
    safe_replace_from_temp "$destination" "$temporary" 600 boot
    unset hash
}

write_wifi_file() {
    local destination="$ROOT_MOUNT/etc/NetworkManager/system-connections/hummer-wifi.nmconnection"
    local temporary escaped_password

    # A literal backslash has escape meaning in NetworkManager keyfiles.
    escaped_password="${WIFI_PASSWORD//\\/\\\\}"
    temporary_file
    temporary="$LAST_TEMP_FILE"
    printf '%s\n' \
        '[connection]' \
        "id=$WIFI_SSID" \
        'type=wifi' \
        'interface-name=wlan0' \
        'autoconnect=true' \
        '' \
        '[wifi]' \
        'mode=infrastructure' \
        "ssid=$WIFI_SSID" \
        '' \
        '[wifi-security]' \
        'key-mgmt=wpa-psk' \
        "psk=$escaped_password" \
        '' \
        '[ipv4]' \
        'method=auto' \
        '' \
        '[ipv6]' \
        'method=auto' > "$temporary"
    safe_replace_from_temp "$destination" "$temporary" 600 posix
}

write_wifi_country_cmdline() {
    local destination="$BOOT_MOUNT/cmdline.txt"
    local temporary

    [[ -e "$destination" ]] || die "Raspberry Pi boot cmdline.txt is missing; cannot set WLAN country safely"
    [[ -f "$destination" && ! -L "$destination" ]] || die "expected regular cmdline.txt: $destination"
    temporary_file
    temporary="$LAST_TEMP_FILE"
    awk -v country="$WIFI_COUNTRY" '
        {
            gsub(/(^|[[:space:]])cfg80211\.ieee80211_regdom=[^[:space:]]+/, "")
            sub(/[[:space:]]+$/, "")
            print $0 " cfg80211.ieee80211_regdom=" country
        }
    ' "$destination" > "$temporary"
    safe_replace_from_temp "$destination" "$temporary" 644 boot
}

write_ssh_password_config() {
    local destination="$ROOT_MOUNT/etc/ssh/sshd_config.d/00-hummer-password-login.conf"
    local temporary

    temporary_file
    temporary="$LAST_TEMP_FILE"
    printf '%s\n' \
        '# Hummer initial headless setup: permit the password created by userconf.txt.' \
        'PasswordAuthentication yes' \
        'KbdInteractiveAuthentication yes' > "$temporary"
    safe_replace_from_temp "$destination" "$temporary" 644 posix
}

write_firstboot_script() {
    local destination="$ROOT_MOUNT/usr/local/sbin/hummer-firstboot.sh"
    local temporary

    temporary_file
    temporary="$LAST_TEMP_FILE"
    cat <<'FIRSTBOOT_SCRIPT' > "$temporary"
#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

LOG_FILE=/var/log/hummer-firstboot.log
DONE_MARKER=/var/lib/hummer-firstboot.done
SERVICE_NAME=hummer-firstboot.service

mkdir -p /var/log
touch "$LOG_FILE"
chmod 0600 "$LOG_FILE" || true
exec >>"$LOG_FILE" 2>&1

log() {
    printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*"
}

on_exit() {
    local status=$?
    if (( status == 0 )); then
        log 'Hummer first-boot setup completed successfully.'
    else
        log "Hummer first-boot setup failed with exit status $status. It remains enabled for retry."
    fi
    exit "$status"
}
trap on_exit EXIT

if [[ -e "$DONE_MARKER" ]]; then
    log 'Completion marker already exists; ensuring the first-boot service is disabled.'
    systemctl disable "$SERVICE_NAME" || true
    exit 0
fi

wait_for_network() {
    local attempt
    for attempt in $(seq 1 60); do
        if command -v nm-online >/dev/null 2>&1 && nm-online -q --timeout=10; then
            log 'NetworkManager reports the network is online.'
            return 0
        fi
        if command -v curl >/dev/null 2>&1 && \
            curl -fsS --connect-timeout 5 --max-time 10 https://deb.debian.org/ >/dev/null; then
            log 'Internet connectivity is available.'
            return 0
        fi
        if command -v ip >/dev/null 2>&1 && command -v getent >/dev/null 2>&1 && \
            ip route get 1.1.1.1 >/dev/null 2>&1 && getent hosts deb.debian.org >/dev/null 2>&1; then
            log 'A route and DNS are available.'
            return 0
        fi
        log "Waiting for networking (attempt $attempt/60)."
        sleep 5
    done
    log 'Timed out waiting for networking.'
    return 1
}

log 'Starting Hummer first-boot setup.'
wait_for_network

log 'Updating APT package indexes.'
sudo apt-get update
log 'Installing required OBD-II, Bluetooth, utility, and Tailscale prerequisites.'
sudo apt-get install -y \
    curl ca-certificates git jq vim htop sqlite3 python3 python3-venv \
    python3-pip python3-serial bluetooth bluez pi-bluetooth rfkill \
    mosquitto-clients

log 'Unblocking Wi-Fi and Bluetooth.'
rfkill unblock all

log 'Enabling SSH.'
systemctl enable --now ssh.service
log 'Enabling Bluetooth.'
systemctl enable --now bluetooth.service
if systemctl cat hciuart.service >/dev/null 2>&1; then
    log 'Enabling the Raspberry Pi Bluetooth UART service.'
    systemctl enable --now hciuart.service
fi

log 'Installing Tailscale using the official installer.'
curl -fsSL https://tailscale.com/install.sh | sh
command -v tailscale >/dev/null 2>&1
if systemctl cat tailscaled.service >/dev/null 2>&1; then
    systemctl enable --now tailscaled.service
fi

AUTH_KEY=''
AUTH_FILE=''
for candidate in /boot/firmware/tailscale-auth-key.txt /boot/tailscale-auth-key.txt; do
    [[ -f "$candidate" ]] || continue
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -n "$line" && "$line" != \#* ]] || continue
        case "${line,,}" in
            *placeholder*|*replace*|*insert*|*your*|none|skip)
                continue
                ;;
        esac
        AUTH_KEY="$line"
        AUTH_FILE="$candidate"
        break 2
    done < "$candidate"
done

if [[ -n "$AUTH_KEY" ]]; then
    log "Enrolling Tailscale using the key from $AUTH_FILE."
    tailscale up --ssh --hostname=hummer --auth-key "$AUTH_KEY"
else
    log 'No usable Tailscale auth key was supplied; leaving Tailscale installed for manual login.'
fi
unset AUTH_KEY AUTH_FILE

install -d -m 0755 /var/lib
printf 'completed=%s\n' "$(date --iso-8601=seconds)" > "$DONE_MARKER"
chmod 0600 "$DONE_MARKER"
systemctl disable "$SERVICE_NAME"
log 'Disabled the first-boot service after successful setup.'
FIRSTBOOT_SCRIPT
    safe_replace_from_temp "$destination" "$temporary" 755 posix
}

write_systemd_unit() {
    local destination="$ROOT_MOUNT/etc/systemd/system/hummer-firstboot.service"
    local temporary

    temporary_file
    temporary="$LAST_TEMP_FILE"
    cat <<'FIRSTBOOT_UNIT' > "$temporary"
[Unit]
Description=Hummer Raspberry Pi first-boot setup
Wants=network-online.target
After=network-online.target
ConditionPathExists=/usr/local/sbin/hummer-firstboot.sh

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/hummer-firstboot.sh
RemainAfterExit=yes
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
FIRSTBOOT_UNIT
    safe_replace_from_temp "$destination" "$temporary" 644 posix
}

write_boot_readme() {
    local destination="$BOOT_MOUNT/HUMMER_FIRST_BOOT_README.txt"
    local temporary

    temporary_file
    temporary="$LAST_TEMP_FILE"
    cat <<'README' > "$temporary"
Hummer Raspberry Pi first-boot setup

Hostname: hummer

Try SSH: ssh jeremy@hummer.local
If mDNS does not resolve, check the router client list and SSH to the Pi's IP address.

Tailscale is installed on first boot. It will auto-enroll only when this boot partition file contains a valid auth key:
tailscale-auth-key.txt
Without a key, log in manually with Tailscale after SSH access works.

The first boot may take 5–15 minutes while packages and Tailscale are installed.
README
    safe_replace_from_temp "$destination" "$temporary" 644 boot
}

existing_tailscale_key_is_usable() {
    local destination="$BOOT_MOUNT/tailscale-auth-key.txt"
    local line

    [[ -f "$destination" && ! -L "$destination" ]] || return 1
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -n "$line" && "$line" != \#* ]] || continue
        case "${line,,}" in
            *placeholder*|*replace*|*insert*|*your*|none|skip)
                continue
                ;;
        esac
        return 0
    done < "$destination"
    return 1
}

write_tailscale_key_file() {
    local destination="$BOOT_MOUNT/tailscale-auth-key.txt"
    local temporary

    [[ ! -L "$destination" ]] || die "refusing to read or edit symlinked tailscale-auth-key.txt: $destination"
    temporary_file
    temporary="$LAST_TEMP_FILE"
    if [[ -n "$TAILSCALE_AUTH_KEY" ]]; then
        printf '%s\n%s\n' \
            '# Tailscale auth key used on first boot.' \
            "$TAILSCALE_AUTH_KEY" > "$temporary"
        safe_replace_from_temp "$destination" "$temporary" 600 boot
    elif [[ -e "$destination" || -L "$destination" ]] && existing_tailscale_key_is_usable; then
        # Do not erase a previously supplied key merely because this rerun
        # chose the blank prompt. It remains protected by its existing file.
        return 0
    else
        cat <<'TAILSCALE_PLACEHOLDER' > "$temporary"
# Optional: replace this comment with a valid Tailscale auth key before booting.
# Leave this file as comments for manual Tailscale login later.
TAILSCALE_PLACEHOLDER
        safe_replace_from_temp "$destination" "$temporary" 644 boot
    fi
}

printf '%s\n' 'Applying the requested offline configuration...'

# Boot partition files.
write_literal_file "$BOOT_MOUNT/ssh" 644 boot ''
write_wifi_country_cmdline
write_userconf_file
write_tailscale_key_file
write_boot_readme

# Root filesystem files.
ensure_directory "$ROOT_MOUNT/etc/NetworkManager/system-connections"
ensure_directory "$ROOT_MOUNT/usr/local/sbin"
ensure_directory "$ROOT_MOUNT/etc/ssh/sshd_config.d"
ensure_directory "$ROOT_MOUNT/etc/systemd/system"
ensure_directory "$ROOT_MOUNT/etc/systemd/system/multi-user.target.wants"
write_literal_file "$ROOT_MOUNT/etc/hostname" 644 posix $'hummer\n'
write_hosts_file
write_wifi_file
write_ssh_password_config
write_literal_file "$ROOT_MOUNT/etc/timezone" 644 posix $'America/Phoenix\n'
safe_replace_symlink "$ROOT_MOUNT/etc/localtime" "/usr/share/zoneinfo/$PI_TIMEZONE"

write_firstboot_script
write_systemd_unit
safe_replace_symlink \
    "$ROOT_MOUNT/etc/systemd/system/multi-user.target.wants/hummer-firstboot.service" \
    "/etc/systemd/system/hummer-firstboot.service"

unset WIFI_PASSWORD PI_PASSWORD TAILSCALE_AUTH_KEY
sync

printf '\n%s\n' 'Patch completed; cleanup will now sync and unmount both SD partitions.'
printf 'Changed paths (%d):\n' "${#CHANGES[@]}"
for changed_path in "${CHANGES[@]}"; do
    printf '  %s\n' "$changed_path"
done
if ((${#BACKUP_RECORDS[@]} > 0)); then
    printf 'Backups created (%d):\n' "${#BACKUP_RECORDS[@]}"
    for backup_record in "${BACKUP_RECORDS[@]}"; do
        printf '  %s\n' "$backup_record"
    done
else
    printf '%s\n' 'Backups created: none (no existing file needed replacement).'
fi

printf '\nExpected first-boot access:\n'
printf '  ssh jeremy@hummer.local\n'
printf '  If Tailscale enrolls: tailscale status\n'

exit 0
