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

## Drive recorder

`hummer-drive.service` is the service that produces almost all of this
project's data, and until 2026-09-03 this runbook did not mention it.

```bash
systemctl status hummer-drive --no-pager
journalctl -u hummer-drive -n 40 --no-pager
```

It records a session whenever the vehicle is awake and sends **only `ATRV`**
while it sleeps. Settings live in `/etc/default/hummer-drive`; sessions land in
`evidence/sessions/drive-<timestamp>.csv`, one file per wake period.

### Restarting it

Restarts need no password — `scripts/enable_service_control.sh` installs a
sudoers rule scoped to exactly these units:

```bash
sudo systemctl restart hummer-drive
```

That rule exists because the recorder's code can be updated with no privilege
at all while the *running process* only picks up new code on a restart. A fix
could otherwise sit inert on disk while the vehicle drove away, which is exactly
what happened once.

### Reading what it collected

Three commands, none of which opens the serial device. All are safe to run at
any time, including while driving, and add no traffic to the vehicle:

```bash
# Every sensor, and how long since each last answered.
hummer-obd-live --watch

# What a completed session shows: distance, energy, efficiency, capture quality.
hummer-obd-analyze --dir evidence/sessions

# What each module says it supports, using J1979's own bitmaps.
hummer-obd-discover --confirm
```

`hummer-obd-live` reads the session the recorder is already writing rather than
competing for `/dev/rfcomm0`. **Never run a second diagnostic process against
that device while `hummer-drive` owns it** — two writers on one serial link
corrupt both streams. Anything that transmits (`hummer-obd-discover`,
`hummer-obd-enhanced`) needs the recorder stopped first:

```bash
sudo systemctl stop hummer-drive
# ... probe ...
sudo systemctl start hummer-drive
```

Pull the sessions to a workstation **before** stopping it. Data first.

### Listening without asking (`hummer-obd-passive`)

Every other tool here asks a question. This one asks nothing: it puts the
adapter into receive-only CAN monitoring — where it does not even assert the
acknowledgement bit that a normal CAN node puts on every frame it hears — and
records whatever arrives. It answers a different question from the rest of the
project: not *what will a module tell me if I ask*, but *is anything being said
at all*.

```bash
hummer-obd-passive                      # dry run: prints the exact command list
hummer-obd-passive --confirm --seconds 30 --label "parked, awake"
```

The dry run transmits nothing and does not open the serial device. It needs the
recorder stopped, like anything else that owns `/dev/rfcomm0`, and the sessions
pulled first.

**A capture of zero bytes exits 0 and is the expected result.** It is not a
failure to retry, and retrying it at a different length proves nothing. It says
the gateway forwards little or nothing unsolicited to this connector — which is
what [PASSIVE_CAN_VALIDATION.md](PASSIVE_CAN_VALIDATION.md) predicted, and which
is worth more as a measured fact about this truck than as an inference from
other people's vehicles.

**If bytes do arrive, do not read the count as bus load.** The link is ASCII
over Bluetooth at 115200 baud; it caps at a few hundred frames per second
against a bus carrying thousands. The capture is lossy by construction and the
loss is not recorded anywhere.

### Making an unknown field identify itself

Seventeen recorded columns are raw payload bytes with no meaning claimed, and no
amount of correlating them against the vehicle's *other* numbers will fix that —
it can only show that two of the truck's outputs move together. What identifies
a field is an **outside intervention**.

The recorder does not need stopping for this. It is already sampling every
column every eight seconds; all you add is a note of when you did something.

One-time setup, so the command is short enough to type at the vehicle:

```bash
scripts/install_mark_alias.sh          # on the node; safe to re-run
```

A command you will not type one-handed in the dark is a measurement you will
not take.

```bash
# on the node, or over ssh from a phone
mark "hvac max cool on"                # or the full form:
PYTHONPATH=src python3 -m hummer_obd.experiment mark "hvac max cool on"
#   ... hold that state at least three minutes ...
PYTHONPATH=src python3 -m hummer_obd.experiment mark "hvac off"

# then, offline, from anywhere
hummer-obd-respond --dir evidence/sessions
hummer-obd-respond --since 2026-09-04T04:00Z    # just tonight's experiment
```

