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

## Running a bounded collector trial

A trial is the supervised step between one `--once` cycle and the always-on
collector. Run it through systemd, **never** as a background process over SSH.

The reason is concrete. A trial was once started with `nohup` over SSH, and a
later command meant to restart it with a wider PID set was cut off after
stopping the old process and before the new one detached. Local logging was
never at risk -- every sample is fsync'd to the node's own disk and nothing in
the collector path touches the network -- but nothing was left to restart the
process. Supervision, not storage, was the gap.

```bash
# 1. Write a SEPARATE trial config.  Never edit config/hummer.toml for a trial.
sudoedit /etc/default/hummer-collector-trial     # TRIAL_CONFIG, duration, interval

# 2. Start one bounded run.  It survives your SSH session closing.
sudo systemctl start hummer-collector-trial

# 3. Watch it without touching the vehicle.
systemctl status hummer-collector-trial
journalctl -u hummer-collector-trial -f

# 4. It stops itself at TRIAL_DURATION_S.  To end it early:
sudo systemctl stop hummer-collector-trial
```

What the unit guarantees:

| Concern | How it is bounded |
|---|---|
| Your SSH session dies | systemd owns the process, not your shell |
| The collector crashes | `Restart=on-failure`, `RestartSec=30` |
| A crash loop polls a parked vehicle all night | `StartLimitBurst=5` per `StartLimitIntervalSec=3600`, then systemd gives up |
| `TRIAL_DURATION_S` is mistyped | `RuntimeMaxSec=7200` hard ceiling per start |
| The trial finishes normally | it exits 0, and `Restart=on-failure` does not restart a clean exit |
| A trial silently becomes permanent | the unit has no `[Install]` section, so it cannot be enabled at boot |
| A trial leaves config changed | `TRIAL_CONFIG` is a separate file; `config/hummer.toml` is never touched |

### When the systemd unit cannot be installed

Installing the unit needs root, and a `sudo` invocation over an unreliable link
is the fragile step the unit exists to remove. If the link will not hold long
enough (it failed twice during a drive on a phone hotspot), use the shell
supervisor instead. It mirrors the same guarantees without root:

```bash
cd ~/hummer-obd && setsid ./scripts/run_trial.sh >/dev/null 2>&1 </dev/null &
```

Keep the launch command **short**. The original incident was not the link
dropping; it was a long remote command that stopped the old collector, then
lost the link before starting the new one. Stage anything longer as a file
first, then invoke it in one short command.

After a trial, confirm the vehicle still sleeps before considering another:

```bash
hummer-obd-voltage --root . --interval-s 300 --duration-s 3600   # no CAN traffic
hummer-obd-capabilities --root .                                 # opens no serial device
```

## Battery watch and graceful shutdown

The node runs on a PiSugar2 pack. `hummer-battery.service` watches the cell and
powers the node down cleanly before it runs flat.

The reason is data, not tidiness: an unexpected power loss can corrupt the SD
card mid-write, and the SQLite database and append-only transcript on it hold
readings nobody can take again.

```bash
python3 -m hummer_obd.battery --once          # one reading, never shuts down
python3 -m hummer_obd.battery --dry-run       # log the decision, never act
systemctl status hummer-battery
journalctl -u hummer-battery -n 20
sudoedit /etc/default/hummer-battery          # threshold, interval, streak
```

### Known limitation: the node does not come back by itself

**`systemctl poweroff` halts the operating system. It does not tell the PiSugar
to cut power, and a halted Pi does not restart itself.** So if this watch fires
today, the outcome is:

- the Pi halts and keeps drawing a small current, so the cell keeps draining,
  just more slowly;
- power returning does **not** boot it, because power was never removed; and
- somebody has to walk out to the vehicle and press the button.

That protects the SD card and strands the node, which is the wrong trade for an
unattended vehicle node. Until it is resolved the service runs with
`--dry-run` on the reference node: it reads the cell, logs what it would do,
and never powers off.

Resolving it means choosing one of:

| Option | What it costs |
|---|---|
| Vehicle power, read-only root filesystem, battery as a UPS that never cuts power | a filesystem change; accepts bounded data loss on a dirty cut, which WAL and `fsync` already limit to seconds rather than corruption |
| Tell the PiSugar to cut power after halt, and rely on auto-boot when power returns | requires writing to the IP5209 power IC and confirming auto-boot behaviour; both need evidence this project does not yet have |
| Stop the collector cleanly on low battery and leave the OS running | keeps the node reachable and never strands it, but does not protect against the cell actually reaching cutoff |

The third is the smallest safe step and is the likely default: halting the OS is
only the right move once its return is guaranteed.

### Why it is hard to make it fire wrongly

A shutdown that fires when it should not leaves the node dead until somebody
walks out to the vehicle, which is worse than the flat battery it was meant to
prevent. So:

| Guard | Behaviour |
|---|---|
| Measured voltage, not a modelled percentage | the threshold is a number the hardware reports, not one derived from a discharge curve |
| Implausible reading | refused, and it **clears** the low streak rather than extending it |
| I2C bus failure | never counts towards a shutdown |
| One low reading | does nothing; five consecutive at 30 s apart are required |
| A cell that is rising | never shut down — below the threshold but recovering means it is on a charger |
| Writes to the power IC | none exist; a test asserts the module contains no I2C write |

### Hardware identification

A PiSugar2 carries an IP5209 and a PiSugar2 Pro carries an IP5312, and they
report battery voltage from different registers. The chip was identified by
measurement rather than from the label: on this node the IP5209 registers read
4.05 V while the IP5312 registers read 2.60 V — and 2.60 V is below the voltage
at which the Pi could have taken the reading at all, so it cannot be the cell.
`identify_chip()` repeats that check at run time and refuses to guess if both
profiles look plausible.

I2C on the GPIO header was not enabled on this node. `dtparam=i2c_arm=on` was
appended to `/boot/firmware/config.txt` (with a backup) so it survives a
reboot, and enabled at run time so no reboot was needed. The reader uses only
the standard library over `/dev/i2c-1` and needs group membership rather than
root.

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
