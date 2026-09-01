# Operations runbook

This runbook covers an already-built node. For first installation, use
[Build and deploy](BUILD_AND_DEPLOY.md). Commands assume the reference account
and path; override them for a different installation.

```bash
export PI_HOST="${PI_HOST:-jeremy@hummer.local}"
export HUMMER_ROOT="${HUMMER_ROOT:-/home/jeremy/hummer-obd}"
```

Never paste passwords, Wi-Fi keys, a VIN, or raw diagnostic responses into an
issue, commit, or public terminal transcript.

## Access and baseline health

```bash
ssh "$PI_HOST"

hostname
date
uptime
df -h /
free -h
ip -brief address
rfkill list
systemctl --failed --no-pager
```

Key-based SSH is preferred. If `hummer.local` does not resolve, use the Pi's
current LAN address or a private VPN name. The public repository deliberately
does not record either one.

## Expected service state

| Unit | Expected state | Purpose |
|---|---|---|
| `hummer-display.service` | enabled, active | e-paper node status |
| `hummer-rfcomm.service` | enabled, active while adapter is reachable | persistent `/dev/rfcomm0` bind |
| `hummer-btdiscover.service` | disabled, inactive after a healthy bind | one-shot recovery for an existing bond |
| `hummer-collector.service` | disabled, inactive pending power validation | continuous read-only telemetry |

Check all four without changing anything:

```bash
for unit in hummer-display hummer-rfcomm hummer-btdiscover hummer-collector; do
  printf '%-22s enabled=%-9s active=%s\n' \
    "$unit" \
    "$(systemctl is-enabled "$unit" 2>/dev/null || true)" \
    "$(systemctl is-active "$unit" 2>/dev/null || true)"
done

systemctl --failed --no-pager
```

## Network profiles

The Pi Zero 2 W supports only 2.4 GHz Wi-Fi. Keep the mobile hotspot profile at
a higher NetworkManager autoconnect priority than the stationary fallback:

```bash
nmcli -f NAME,AUTOCONNECT,AUTOCONNECT-PRIORITY connection show
nmcli -t -f ACTIVE,SSID,SIGNAL device wifi list
```

Autoconnect priority is consulted when NetworkManager chooses a connection; it
does not interrupt an already healthy connection. To deliberately switch to a
saved profile, use the guarded helper. It verifies that the target SSID is in
range and restores the previous profile if address, DNS, or Internet checks
fail:

```bash
read -r -p 'Saved hotspot profile: ' HOTSPOT_PROFILE
sudo setsid "$HUMMER_ROOT/scripts/switch_wifi_profile.sh" \
  "$HOTSPOT_PROFILE" "$HUMMER_ROOT/logs/wifi-switch.log" </dev/null &

# Reconnect over the new address/private VPN, then inspect:
tail -n 80 "$HUMMER_ROOT/logs/wifi-switch.log"
```

The log omits Wi-Fi keys.

## Bluetooth and RFCOMM

### Inspect the bond and binding

Read the deployed adapter address without publishing it:

```bash
OBD_MAC="$(sudo sed -n 's/^ADAPTER_MAC=//p' /etc/default/hummer-rfcomm)"
bluetoothctl devices Paired
bluetoothctl info "$OBD_MAC"
sdptool browse "$OBD_MAC"
rfcomm
ls -l /dev/rfcomm0
```

Healthy adapter state is `Paired: yes`, `Bonded: yes`, and `Trusted: yes`. SDP
must show exactly one **Serial Port** record; on the validated adapter it is
named `STN-SPP` and uses RFCOMM channel 1. An iAP record is not SPP and must not
be selected.

### First pairing or a replaced adapter

First pairing requires physical access:

1. disconnect any phone diagnostic app;
2. plug the OBDLink into the vehicle and press its pairing button;
3. run the scan and require one unambiguous OBDLink candidate; and
4. answer `yes` to the six-digit BlueZ confirmation.

