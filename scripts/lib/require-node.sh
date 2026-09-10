# Refuse to configure the wrong machine.
#
# Sourced by every script that changes system state belonging to the vehicle
# node: systemd units, /etc/sudoers.d, /etc/default, kernel modules, Wi-Fi
# profiles. Those scripts call sudo directly, which means they configure
# whichever machine invokes them -- and they are kept and edited on a
# workstation, which is exactly where someone will run them by mistake.
#
# This is not hypothetical. allow-places-write.sh was run from the workstation,
# wrote a systemd drop-in into the workstation's /etc, and failed afterwards
# with "Unit hummer-dashboard.service not found": litter on the wrong machine,
# nothing fixed on the right one. The blast radius of the others is worse --
# switch_wifi_profile.sh would reconfigure the workstation's Wi-Fi, and
# bt-recover.sh would unload its Bluetooth driver.
#
# A comment saying "run this on the Pi" is not a guard. This is.
#
# The test is /proc/device-tree/model, not the hostname: an ordinary x86 box
# has no device tree at all, so the check fails closed on anything that is not
# a Pi, and it keeps working if the node is ever renamed.

require_node() {
    local model=""
    if [ -r /proc/device-tree/model ]; then
        model=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)
    fi

    case "$model" in
        *"Raspberry Pi"*) return 0 ;;
    esac

    cat >&2 <<EOF
error: this script configures the vehicle node, and this is not it.

  running on : $(hostname) ${model:+($model)}
  expected   : the Raspberry Pi node (hummer)

Nothing has been changed. Get onto the node and run it there:

    ssh jeremy@100.71.118.116
    cd /home/jeremy/hummer-obd && bash $(basename "${BASH_SOURCE[1]:-this script}")

If the node does not have the current code yet, deploy first:

    HOST=jeremy@100.71.118.116 scripts/deploy.sh

Set HUMMER_I_AM_THE_NODE=1 to override, only if you know why that is right.
EOF
    exit 1
}

# Deliberate escape hatch, named so it cannot be typed by accident and so it
# shows up in a grep of anything that bypassed the check.
if [ "${HUMMER_I_AM_THE_NODE:-}" = "1" ]; then
    require_node() { :; }
fi