`--since` matters more than it looks: **every mark is a segment boundary**, so a
stray one from setting the tooling up will slice the experiment into noise.

**Two rules that decide whether the result means anything.**

*Start with the largest step available.* A cabin setpoint moved one degree may
change nothing measurable; the climate system switched off and on is a
multi-kilowatt step this recorder cannot miss. Find which field responds with
the coarse step, then use fine steps to find its resolution. The other order
produces a null result that means nothing.

*Hold each state long enough.* At an eight-second cycle three minutes is about
twenty samples. `hummer-obd-respond` refuses to call a difference a response
when either side has fewer than eight, because a difference from five samples is
a coincidence with a decimal point.

Marks are keyed by UTC time, not by session, so it does not matter which CSV the
recorder happens to be writing or whether you cross a session boundary.

**What it gives you is association in time, and nothing more.** A field that
moves when the climate system starts may be measuring compressor current, cabin
temperature, the pack heater reacting or the 12 V load. Separating it from
everything that did *not* move is real progress; identifying it needs a second
intervention that changes one of those and not the others.

### Comparing captures (`hummer-obd-passive-diff`)

One capture answers *is anything being said*. Two answer the more interesting
question: *does it change when something happens?* Take a baseline with the
vehicle awake and nothing going on, then one capture per physical event —
one event each, never two in a file:

```bash
hummer-obd-passive-diff logs/raw/monitor-BASELINE.jsonl logs/raw/monitor-EVENT.jsonl
```

It opens no serial device and transmits nothing, so it is safe at any time.

**It will very likely tell you both captures are empty**, and that is a result
rather than a fault — the 2026-09-04 baseline returned zero bytes. Run two or
three event captures before investing any further in this path: if a fob lock
and a charge start also produce nothing, passive monitoring at this connector is
finished.

**A frame that changes when the doors lock is a lead, not a command.** This
project does not replay frames, and the tool says so in its own output. Modern
CAN security can include freshness counters, sequence numbers and message
authentication; a byte pattern is not an instruction.