```bash
cd "$HUMMER_ROOT"
sudo scripts/pair_obdlink.sh scan
read -r -p 'OBDLink MAC: ' OBD_MAC
sudo scripts/pair_obdlink.sh pair "$OBD_MAC"
sudo scripts/pair_obdlink.sh sdp "$OBD_MAC"
read -r -p 'Confirmed SPP channel: ' SPP_CHANNEL
sudo scripts/pair_obdlink.sh bind "$OBD_MAC" "$SPP_CHANNEL"
sudo systemctl enable --now hummer-rfcomm.service
```

The reference MX+ rejects unattended `NoInputNoOutput` pairing. The recovery
service cannot work around that and intentionally does not try.

### Recover a missing binding

If the bond is healthy but `/dev/rfcomm0` is absent:

```bash
sudo systemctl restart hummer-rfcomm.service
systemctl status hummer-rfcomm.service --no-pager
rfcomm
ls -l /dev/rfcomm0
```

If the known adapter/channel configuration is missing but BlueZ still has a
healthy bond, run the fail-closed recovery unit:

```bash
sudo systemctl enable --now hummer-btdiscover.service
journalctl -u hummer-btdiscover.service -n 100 --no-pager
```

It reads only known BlueZ devices, selects exactly one bonded/trusted OBDLink,
requires exactly one SPP channel, installs the binding, verifies
`/dev/rfcomm0`, and exits. Disable it again after recovery:

```bash
sudo systemctl disable --now hummer-btdiscover.service
```

It never opens the serial device, pairs/trusts/removes a device, runs a probe,
enables the collector, or sends a vehicle command.

## Raw probe and transcript review

The initial probe is a supervised acceptance action, not a health-check loop.
Do not rerun it merely because a service restarted.

```bash
cd "$HUMMER_ROOT"
PYTHONPATH=src python3 -m hummer_obd.probe \
  --device /dev/rfcomm0 \
  --config config/hummer.toml \
  --root . \
  --summary evidence/probe-summary.json
```

Review without touching the adapter:

```bash
PYTHONPATH=src python3 scripts/review_raw_log.py logs/raw/probe-*.jsonl
```

The reviewer pairs requests with responses, verifies each record's hex/base64
agreement, decodes adapter/protocol/PID/DTC/Mode 09 results, masks the VIN, and
rechecks every request through the safety gate.

Do not copy the raw JSONL off the Pi unless the destination is private. A raw
Mode 09 response can contain the full VIN.

## Collector

### One supervised cycle

After a probe passes review:

```bash
cd "$HUMMER_ROOT"
PYTHONPATH=src python3 -m hummer_obd.collector \
  --config config/hummer.toml --root . --once --force
```

Inspect the result:

```bash
sqlite3 data/hummer_obd.sqlite3 \
  'SELECT ts,pid,name,value,unit,status FROM samples ORDER BY id DESC LIMIT 10;'
sqlite3 data/hummer_obd.sqlite3 \
  'SELECT ts,mode,codes FROM dtc_reads ORDER BY id DESC LIMIT 10;'
```

### Continuous operation gate

The reference deployment keeps continuous polling off even though the one-shot
path works. A short polling loop can keep vehicle modules awake and drain the
12 V battery.

Do not enable the service until either:

- a full vehicle sleep/wake cycle has been observed with the Pi and adapter
  attached and sleep current remains acceptable; or
- the Pi is confirmed to use ignition-switched power.

After that physical validation, enable it deliberately:

```bash
sed -i '/^\[collector\]/,/^\[/ s/^enabled = false/enabled = true/' \
  "$HUMMER_ROOT/config/hummer.toml"
sudo systemctl enable --now hummer-collector.service
systemctl status hummer-collector.service --no-pager
journalctl -u hummer-collector.service -n 100 --no-pager
```

Stop and disable it immediately if sleep behavior or 12 V draw is uncertain:

```bash
sudo systemctl disable --now hummer-collector.service
```

## Display

Check the service and its last updates:

```bash
systemctl status hummer-display.service --no-pager
journalctl -u hummer-display.service -n 50 --no-pager
```

Render without touching the panel:

