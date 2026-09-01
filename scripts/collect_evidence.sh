#!/bin/bash
# Collect a full state snapshot from the Pi for the deployment record.
# Read-only: it inspects, it never changes anything.  Secrets are never
# printed (no Wi-Fi PSKs, no keys, no VINs).
export PATH="$PATH:/usr/sbin:/sbin"
echo "### collected $(date -Is) on $(hostname)"
echo "### os/kernel";      cat /etc/os-release | head -2; uname -r; cat /proc/device-tree/model 2>/dev/null; echo
echo "### uptime/load";    uptime; cat /proc/loadavg
echo "### memory";         free -m
echo "### storage";        df -h / /boot/firmware
echo "### temperature";    vcgencmd measure_temp 2>/dev/null; vcgencmd get_throttled 2>/dev/null
echo "### default target"; systemctl get-default
echo "### package count";  dpkg-query -f '${binary:Package}\n' -W | wc -l
echo "### dpkg audit";     dpkg --audit 2>&1 | head -5 || true
echo "### key services"
for s in ssh NetworkManager tailscaled bluetooth avahi-daemon hummer-display hummer-collector hummer-rfcomm; do
    printf '%-22s %-12s %s\n' "$s" "$(systemctl is-enabled "$s" 2>&1)" "$(systemctl is-active "$s" 2>&1)"
done
echo "### failed units";   systemctl --failed --no-pager --no-legend
echo "### running services"; systemctl list-units --type=service --state=running --no-pager --no-legend | wc -l
echo "### memory top";     ps -eo rss,comm --sort=-rss | head -12
echo "### network";        ip -br a; ip route | head -3
echo "### wifi profiles";  nmcli -t -f NAME,TYPE,AUTOCONNECT,AUTOCONNECT-PRIORITY,DEVICE,ACTIVE connection show
echo "### wifi link";      nmcli -t -f ACTIVE,SSID,SIGNAL device wifi | grep '^yes' || true
echo "### dns";            cat /etc/resolv.conf | grep -v '^#'; getent hosts deb.debian.org >/dev/null && echo "resolution OK" || echo "resolution FAILED"
echo "### tailscale";      tailscale ip -4 2>/dev/null; tailscale status --self --peers=false 2>/dev/null | head -2
echo "### rfkill";         rfkill list
echo "### bluetooth";      bluetoothctl --timeout 3 show 2>/dev/null | grep -E 'Controller|Powered|Discoverable|Pairable'
echo "### paired devices"; bluetoothctl --timeout 3 devices Paired 2>/dev/null || true
echo "### rfcomm";         rfcomm -a 2>/dev/null; ls -l /dev/rfcomm0 2>&1 | head -2
echo "### spi";            ls -l /dev/spidev* 2>&1; lsmod | grep -c spi
echo "### python deps";    for m in serial spidev RPi.GPIO gpiozero PIL numpy; do python3 -c "import $m" 2>/dev/null && echo "  $m OK" || echo "  $m MISSING"; done
echo "### waveshare driver"; PYTHONPATH=/home/jeremy/hummer-obd/vendor/waveshare python3 -c "from waveshare_epd import epd2in13_V4; print('  epd2in13_V4 OK')" 2>&1 | tail -1
echo "### project";        ls /home/jeremy/hummer-obd 2>/dev/null; echo "  raw logs: $(ls /home/jeremy/hummer-obd/logs/raw 2>/dev/null | wc -l)"