Stop the capture and pull the adapter if `ATCS` — recorded automatically either
side of every capture — shows a transmit error counter off zero, if the driver
information centre shows any new message, or if any vehicle behaviour changes.
The full stop list is in
[PASSIVE_CAN_VALIDATION.md](PASSIVE_CAN_VALIDATION.md#rollback-and-stop-conditions).

### When it looks stuck

| Symptom | Cause | What to do |
|---|---|---|
| `adapter did not answer ATRV (n); retrying` | A dropped read. Normal on a moving vehicle. | Nothing. Three prompt retries precede any action. |
| `adapter still silent; reopening the link` | The link is gone, usually because the vehicle slept and the OBD port lost power. | Nothing. It reopens on its own; `rfcomm` binds connect-on-open. |
| `decoded nothing (n/3)` | Reads are reaching nothing. | Nothing yet — after three it exits and systemd restarts it with a fresh device. |
| Service `active` but no new rows for hours | Check `rfcomm -a`. `closed` with the vehicle awake is a genuine fault. | `sudo systemctl restart hummer-drive` |

A session that ends is not a fault: one file per wake period is the design.

---

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

The radar panel's `/api/r8` response checks two clocks: `age_s` is the age of
the published state, while `telemetry_age_s` is the detector packet age at
publication plus elapsed time since publication. A heartbeat does not refresh
detector telemetry. Packets older than ten seconds, a producer stale flag,
unknown packet age, or a disconnected detector withhold cached voltage,
GPS-lock and live alerts. The dashboard keeps the detector screen dark when
stale or disconnected; recorded alert history remains available separately.

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

## Running the commands

The package is installed **editable** on the node, so the seven console scripts
are on `PATH` and `src/` remains the source of truth — a `scripts/deploy.sh`
does not need a reinstall.

```bash
cd ~/hummer-obd
hummer-obd-capabilities --root .        # opens no serial device
hummer-obd-export --root . --format jsonl --output evidence/export.jsonl
hummer-obd-voltage --root . --interval-s 300 --duration-s 28800 \
    --output evidence/voltage-watch.csv
```

If a command reports *command not found*, the install step has not been run:
`scripts/bootstrap_pi.sh` performs it, and it is safe to re-run. Before that
step existed every documented command failed this way, while
`PYTHONPATH=src python3 -m hummer_obd.<module>` worked — that fallback still
works and is what the systemd units use.

Two things worth knowing when writing your own invocations:

- **Reach the node by its Tailscale name**, not `hummer.local`. mDNS resolves
  to whichever LAN the node last joined (it has answered on a home LAN and on a
  phone hotspot in the same day), so `hummer.local` can point at a stale
  address while the node is perfectly reachable over the tailnet.
- **Capture a timestamp once**, not twice. `foo-$(date -u +%Y%m%dT%H%M%SZ).json`
  and `... | tee foo-$(date -u +%Y%m%dT%H%M%SZ).txt` in one pipeline evaluate
  `date` twice and can straddle a second boundary, leaving the JSON and the
  transcript with different names. Use `STAMP=$(date -u +%Y%m%dT%H%M%SZ)` first.

## Battery watch and graceful shutdown

The node runs on a PiSugar pack — a PiSugar2 until 2026-10-17 and a **PiSugar3**
since. The legacy `hummer-battery.service` watches voltage and stops polling;
it does not shut down by default. The PiSugar3 reserve installation below
replaces that service's command with a graceful shutdown monitor and installs
the missing UPS output-cut hook.

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

### Restarting the watch (and the API) from a workstation

The node's polkit rules authorise `start`/`stop` of `hummer-drive` without a
password, and nothing else — so `hummer-battery` and `hummer-dashboard` need
`sudo` **on the node**. Typing `systemctl restart hummer-battery
hummer-dashboard` on a workstation fails with "Unit hummer-battery.service not
found", because those units are only on the Pi.

```bash
scripts/restart-node-services.sh --check   # read-only: what is running, and does it work
scripts/restart-node-services.sh           # restart both, then verify (prompts for the node's sudo)
```

It never names `hummer-drive`. The recorder is the one service whose
interruption loses readings nobody can take again, and neither of these two
depends on it — `hummer-dashboard` wants only `network-online.target` and
mounts, `hummer-battery` only `multi-user.target` — so a restart cannot cascade
into it. The script asserts the recorder is still active afterwards rather than
relying on that, and an exit code is not taken as acceptance: it checks that the
battery watch *identifies the board and reads a plausible cell*, that the API
*serves the history column the 3D view draws from*, and that the PiSugar3's
restore bit is still set.

Both services also pick up newly deployed code on the next power cycle by
themselves, so this is a way to avoid waiting rather than the only route.

### PiSugar3 reserve shutdown installation

**Installed and running, verified 2026-10-19 at 10:17 MST.** The operator ran
the manual root installer; fresh logs show `reserve=10%`, `floor=3.40 V`, three
readings 30 s apart, and `dry_run=False`. Both root-owned program copies and
the service drop-in match the committed files. The recorder kept its process
and had no restart. **A physical return-of-power test remains deferred** at the
owner's request to keep the Pi online; no controlled shutdown was initiated.
The 2026-10-19 boot already loaded Claude's PiSugar3 reader and updated dashboard;
the earlier request to restart those two services is no longer pending.

For initial installation or future updates, run the installer **on the Pi**
after deploying the source:

```bash
cd /home/jeremy/hummer-obd
sudo bash scripts/install-pisugar3-power.sh
```

From a workstation, use `ssh -t "$PI_HOST" 'sudo bash
/home/jeremy/hummer-obd/scripts/install-pisugar3-power.sh'`, with `PI_HOST` set
to the node's SSH destination. It refuses a workstation, checks the actual
board and firmware, backs up existing files, and restarts only
`hummer-battery`. It does not stop the recorder or power the Pi off.

After installation:

| Condition | Action |
|---|---|
| External UPS input present | Keep running, including when charge is low |
| Input absent and board estimate at or below 10% | Three consecutive readings, 30 s apart, request normal `systemctl poweroff` |
| Input absent and cell at or below 3.40 V | Same debounce, emergency fallback even if percentage is optimistic |
| Invalid I2C/voltage/percentage, power changing during a sample, or restore bit absent | Clear the streak; do not request shutdown |
| Shutdown requested | systemd stops services normally so their signal handlers can flush files |
| Final poweroff/halt phase with root read-only | Root-owned hook sets restore, zeroes the cutoff delay, and disables UPS output |
| Vehicle input returns during automatic shutdown | Hook reboots directly from the final phase, avoiding a halted Pi on a live supply |
| Normal reboot/kexec | Hook does not open I2C or cut power |

The 10% value is the UPS firmware's **estimate**, read at register `0x2A`,
not a guaranteed measured energy reserve. The separate voltage floor protects
against an optimistic estimate. Three readings span at least 60 seconds;
the final request takes another fresh reading to catch returning input.

The installer copies `pisugar3_power.py` as a root-owned standalone program
under `/usr/local/lib/hummer-power` and as an executable under
`/usr/lib/systemd/system-shutdown`. Python isolated mode excludes user modules.
The `20-pisugar3.conf` service drop-in runs the monitor as root, with a
read-only filesystem view except `/run`. No broad sudo or polkit permission is
added. It checks/enables the restore bit at every service start. The final
hook refuses to cut while PID 1 is still normal systemd or root is writable.
Firmware older than 1.2.4 and separate `/usr` mounts are refused.

The old `/etc/default/hummer-battery` is left intact but no longer used by the
drop-in. Configure the new threshold in the drop-in's `ExecStart`; the monitor
accepts reserves from 5 through 10%. The installed root-owned copies must be
updated by re-running the installer after future source changes.

Check without changing state:

```bash
python3 -I /home/jeremy/hummer-obd/src/hummer_obd/pisugar3_power.py check
systemctl show hummer-battery -p User -p ExecStart -p ExecStartPre
journalctl -u hummer-battery -n 15 --no-pager
```

Physical acceptance requires an observed sequence: UPS input disappears,
reserve shutdown completes, UPS output actually switches off, vehicle input
returns, and SSH plus the recorder return automatically. A read restore bit
alone does not prove this sequence. The vehicle feed must be connected to the
**UPS charging input** and must really lose and restore voltage; a permanently
live accessory outlet or a separate supply to the Pi cannot supply that edge.
No shutdown or vehicle power cycle was forced during the audit.

A person at the vehicle can test the cutoff/return path before draining the
pack to 10%: first confirm `external=False` using the read-only check, then
manually run `sudo systemctl poweroff` on the Pi. Wait for UPS output to stop
before restoring the vehicle feed. Confirm SSH and the recorder return. That
checks the hardware return path; a later actual low-battery shutdown is still
needed to accept the complete reserve lifecycle.

Rollback on the Pi (backups are printed by the installer):

```bash
sudo rm /etc/systemd/system/hummer-battery.service.d/20-pisugar3.conf
sudo rm /usr/lib/systemd/system-shutdown/hummer-pisugar3
sudo systemctl daemon-reload
sudo systemctl restart hummer-battery
```

This returns to the legacy voltage watch. If those paths existed before this
installation, restore their installer backups instead of removing them.

Register semantics and write protection come from the [PiSugar3 datasheet](https://docs.pisugar.com/docs/product-wiki/battery/pisugar3/pisugar-3-i2c)
and [vendor driver](https://github.com/PiSugar/pisugar-power-manager-rs/blob/master/pisugar-core/src/pisugar3.rs).
The late-hook ordering follows [systemd's shutdown implementation](https://github.com/systemd/systemd/blob/main/man/systemd-poweroff.service.xml).
The vendor driver uses control bit 5 to cut output; newer documentation also
describes it as delayed cutoff, so register `0x09` is explicitly set to zero.
Only the intended control bits are changed, preserving all other bits.

### Why the legacy watch stops polling

This used to rest on one fact — a PiSugar2 cannot power the Pi back on — and
**the PiSugar3 changed that half.** It still stops the collector, for the half
that did not change:

| Question | Answer, on the PiSugar3 |
|---|---|
| Can the pack power the Pi back on when external power returns? | **Yes, and it is already set.** Control register 1 (`0x02`) read `0xFC` on this node, which has the restore bit (bit 4) set. Nothing had to be programmed. On the PiSugar2 this was "not supported" |
| Does the Pi itself need anything programmed to boot when 5 V arrives? | **No.** A Pi Zero 2 W has no power latch and boots whenever 5 V appears. There is no setting to change |
| Does a bare `systemctl poweroff` cut PiSugar power? | **No, and this is the half that still bites.** The vendor ships a `pisugar-poweroff` service precisely because something must tell the chip to cut, over I2C, during shutdown. It is **not installed here**. Without it the Pi sits halted while the board keeps supplying it, and the restore bit never fires — from the board's point of view power never went away |
| Can GPIO3 wake a halted Pi Zero 2 W? | **Unknown.** `WAKE_ON_GPIO` is a bootloader-EEPROM setting, and Raspberry Pi documents the Zero 2 W as having no EEPROM. GPIO3 wake for non-EEPROM boards is a community claim, not a documented feature |

Without the reserve installation above, `--poweroff` strands the node, for a different reason
than before: not because the board cannot bring it back, but because nothing
cuts the output on the way down, so there is no power cycle for the restore bit
to act on. **Automatic power-on only helps across an output the board actually
cut.** The default stays `stop-collector`, and `--poweroff` becomes genuinely
safe the day something cuts the output during shutdown.

The default action is therefore `stop-collector`: it ends vehicle polling --
the thing actually drawing power and holding the serial port -- and leaves the
operating system up and reachable. A node you can still talk to can be told
what to do next; a halted one cannot. The watch keeps running afterwards, so a
recovering cell needs no intervention.

Do not enable legacy `--on-low poweroff` as a substitute for the complete
reserve installation and physical acceptance.

**If you want the node to survive vehicle power loss unattended**, the answer
is not a better shutdown, it is not needing one: run from vehicle power with a
read-only root filesystem, keep the pack as a UPS that never cuts, and accept
that a dirty cut costs seconds of data rather than the filesystem. WAL and
`fsync` already bound the loss to seconds; a read-only root removes the
corruption risk that a battery shutdown was protecting against.

Scheduled RTC wake is a separate feature and is not configured here. The
PiSugar3 has an MCU-emulated RTC; the separate SD3078 belonged to the old board.

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

Three boards are known. A PiSugar2 carries an IP5209 and a PiSugar2 Pro an
IP5312, reporting battery voltage from different registers at `0x75`; the
**PiSugar3** is a single MCU at `0x57`. Every one was identified by measurement
rather than from the label.

PiSugar2, while it was fitted: the IP5209 registers read 4.05 V while the IP5312
registers read 2.60 V — and 2.60 V is below the voltage at which the Pi could
have taken the reading at all, so it cannot be the cell.

PiSugar3, on 2026-10-17: `0x75` and `0x32` stopped acknowledging altogether,
`0x57` answers, its firmware register (`0xE2`) reads `v1.3.4`, and its voltage
pair (`0x22` high, `0x23` low, plain big-endian **millivolts**) read 3860 mV.

The swap changed three things at once, which is why the old profiles did not
merely read wrong numbers but could not read at all — and why the watch logged
`unusable reading: no power IC read a plausible cell voltage` every 30 seconds
instead of converting something. **The transaction style moved too**: read the
PiSugar3 with a write-then-read instead of one combined transaction and every
register returns `0x03`. That is the worst available failure, because `0x03` is
a number a profile will happily convert, and only the plausibility floor stands
between it and a confident wrong voltage. A test asserts that it stands.

`identify_chip()` repeats the whole check at run time across all three profiles
and refuses to guess if more than one looks plausible. A board that is simply
absent disqualifies itself through the ordinary path: its address does not
acknowledge, the read raises, and the reading is implausible.

I2C on the GPIO header was not enabled on this node. `dtparam=i2c_arm=on` was
appended to `/boot/firmware/config.txt` (with a backup) so it survives a
reboot, and enabled at run time so no reboot was needed. The reader uses only
the standard library over `/dev/i2c-1` and needs group membership rather than
root.

## USB GPS naming and hotplug

The same BU-353S4 receiver changed from `/dev/ttyUSB0` to `/dev/ttyUSB1` on
2026-10-19. gpsd was still configured with the old name, so the daemon was
active but its actual `DEVICES` response was empty. Debian's installed
`60-gpsd.rules` deliberately disables automatic matching for generic PL2303
USB serial adapters, which explains why `USBAUTO="true"` alone did not fix it.

`scripts/install-gps-hotplug.sh` installs a durable repair using the measured
USB identity and a stable `/dev/hummer_gps` alias. **Installed and verified on
2026-10-19 at 14:12 MST:** the alias resolves to `ttyUSB0`, gpsd registers it
with the SiRF driver and a live 3D fix, and the hotplug unit is active. The
recorder's GPS reader also receives a 3D fix using eight satellites. Both
recorders stayed running without restarts. Physical unplug/replug acceptance
is still pending. For installation or updates, run on the Pi:

```bash
sudo bash /home/jeremy/hummer-obd/scripts/install-gps-hotplug.sh
```

From a workstation, use:

```bash
ssh -t "$PI_HOST" 'sudo bash /home/jeremy/hummer-obd/scripts/install-gps-hotplug.sh'
```

The installer refuses a workstation and requires exactly one receiver matching
the inspected USB profile. It backs up `/etc/default/gpsd` and any prior
`79-hummer-gps.rules` under `/var/backups/hummer-gps/`, preserves unrelated
gpsd options, sets `DEVICES="/dev/hummer_gps"` and `USBAUTO="true"`, then
installs a narrowly matched udev rule. Only this receiver receives a change
event; there is no global USB trigger. It restarts gpsd and its GPS hotplug
unit, while leaving the OBD and radar recorders running. Their existing gpsd
clients reconnect to the same local port.

On future insertion, udev creates `/dev/hummer_gps`, declares its systemd
device alias and starts Debian's `gpsdctl@hummer_gps.service`. That stock unit
adds the alias to gpsd and is bound to `dev-hummer_gps.device`; on removal it
removes the device from gpsd. Both the configured boot path and hotplug path
use the same alias, preventing separate alias/kernel-name registrations.
Rebooting, re-enumerating under another `ttyUSB` number, or moving this single
receiver to another USB port therefore do not require editing the configured
device name.

The receiver exposes a model identity without a unique hardware serial. The
installer refuses two indistinguishable matching adapters; supporting two of
those at once requires a deliberate physical-port mapping. Unrelated USB
serial hardware does not match the installed rule.

Read-only checks, with no coordinates printed:

```bash
readlink -f /dev/hummer_gps
python3 -I /home/jeremy/hummer-obd/src/hummer_obd/gps_setup.py --check
systemctl status gpsd gpsdctl@hummer_gps --no-pager
```

The check compares gpsd's actual device list with the alias's resolved target,
so it also accepts a daemon reporting the underlying numbered path. It reports
device registration separately from satellite fix mode: `registered=true`
with no fix means the device is present but has not acquired satellites. An
empty device list fails the check even if systemd says gpsd is active.

Remaining physical acceptance: unplug/replug the receiver, then verify the
alias and gpsd registration return automatically and the recorder processes
stay running. Numbered tty names may or may not change on any given
reinsertion. The operator's installer restarted gpsd; the agent performed
read-only runtime verification without unplugging hardware or rebooting.

To roll back, restore the installer backups, remove the new rule if none
previously existed, reload udev rules, stop `gpsdctl@hummer_gps.service`, and
restart gpsd. The original numbered path may still be absent, so inspect it
before relying on the restored configuration.

This follows [Debian's gpsd hotplug unit](https://sources.debian.org/src/gpsd/3.25-5%2Bdeb13u2/systemd/gpsdctl%40.service.in/),
the [gpsd device-management interface](https://gpsd.io/gpsd.html), and
[systemd device aliases and dependencies](https://www.freedesktop.org/software/systemd/man/latest/systemd.device.html).

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