```bash
cd "$HUMMER_ROOT"
PYTHONPATH=src python3 -m hummer_obd.display.status \
  --once --simulate /tmp/hummer-status.png
```

Perform one physical refresh or clear:

```bash
PYTHONPATH=src:vendor/waveshare python3 -m hummer_obd.display.status --once
PYTHONPATH=src:vendor/waveshare python3 -m hummer_obd.display.status --clear
```

An unchanged panel is not necessarily stale: identical frames are skipped.
Only full refreshes are used, with a minimum interval, and the panel sleeps
between updates.

## Sleeping vehicle behavior

`NO DATA` or `UNABLE TO CONNECT` across the approved PID set normally means
the vehicle is asleep. This is a wait condition, not a request to probe harder.
The collector backs off and does not send extra traffic to wake the bus.

If the adapter itself is absent, distinguish that transport failure from a
sleeping vehicle before changing anything:

```bash
test -e /dev/rfcomm0 && echo bound || echo not-bound
rfcomm
OBD_MAC="$(sudo sed -n 's/^ADAPTER_MAC=//p' /etc/default/hummer-rfcomm)"
bluetoothctl info "$OBD_MAC" | grep -E 'Connected|Paired|Bonded|Trusted'
```

## Data retention and backup

Private runtime data lives under:

```text
/home/jeremy/hummer-obd/logs/raw/           byte-exact JSONL transcripts
/home/jeremy/hummer-obd/data/               SQLite database and WAL files
/home/jeremy/hummer-obd/config/hummer.toml  deployed local configuration
/etc/default/hummer-rfcomm                   private adapter address/channel
```

Stop the collector before taking a consistent manual SQLite copy:

```bash
sudo systemctl stop hummer-collector.service
sqlite3 "$HUMMER_ROOT/data/hummer_obd.sqlite3" ".backup '$HUMMER_ROOT/data/backup.sqlite3'"
```

Store backups privately. Never commit these paths.

## Upgrades

From a trusted workstation checkout:

```bash
git pull --ff-only
python -m pytest -q
HOST="$PI_HOST" scripts/deploy.sh
```

On the Pi:

```bash
cd "$HUMMER_ROOT"
./scripts/pi_smoke.sh
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart hummer-display.service hummer-rfcomm.service
```

Do not restart or enable the collector as an incidental part of a deployment.

## Troubleshooting

### Pi appears online but SSH times out

A Pi Zero 2 W can become effectively unreachable during a large package purge
because SD-card I/O and thermal throttling starve interactive services while a
VPN control connection still appears online. Before power-cycling:

1. confirm whether `apt`, `dpkg`, or a filesystem job is expected;
2. allow a long-running package operation to finish;
3. try the LAN address as well as private VPN access; and
4. only power-cycle when no package/database write is in progress.

Before removing packages, simulate both the explicit purge and autoremove and
verify that NetworkManager, wpa_supplicant, firmware, SSH, Tailscale (if used),
BlueZ, Avahi, SPI/GPIO, and Python runtime dependencies are not selected.

### DNS fails only while a private VPN is active

Inspect `/etc/resolv.conf`, NetworkManager DNS state, and VPN DNS settings. A
private DNS proxy with no usable upstream can break package downloads even
while private peers remain reachable. Fix DNS policy; do not remove networking
packages.

### Every OBD request says `NO DATA`

- confirm `/dev/rfcomm0` exists;
- disconnect phone apps from the OBDLink;
- ensure the vehicle/key is present and the vehicle is awake;
- wait rather than increasing polling frequency; and
- do not send unapproved commands to “test” the bus.

### The display is blank

```bash
ls -l /dev/spidev0.0
lsmod | grep spi_bcm2835
journalctl -u hummer-display.service -n 100 --no-pager
cd "$HUMMER_ROOT"
PYTHONPATH=src python3 -m hummer_obd.display.status \
  --once --simulate /tmp/hummer-status.png
```

If simulation works, check the V4 driver import, SPI/GPIO permissions, HAT
seating, and panel cable before changing renderer code.
