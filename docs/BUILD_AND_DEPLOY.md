# Build and deploy

This guide reproduces the reference node from a clean Raspberry Pi OS image.
Commands that touch a vehicle are isolated in the final sections. Development,
tests, deployment, display simulation, and offline replay need no vehicle.

## 1. Hardware

- Raspberry Pi Zero 2 W with a reliable microSD card and power supply
- Waveshare 2.13-inch E-Ink Display HAT V4, 250x122 monochrome
- OBDLink MX+ Bluetooth adapter
- GMC Hummer EV (required only for the supervised read-only probe)
- a 2.4 GHz Wi-Fi network; the Zero 2 W does not support 5 GHz Wi-Fi

Do not power the finished installation from an always-live vehicle circuit
until its sleep current has been measured. The continuous collector stays off
until the power/sleep gate in section 10 is satisfied.

## 2. Development workstation

```bash
git clone git@github.com:JeremyWhittaker/hummer_obdII.git
cd hummer_obdII

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
python -m pytest -q
```

The suite uses a PTY-backed ELM simulator. It does not require Bluetooth,
serial hardware, GPIO, the display, or a vehicle.

## 3. Prepare Raspberry Pi OS

Use 64-bit Raspberry Pi OS Lite (Debian 13/trixie or newer), enable SSH, and
configure a non-root user. The reference units expect:

```text
hostname: hummer
user:     jeremy
path:     /home/jeremy/hummer-obd
```

Those values can be changed, but the systemd units and script defaults must be
updated together.

### Optional: patch an existing SD card offline

`patch_hummer_pi_sd.sh` safely modifies an existing Raspberry Pi OS card. It
never flashes, formats, or repartitions. It requires an exact whole-disk
confirmation and creates timestamped backups of every replaced file.

Create a private, untracked `hummer_pi_sd.env` next to the script:

```dotenv
WIFI_SSID=your-2.4-ghz-ssid
WIFI_PASSWORD=replace-with-your-wifi-password
PI_PASSWORD=replace-with-an-initial-login-password
TAILSCALE_AUTH_KEY=
```

Then inspect the detected device carefully:

```bash
sudo ./patch_hummer_pi_sd.sh --device /dev/sdX
```

The script shows the FAT boot and ext4 root partitions and requires the exact
text `PATCH /dev/sdX` before mounting either one. Never substitute the host's
system disk.

After first login, install an SSH public key and disable password login if your
operating environment permits it. Never commit the environment file.

## 4. Install target dependencies

On the Pi:

```bash
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y \
  bluetooth bluez pi-bluetooth rfkill \
  python3 python3-venv python3-pip python3-serial \
  python3-pil python3-numpy python3-spidev python3-rpi.gpio python3-gpiozero \
  git curl jq sqlite3 rsync screen minicom mosquitto-clients

sudo systemctl enable --now ssh bluetooth
sudo rfkill unblock all
```

Enable SPI in `raspi-config` or set `dtparam=spi=on` in the active Raspberry Pi
boot configuration, then reboot and verify:

```bash
ls -l /dev/spidev0.0 /dev/spidev0.1
lsmod | grep spi_bcm2835
```

Tailscale is optional. The project requires only IP connectivity and SSH; it
does not rely on a particular private DNS name.

## 5. Deploy the repository

From the workstation:

```bash
HOST=jeremy@hummer.local DEST=/home/jeremy/hummer-obd scripts/deploy.sh
```

If mDNS is unavailable, replace `hummer.local` with the Pi's LAN or private VPN
address. `deploy.sh` copies source, tests, configuration templates, docs,
scripts, and systemd units. It does not copy secrets or runtime data and does
not enable a service.

On the Pi:

```bash
cd /home/jeremy/hummer-obd
./scripts/bootstrap_pi.sh
```

The bootstrap script:

- creates private `logs/raw`, `data`, and `evidence` directories;
- copies the example configuration only when no local configuration exists;
- runs the hardware-free test suite;
- fetches and verifies the pinned official Waveshare V4 driver; and
- installs all four systemd units without enabling them.

## 6. Configure the node

```bash
cd /home/jeremy/hummer-obd
cp -n config/hummer.example.toml config/hummer.toml
chmod 600 config/hummer.toml
```

Important defaults:

```toml
[collector]
enabled = false
pids = ["010D", "011F", "0142"]

[upload]
enabled = false
endpoint = ""
```

The default PID set is the small standard set validated on the reference
vehicle: speed, run time since start, and control-module voltage. Do not add an
enhanced PID merely because it appears in an app or online list; validate it
for the exact vehicle and update the safety gate and tests first.

## 7. Validate the display

First render without hardware:

```bash
cd /home/jeremy/hummer-obd
PYTHONPATH=src python3 -m hummer_obd.display.status \
  --once --simulate /tmp/hummer-status.png
```

Then perform one physical refresh:

```bash
PYTHONPATH=src:vendor/waveshare \
  python3 -m hummer_obd.display.status --once
```

If the panel orientation, contrast, and text are correct:

```bash
sudo systemctl enable --now hummer-display.service
systemctl status hummer-display.service --no-pager
```

The service uses full refreshes, skips identical frames, and sleeps the panel
between updates to reduce ghosting and wear.

## 8. Pair and bind the OBDLink

This is the first step that needs the physical adapter. Keep phone diagnostic
apps disconnected because the adapter may accept only one active client.

1. Plug the OBDLink MX+ into the vehicle.
2. Press its pairing button.
3. Scan and require exactly one OBDLink-looking result:

   ```bash
   sudo ./scripts/pair_obdlink.sh scan
   read -r -p 'OBDLink MAC: ' OBD_MAC
   ```

4. Pair interactively, answering `yes` to BlueZ's six-digit confirmation:

   ```bash
   sudo ./scripts/pair_obdlink.sh pair "$OBD_MAC"
   ```

5. Inspect SDP and record the channel from the **Serial Port / STN-SPP**
   service—not an iAP service:

   ```bash
   sudo ./scripts/pair_obdlink.sh sdp "$OBD_MAC"
   ```

6. Bind the confirmed channel and install the persistent unit configuration:

   ```bash
   read -r -p 'Confirmed SPP channel: ' SPP_CHANNEL
   sudo ./scripts/pair_obdlink.sh bind "$OBD_MAC" "$SPP_CHANNEL"
   sudo systemctl enable --now hummer-rfcomm.service
   ls -l /dev/rfcomm0
   rfcomm
   ```

The reference OBDLink requires interactive Secure Simple Pairing. An
unattended `NoInputNoOutput` BlueZ agent fails authentication. The
`hummer-btdiscover` recovery unit intentionally cannot perform first pairing;
it only recovers a lost binding for an existing bond.

## 9. Run and review one raw probe

Put the vehicle in an awake state with the key present. Do not clear DTCs or
run actuator tests.

```bash
cd /home/jeremy/hummer-obd
PYTHONPATH=src python3 -m hummer_obd.probe \
  --device /dev/rfcomm0 \
  --config config/hummer.toml \
  --root . \
  --summary evidence/probe-summary.json
```

Review the transcript offline:

```bash
PYTHONPATH=src python3 scripts/review_raw_log.py logs/raw/probe-*.jsonl
```

Acceptance criteria:

- hex and base64 agree for every raw record;
- adapter identity and a selected protocol are present;
- `0100` yields a supported-PID bitmap;
- VIN, if returned, is shown only masked;
- DTC reads parse as valid empty/non-empty results;
- every transmitted command passes the current safety gate;
- no Mode 04, Mode 08, Mode 22, or UDS write/control/security service appears.

If every vehicle request returns `NO DATA`, stop and wake the vehicle. Never
increase traffic to force it awake.

## 10. Prove one collector cycle

After the probe transcript passes review:

```bash
PYTHONPATH=src python3 -m hummer_obd.collector \
  --config config/hummer.toml --root . --once --force

sqlite3 data/hummer_obd.sqlite3 \
  'SELECT ts,pid,name,value,unit,status FROM samples ORDER BY id DESC LIMIT 10;'
```

Leave `collector.enabled = false` and the systemd unit disabled until one of
these conditions is proven:

- the vehicle completes a full sleep/wake cycle while the adapter and Pi are
  attached without abnormal 12 V battery draw; or
- the Pi is powered from a confirmed ignition-switched supply.

Only after that separate power validation should an operator deliberately set
`enabled = true` and run:

```bash
sudo systemctl enable --now hummer-collector.service
```

This deployment gate is intentionally stricter than “the software works.”

## 11. Final acceptance

```bash
cd /home/jeremy/hummer-obd
./scripts/pi_smoke.sh
systemctl --failed --no-pager
systemctl is-enabled hummer-display hummer-rfcomm hummer-collector
systemctl is-active hummer-display hummer-rfcomm hummer-collector
```

Expected before the power decision:

- display: enabled and active;
- RFCOMM: enabled and active;
- collector: disabled and inactive;
- failed units: none.

Continue with the [Runbook](RUNBOOK.md) for normal operation and recovery.
