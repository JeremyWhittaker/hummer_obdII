# Validation record

Last hardware-backed validation: 2026-09-04 (UTC).

This document publishes the useful acceptance results without publishing the
VIN, network identifiers, Bluetooth addresses, raw diagnostic frames, or local
credentials. Detailed terminal captures and raw logs remain private on the
reference node.

## Automated test matrix

| Area | Acceptance covered |
|---|---|
| Safety gate | approved AT/ST and standard read services accepted; Mode 04/08/22, UDS write/control/security, batching, malformed and unknown commands rejected |
| Serial transport | prompt framing, partial reads, timeout, reconnect, and proof that rejected commands write zero bytes |
| Raw logging | append semantics, sequence order, byte-for-byte hex/base64 round trips, corruption reporting |
| Adapter session | initialization, protocol selection, supported-PID discovery, read-only DTC and Mode 09 flows |
| CAN/ISO-TP decode | 11-bit and 29-bit framing, per-ECU interleaving, complete multi-frame VIN, truncated sequence rejection |
| Collector/storage | one-shot collection, sleep/no-data backoff, WAL schema, session closure, local queue markers |
| Bluetooth recovery | candidate ambiguity, bond-state checks, SPP selection, command allowlist, and proof that recovery cannot pair/trust/remove |
| Display | 250x122 1-bit render, status formatting, simulation, rotation, and change detection |
| Upload | disabled-by-default refusal, endpoint requirement, successful marking, failure preservation |
| Integration | PTY-backed simulated ELM adapter; exact command transcript and no forbidden transmission |

Latest result — **the counts that used to sit here said 509 tests / 391
subtests while the suite measured 836 / 2424.** A written count is a claim
like any other and this one drifted by 327 tests. Run the command instead:

```text
pytest:   run it -- `PYTHONPATH=src python3 -m pytest -q`
unittest: run it -- `PYTHONPATH=src python3 -m unittest discover -s tests -q`
shell:    all repository shell scripts pass bash -n
compile:  all Python modules compile
```

CI runs the same pytest suite on Python 3.11, 3.12, and 3.13.

## Hardware acceptance

| Check | Result |
|---|---|
| Pi platform | Raspberry Pi Zero 2 W, 64-bit Debian 13 |
| Headless boot | SSH, NetworkManager, Bluetooth, and private VPN access recovered after reboot |
| SPI | `/dev/spidev0.0` and the Pi SPI driver present |
| E-paper | official Waveshare `epd2in13_V4` driver loaded; physical panel refreshed; simulated PNG visually inspected |
| Bluetooth | OBDLink paired, bonded, and trusted using interactive Secure Simple Pairing |
| SDP/RFCOMM | exactly one Serial Port (`STN-SPP`) record selected; `/dev/rfcomm0` bound on channel 1 |
| Persistent services | e-paper and RFCOMM units enabled/active; zero failed units at checkpoint |
| Recovery/collector | recovery watcher disabled after success; continuous collector disabled by policy |

A controlled final reboot produced a real SSH outage, returned over both mDNS
and the private VPN path in approximately 36 seconds, and passed the following
post-boot checks:

- SSH, NetworkManager, Bluetooth, mDNS, and private VPN services enabled and
  active;
- display and RFCOMM services enabled and active;
- `/dev/rfcomm0` present on the confirmed channel;
- OBDLink still paired, bonded, and trusted;
- recovery watcher and collector still disabled/inactive;
- collector configuration still false with exactly `010D`, `011F`, `0142`;
- physical panel refreshed after boot;
- zero failed systemd units;
- raw probe and collector transcript hashes unchanged; and
- all 134 tests plus the safety-gate smoke check passed on the Pi.

## Read-only vehicle probe

One supervised raw probe completed successfully. Its private JSONL transcript
contained 66 records (31 request/response pairs plus session events), 37,812
bytes, with SHA-256:

```text
71baee61ab42d16ef2698a39bb8d54457df793eb5bf504a988e8acc25108101c
```

The offline reviewer found zero corrupt records and zero hex/base64
disagreements.

### Adapter and protocol

| Query | Result |
|---|---|
| `ATI` | `ELM327 v1.4b` |
| `STI` | `STN2255 v5.12.4` |
| `STDI` | `OBDLink MX+ r3.1.3` |
| supply voltage | 13.9 V during the probe |
| selected protocol | AUTO, ISO 15765-4 (CAN 29/500) |
| supported standard PIDs | 14 advertised Service 01 PIDs |

The adapter's secondary device-description query returned `?`; this was an
unsupported adapter-information command, not a vehicle failure.

### Vehicle reads

- vehicle speed: 0.0 km/h;
- run time since start: 1100 seconds;
- control-module voltage: 13.747 V;
- stored DTCs: zero codes from eight responding modules;
- pending DTCs: zero codes from eight responding modules;
- permanent DTCs: zero codes from five responding modules;
- VIN: decoded to 17 characters and reported only in masked form; and
- literal `NO DATA` responses: none during the probe.

The vehicle was awake enough to answer without an additional ready-mode step.
Its exact ignition state was not independently observed, so the result should
not be used to infer that every future probe works while the vehicle is off.

### Safety audit

Every transmitted command was replayed through the current gate. The transcript
contained only adapter setup/identity plus Modes 01, 03, 07, 09, and 0A.

```text
Mode 04 / DTC clear:              absent
Mode 08 / actuator control:       absent
Mode 22 / enhanced identifiers:   absent
UDS write/control/security:       absent
```

## Collector acceptance

One forced one-shot collector cycle completed after probe review:

```text
session:    collect-20260901T225855Z
records:    54
raw SHA-256 bdbafd5dfd251ad0fb357a3f7ca0df4be81046166411258b09b1a6c80b335ce7
```

It stored decoded speed and control-module voltage, recorded valid zero-DTC
reads, preserved raw response hex in SQLite, and closed the session cleanly.
Four non-advertised test PIDs returned `NO DATA`; they were removed from the
continuous polling set. The resulting configured set is `010D`, `011F`, and
`0142`.

## Sleep-state capability probe and the power gate

A second capability probe was run on 2026-09-01 at the owner's request, after
the vehicle had been left parked. It produced the clearest evidence yet about
both the adapter and the power gate, and it did so without ever escalating
traffic to a sleeping vehicle.

### Adapter identity, in full

`STDIX` was requested for the first time. It returns the adapter's complete
self-description in one reply:

| Field | Result |
|---|---|
| Device | `OBDLink MX+ r3.1.3` |
| Firmware | `STN2255 v5.12.4 [2025.12.15]` |
| Manufacturer | `OBD Solutions LLC` |
| Bootloader | `4.4` |
| Bluetooth modem | `BT24H, R15` |
| Engine cranks / starts | `0` / `0` |

The serial number, Bluetooth device name and Bluetooth address it also returns
are private identifiers and are recorded only on the node.

`STSN`, `ATCS` and `STPRS` were also confirmed to answer for the first time.
The engine crank and start counters reading zero is the expected result for a
battery-electric vehicle.

### Three distinct vehicle states are now distinguishable

| Adapter reading | Interpretation |
|---|---|
| positive data from 5–8 ECUs | vehicle serving diagnostics |
| `7F <service> 22` from ECU `28` only | `BSCM-BrakeSystem` alive, refusing: `conditionsNotCorrect`. Not the gateway; that is `45` |
| `NO DATA` with `ATCS T:00 R:00` | protocol correct, request sent, no ECU on the bus |
| `SEARCHING... / UNABLE TO CONNECT` | auto-detect found no protocol at all |

The third row is new and it matters. With the protocol forced to `ATSP7`
(`ISO 15765-4 (CAN 29/500)`, confirmed by `ATDP`), `0100` returned `NO DATA`
while `ATCS` reported `T:00 R:00` — zero CAN transmit errors and zero receive
errors. The adapter transmitted correctly and nothing answered. That separates
"the vehicle is asleep" from "the adapter or wiring has a fault", which had
previously been indistinguishable.

Forcing the known protocol rather than repeating `ATSP0` auto-search was also
the lower-traffic choice: auto-search walks every protocol, including K-line
initialisation sequences this vehicle has no use for.

### The OBD-II port is always live

Two independent observations agree:

1. The adapter's own `STDIX` counters showed a single continuous power-on
   session of over seven hours, spanning periods when the vehicle was off.
2. The owner directly observed that after the truck powered itself off, the
   OBD-II port and the devices attached to it still had power.

This resolves the first question in the safe-next-milestone list: the port is
**not** ignition-switched, so the Pi and the adapter are a parasitic load on the
12 V battery whenever they are attached.

It also produced one encouraging negative result: the vehicle powered itself
off normally while the adapter was connected, shortly after diagnostic traffic.
Adapter presence alone did not hold the vehicle awake. That is **not** the same
as proving a two-second polling loop lets the modules reach deep sleep, and it
does not open the gate.

### `ATRV` as a zero-traffic measurement

`ATRV` reads connector voltage without a protocol, without an ECU and without
any CAN traffic. Values observed so far:

| Vehicle state | `ATRV` |
|---|---|
| awake, DC-DC converter running | 13.9 V |
| powered off, bus silent | 12.7 V – 13.0 V |

Trending this across a full sleep period is the measurement the continuous
collector gate is waiting on, and it can be taken without waking the bus.

### Capability gap found in our own code, not the vehicle

The vehicle advertises 14 service 01 PIDs. The probe had been asking from a
fixed generic list that overlapped that set in only three places, so **eight
advertised PIDs had never been requested** — including `A6`, the odometer.

Two fixes followed, neither of which changes the safety boundary:

- `probe.py` now reads exactly what the vehicle's own support bitmap
  advertises, minus the bitmap pointers, falling back to the generic list only
  when the vehicle will not answer the bitmap. It also reads the service 09
  support bitmap (`0900`) and asks for the items the vehicle advertises,
  excluding `0902`, which stays on the separate masked path.
- `decode.py` gained decoders for `A6` (odometer, four bytes at 0.1 km per
  bit), `30` (warm-ups since codes cleared) and `1C` (OBD standard conformance
  code). PID `01` is deliberately left undecoded: it is a composite of MIL
  state and readiness monitors, and the scalar sample shape cannot represent it
  honestly.

The odometer reading itself remains **unproven**. `A6` is advertised by the
vehicle but has not yet returned a value, and a probe run while the bus is
asleep cannot prove it.

### Commands transmitted

```text
ATZ ATE0 ATL0 ATS0 ATH1 ATAL ATAT1
ATI AT@1 AT@2 STI STDI STDIX STSN ATRV ATCS
ATSP0 0100 ATDP ATDPN STPRS
0120 0140 0160 0180 01A0 01C0 0900
ATSP7 ATDP ATDPN 0100 ATCS ATRV
```

No clear, control, write, security, or Mode 22 request appeared in either
transcript. After the second run confirmed a silent bus, vehicle traffic was
stopped rather than repeated: a sleeping vehicle is a wait condition.

## Full standard-OBD capability probe

With the vehicle awake on 2026-09-01, the improved probe read **every** Service
01 PID the vehicle advertises rather than a fixed generic list. All fourteen
answered.

| PID | Reading | Value |
|---|---|---|
| `01` | monitor status since DTCs cleared | answered; left undecoded by design |
| `0D` | vehicle speed | 0.0 km/h |
| `1C` | OBD standard conformance code | 5 |
| `1F` | run time since engine start | 1144 s |
| `21` | distance travelled with MIL on | 0 km |
| `30` | warm-ups since codes cleared | 2 |
| `31` | distance travelled since codes cleared | 19 km |
| `42` | control module voltage | 13.712 V |
| `A6` | **odometer** | **2146.6 km** |

`A6` is the headline result: the vehicle had been advertising an odometer all
along and the old fixed probe list never asked for it. The four-byte,
0.1 km-per-bit decoding was written before the reading was taken and matched on
the first attempt.

`01` remains deliberately undecoded. It is a composite of MIL state and
readiness monitors and the scalar sample shape cannot represent it honestly;
its raw bytes are preserved in the transcript.

### Service 09 vehicle information

`0900` advertises four items, and all four answered:

| Item | Result |
|---|---|
| `0902` VIN | 17 characters, decoded, reported only masked |
| `0904` calibration IDs | returned from every responding module |
| `0906` calibration verification numbers | returned from 6 modules |
| `090A` ECU names | full module names, listed below |

`0906` exposed a real reporting defect. CVNs are four-byte binary values, and
the probe was rendering them through the ASCII item decoder, which produced
noise that read like a decode failure on a perfectly good reply. The probe now
routes `0906` through `decode_cvns`.

### Module inventory

Service 09 PID `0A` named all eight responding modules:

```text
Gateway Module - GWM     BCM-BodyControl
BSM-BatterySysMngr       BSCM-BrakeSystem
DMCM-DriveMotorCtrl      BSM-BatterySysMngr
DMC2-DriveMotorCtrl2
DMC3-DriveMotorCtrl3
```

Three drive-motor controllers and two battery-system managers, consistent with
a tri-motor Ultium truck. The mapping from these names to the eight responding
CAN addresses (`17 1D 1E 28 40 45 CB CD`) is still unknown; closing it needs
`ATCRA` receive filtering, which is already allowlisted.

### Bounded collector trial

The staged-trial path was exercised for the first time: a separate trial
configuration polling `010D`, `0142`, `011F`, `01A6` and `0131` every two
seconds, with `duration_s` set so the run stops itself. The deployed
`config/hummer.toml` was not modified, and `collector.enabled` in it remains
false.

## Drive trial and odometer cross-validation

On 2026-09-01 the collector ran for the first time against a **moving** vehicle,
in two bounded sessions totalling about twenty minutes. Polling a vehicle that
is awake and running its DC-DC converter does not interact with the sleep gate;
the gate is about parasitic drain on a *sleeping* vehicle, which is why the run
was time-boxed and stopped the moment the truck parked.

| Measurement | Result |
|---|---|
| Vehicle speed | 0 to **94 km/h**, 539 valid samples |
| Odometer (`A6`) | 2146.6 to **2157.2 km** |
| Distance since codes cleared (`31`) | 19 to **29 km** |
| Control module voltage (`42`) | **12.55 to 13.781 V** (dips under load) |
| Warm-ups since codes cleared (`30`) | incremented 2 to 3 during the drive |
| Distance with MIL on (`21`) | constant 0 km |
| OBD standard code (`1C`) | constant 5 |
| DTC reads | 4 each of services 03/07/0A, **zero codes throughout** |

### The odometer decoder is now independently corroborated

Two unrelated counters moved together:

```text
A6 odometer                    2146.6 -> 2157.2 km   delta 10.6 km
31 distance since codes cleared    19 ->      29 km   delta 10   km
```

`A6` has 0.1 km resolution and `31` has 1 km resolution, so a 0.6 km
disagreement is exactly what agreement looks like at those resolutions. The
four-byte, 0.1 km-per-bit scaling was written from the SAE definition *before*
any reading was taken, and it now matches a second, independently-scaled
counter over a real distance. That is stronger evidence than a single
plausible-looking value.

Speed distribution over the drive, in 10 km/h bands:

```text
  0-  9  ######################################## 237
 10- 19  ######## 32
 20- 29  ######## 32
 30- 39  ###### 27
 40- 49  ###### 27
 50- 59  ######## 33
 60- 69  ########## 43
 70- 79  ################ 67
 80- 89  ######## 32
 90- 99  ## 9
```

PID `01` was polled 220 times and recorded 220 times with status `undecoded`.
That is the intended behaviour: the raw bytes are preserved and no value is
invented for a composite the sample shape cannot represent.

### Honest note on collection continuity

There was a gap between the two sessions, and it is worth being precise about
both its size and its cause, because the obvious reading of each is wrong.

The gap was **66.9 seconds**, measured between the last sample of one session
and the first of the next. It was described in conversation at the time as
"about three minutes", from memory of the timeline rather than from the data.
The coverage report built in response to this incident is what produced the
real figure, which is a reasonable argument for having built it.

Collection is entirely local. Each response is written to an append-only JSONL
transcript and `os.fsync`'d before it is parsed, and decoded samples go to a
WAL-mode SQLite database on the node's own disk. Nothing in that path touches
the network, so a dropped access point cannot lose a sample.

The gap was an operating mistake, not a storage or a network failure. The trial
had been started as a background process over SSH rather than under systemd.
When a wider PID set was wanted, a single remote command stopped the running
collector and then started a new one; the link dropped between those two steps,
so the stop succeeded and the start never ran. Nothing was supervising the
process, so nothing restarted it.

Two changes followed:

- `systemd/hummer-collector-trial.service` -- a supervised, bounded trial unit.
  It survives an SSH session closing, restarts the collector on failure, caps a
  restart loop at five starts per hour, caps each start at two hours regardless
  of configuration, does not restart a clean exit, and has no `[Install]`
  section so it cannot be enabled at boot. The runbook now tells operators to
  use it and says plainly not to use `nohup` over SSH.
- The capabilities report gained a collection-coverage section, so a gap is
  visible and auditable from the local database rather than noticed by
  accident. Run against the reference node it reports the two real gaps: the
  66.9 second one above, and the expected 85 minute idle period between the
  parked evening probe and the drive.

Remote changes are now also sent as a single atomic payload so they cannot
half-apply.

## Sleep observation

With the vehicle parked, all vehicle polling was stopped and a **zero-CAN-traffic**
watch started instead (`hummer-obd-voltage`). It sends only `ATZ`, `ATE0`,
`ATRV` and `ATCS` — adapter commands that never reach the bus — every five
minutes.

Baseline immediately after parking:

```text
2026-09-02T00:50:29Z  13.9 V  T:00 R:00  ok
```

13.9 V is the DC-DC converter still running. The second sample, five minutes
later, caught the transition:

```text
2026-09-02T00:50:29Z  13.9 V  T:00 R:00  ok    DC-DC running
2026-09-02T00:55:34Z  12.7 V  T:00 R:00  ok    DC-DC off, vehicle asleep
```

**The vehicle reached sleep normally, roughly five minutes after parking, with
the Pi and the OBDLink attached and drawing from an always-live port.** No CAN
traffic was generated to observe it: the drop from an alternator/DC-DC voltage
to a resting battery voltage is visible from the connector alone.

This answers step 3 of the safe-next-milestone list for the *attached hardware*
case. It does **not** open the gate, and two questions remain:

1. does the 12 V rail hold at rest, or decay measurably over hours with the Pi
   and adapter attached? That is the parasitic-drain question, and it needs
   wall-clock time rather than more instrumentation; and
2. does the vehicle still reach sleep while the collector is *actively
   polling*? Tonight's observation had polling stopped, which is the correct
   control but is not the condition the gate is really about.

Until both are answered the accepted state is unchanged:

```text
collector.enabled = false
hummer-collector.service = disabled / inactive
```

## Second drive, and the supervised trial unit in service

A return drive on 2026-09-01 was collected through the supervised trial path
rather than a hand-started background process. Cumulative results across both
drives:

| Measurement | Result |
|---|---|
| Vehicle speed | 0 to 94 km/h, 961 valid samples |
| Odometer (`A6`) | 2146.6 to **2166.2 km** |
| Distance since codes cleared (`31`) | 19 to **38 km** |
| Control module voltage (`42`) | 12.55 to 13.898 V |
| Warm-ups since codes cleared (`30`) | 2 to 4 |
| DTCs | zero codes throughout |

The odometer cross-check now holds over a longer distance: `A6` moved 19.6 km
while `31` moved 19 km, which is agreement at 1 km resolution. The collector
stopped itself cleanly on request after 422 cycles.

### Supervision, installed

`hummer-collector-trial.service` is now installed on the reference node.
`systemctl is-enabled` reports **`static`**, which is the intended result: the
unit has no `[Install]` section, so it cannot be enabled to start at boot. That
property is now demonstrated on the real system rather than asserted in a
comment.

Installing it exposed a gap in `scripts/deploy.sh`. The script copied
`config/hummer.example.toml` by name, so `config/hummer-collector-trial.default`
- added in the same change as the unit - never reached the node, and the
install failed on a missing file. The script now ships every config *template*
(`*.example.toml` and `*.default`) while still never touching the node's live
`config/hummer.toml`.

Two attempts to install the unit earlier, during the drive, failed outright:
`sudo` over a phone hotspot did not survive long enough to complete. That is
the same class of failure as the original collection gap, and it is why
`scripts/run_trial.sh` exists as a rootless fallback with the same bounds.

## Maximal capability expansion, 2026-09-01

Three capability gaps were closed. None of them was a vehicle limitation; all
three were limitations of this software.

### Services 02 and 06 added to the gate

`ALLOWED_OBD_MODES` became `{01, 02, 03, 06, 07, 09, 0A}`. Both additions are
standard SAE J1979 read services and needed no guessed identifier, which is
what distinguishes them from Mode 22. The full change-control record is in
[Safety](SAFETY.md). `FORBIDDEN_SERVICES` is unchanged, the allowlist and the
denylist are asserted disjoint, and service 02 was given its own request shape
rather than relaxing the existing one-parameter rule.

Status: **permitted, not yet proven on this vehicle.** The truck was asleep
when they were added and no live request has been made.

### Seven of every eight readings were being discarded

`decode_pid` returned the first matching frame and dropped the rest. On this
vehicle a single `0142` request is answered by eight modules, each with its own
supply voltage. Replayed against the transcript already on the node:

```text
ecu 45  13.747 V     ecu CB  13.693 V
ecu 17  13.571 V     ecu 1D  13.500 V
ecu 40  13.875 V     ecu 1E  13.524 V
ecu CD  13.726 V     ecu 28  13.910 V
```

A 0.41 V spread, which is harness voltage drop between modules and a real
measurement. `decode_pid_per_ecu` now returns one value per responding module
and `samples.ecu` records which module produced it. `decode_pid` is unchanged
for existing callers.

### Schema migration, verified against the live database

The `ecu` column and a `monitor_tests` table required schema version 2. The
node holds readings nobody can take again, so the migration only ever adds
(`ALTER TABLE ... ADD COLUMN`, `CREATE TABLE IF NOT EXISTS`) and never drops,
renames or rebuilds.

It was verified against a **copy of the reference node's real database**, not a
fixture:

```text
samples        7384 rows  identical
dtc_reads        18 rows  identical
sessions          5 rows  identical
vehicle_info      1 rows  identical
events            5 rows  identical
EVERY ORIGINAL COLUMN AND ROW IDENTICAL: True
schema_version: 2      ecu column present: True
ecu default on old rows: ''      monitor_tests exists: True
```

Comparing the original columns explicitly matters: a `SELECT *` check reports a
difference purely because the new column exists, which looks like data loss and
is not.

### `--max` probe

`hummer-obd-probe --max` asks for everything the vehicle advertises: per-module
attribution of every PID, service 06 monitor discovery and results, an ECU
address-to-name map via `ATCRA` receive filtering, and freeze frames **only**
when a DTC read actually returned codes. The default probe path is unchanged.

This has **not been run on the vehicle yet.** It is the highest-value
outstanding action and needs the truck awake.

## Maximal probe on a live vehicle, 2026-09-01

`hummer-obd-probe --max` was run against the awake vehicle. Charging keeps the
modules up, so the diagnostic bus answered even though the truck was parked.

### The definitive module map

This was the prerequisite for any future per-module work, and it is now
measured rather than inferred. Each address was queried behind its own
`ATCRA18DAF1<addr>` receive filter, so every name is attributed with certainty:

| Address | Module |
|---|---|
| `17` | `DMCM-DriveMotorCtrl` |
| `1D` | `DMC2-DriveMotorCtrl2` |
| `1E` | `DMC3-DriveMotorCtrl3` |
| `28` | `BSCM-BrakeSystem` |
| `40` | `BCM-BodyControl` |
| `45` | `Gateway Module - GWM` |
| `CB` | `BSM-BatterySysMngr` |
| `CD` | `BSM-BatterySysMngr` |

**Correction to an earlier record.** This document previously stated that
address `28` was the gateway, inferred from it being the only module answering
`7F <service> 22` while the vehicle shut down. That was wrong. `28` is the
**brake system control module**; the gateway is `45`. The inference was
confirmed wrong twice: by the name map above, and again when the vehicle
returned to sleep during a later run and `28` was once more the sole responder.
So the module that stays reachable longest during shutdown is the brake system
controller, which is a more interesting fact than the one that replaced it.

### Service 06: supported, and empty

`0600` returned a **positive response advertising zero monitor IDs**. Service 06
is therefore proven to work on this vehicle and this vehicle exposes no
on-board monitors through it. That is a real answer, not a failure: an empty
supported-MID bitmap is the ECU saying it has nothing to report.

### Service 02: still unproven, and possibly unprovable here

Freeze frame was **correctly skipped**, because all three DTC reads returned
zero codes and a freeze frame only exists because a code was set. The
behavioural gate worked exactly as designed. It also means service 02 cannot be
proven on this vehicle while it stays healthy, which is the right problem to
have.

### Per-module readings, stored

Control module voltage from all eight modules in one request, now written to
SQLite with the module recorded against each row:

```text
ecu 28  13.957 V     ecu 40  13.861 V
ecu CD  13.720 V     ecu 45  13.719 V
ecu CB  13.678 V     ecu 17  13.555 V
ecu 1E  13.510 V     ecu 1D  13.493 V
```

A 0.464 V spread, highest at the brake controller and lowest at a drive motor
controller. The database now holds 23 rows carrying a module address across
eight distinct modules.

### Calibration verification numbers

`0906` returned **42 CVNs**, correctly rendered as four-byte hex values.
Previously these went through the ASCII item decoder and came back as
unreadable noise that looked like a decode failure on a perfectly good reply.

### Two malformed rows, disclosed rather than deleted

A transient bug in the probe's database path wrote two `vehicle_info` rows
holding the Python `repr` of a list and of a dict (`ecu:addresses`,
`ecu:names`) instead of one row per module. The bug is fixed and the correct
form is one `ecu:<address>` row per module. The two bad rows remain in the
reference database: this storage layer never deletes, and quietly removing
evidence of a mistake is a worse habit than leaving two identifiable rows.

### Review outcome: one scaling row removed

The adversarial review could not confirm unit-and-scaling identifier `0x24`
against the J1979 table. The argument for keeping it was that a multiplier of
1.0 cannot change a magnitude, which is circular — it only holds if 1.0 is
correct. It was removed. An identifier the table does not know now yields a
null scaled value with the raw counts intact, which is the whole point of
keeping the table deliberately partial.

The review also established that monitor values are read big-endian
**unsigned**, that J1979 defines some identifiers as signed, and that none of
those is currently in the table. That is documented rather than guessed at; a
future signed row must carry its own signedness.

## Service 02 on a vehicle with no faults

Service 02 could not be demonstrated by the maximal probe, because a freeze
frame only exists if a trouble code was set and this vehicle has none. That is
not a defect to fix; it is a property of a healthy truck. **Inducing a fault to
exercise a decoder is not something this project will do.**

There is one request that is still worth making, and the probe now always makes
it: `020000`, the freeze-frame support bitmap. It asks what a frame *would*
contain rather than what one does contain, so it exercises the whole service 02
path — request shaping, the frame byte, parsing — without needing a fault
first. Only the per-PID frame reads stay conditional on a code existing.

Requested live on 2026-09-01 while the vehicle was shutting down:

```text
0100    18DAF128 03 7F 01 22
020000  18DAF128 03 7F 02 22
0202    18DAF128 03 7F 02 22
```

This is more informative than it looks. `7F 02 22` is a negative response to
**service 02** with code `conditionsNotCorrect` — the same code service 01 got
in the same breath, and service 01 is definitely supported on this vehicle.
An unsupported service answers `7F 02 11` (`serviceNotSupported`), which is
**not** what came back.

So the request is transmitted, reaches the vehicle, and is recognised by the
responding module as service 02; it is declined for vehicle-state reasons
exactly as service 01 is. That is not a positive response and does not make
service 02 "proven", but it does establish that nothing between this code and
the ECU is wrong. The remaining gap is a vehicle condition, not a code path.

### Proven, on the next awake run

The positive response arrived on the following `--max` run, with the vehicle
switched on:

```text
020000: ok -> 4 pids
020000 supported: 02 0D 1F 20
```

**Service 02 is proven on this vehicle.** The freeze-frame support bitmap
advertises four PIDs, so if this truck ever sets a diagnostic trouble code the
snapshot it keeps will contain:

| PID | Meaning |
|---|---|
| `02` | the DTC that caused the freeze frame |
| `0D` | vehicle speed at the moment it set |
| `1F` | run time since start at the moment it set |
| `20` | support bitmap pointer for PIDs 21-40 |

That is the whole of what a freeze frame would hold here, known in advance and
without the truck having to develop a fault to find out. It is a small set, and
knowing it is small is itself the useful result: a freeze frame on this vehicle
will not carry pack or thermal state.

The decode side is separately verified offline: a service 02 bitmap carries an
extra frame byte before the four bitmap bytes, and a test asserts that the same
bitmap bytes decode identically through service 01 and service 02. Reading them
one byte out would have advertised a set of PIDs the vehicle never claimed.

## Schema version 3, and PID 01 settled offline

### The migration

Schema v3 adds durable polling cycles, a module-identity table, and per-module
DTC / monitor-status / service-09 tables. One additive migration, following the
v1-to-v2 structure exactly: every step individually guarded, version bump last,
so a v1 and a v2 database upgrade through the same code path and neither can be
half-applied.

Verified against a **copy of the reference node's real database** before the
original was touched:

```text
samples        7440 rows  identical
dtc_reads        27 rows  identical
sessions          9 rows  identical
vehicle_info     16 rows  identical
events            5 rows  identical
monitor_tests     0 rows  identical
EVERY ORIGINAL ROW AND COLUMN IDENTICAL: True
version 2 -> 3      cycle_id on history: NULL (not 0)
```

The backfill took exactly the eight real module addresses and excluded the two
malformed `vehicle_info` rows (`ecu:addresses`, `ecu:names`) that a transient
bug wrote and that this record already discloses. Idempotent across two opens.

The test fixture builds a v2 database the way the node's really was — v1 schema
then `ALTER TABLE` — because a flat schema string would declare `ecu` mid-table
and the byte-identity test would then be proving a shape the node does not
have. Confirmed against the live `.schema`, where `samples.ecu` sits after
`uploaded_at`.

### PID 01 decoded, and cross-checked without touching the vehicle

Service 01 PID 01 was previously stored as `undecoded`, because it is a
composite of MIL state and readiness monitors that a scalar cannot represent.
It now decodes into `monitor_status` plus one `monitor_readiness` row per bit.

The decoder was validated **offline against the frames already on the node** —
no vehicle access required. Every module returned the same reply:

```text
41 01 00 04 00 00
```

| Field | Value |
|---|---|
| MIL | off |
| DTC count | 0 |
| Supported monitors | **1 of 11** — `components` only |
| Ignition table | spark |

Three independent cross-checks agree, which is what makes this a validation
rather than a plausible reading:

1. the DTC count of zero matches the zero codes services 03/07/0A returned in
   the same sessions;
2. MIL off matches PID `21` reporting 0 km travelled with the lamp on; and
3. a single supported monitor is exactly what a battery-electric vehicle should
   report — no catalyst, oxygen sensor, evaporative or misfire monitors exist
   to run.

Bytes C and D are both zero, so the one genuinely ambiguous bit in the standard
(spark byte B bit 4, called A/C refrigerant by older references and reserved by
J1979-DA, and named `reserved_b4` here rather than guessed) never arises on
this vehicle.

### Adaptive collection policy

`policy.py` is a pure state machine with no I/O, so every transition is testable
without a fake transport. It is **not wired into the collector yet** and ships
inert.

Review caught one defect worth recording: `wake_volts` defaulted to 13.0 V,
which sits exactly on top of the observed asleep band (12.7–13.0 V) with a
`>=` comparison — so a resting vehicle would have been read as awake and OBD
polling re-enabled on a truck that was merely parked. It is now 13.4 V, bounded
on both sides by real readings: above the highest resting value and below the
13.9 V running value. It is still not calibrated, and the honest version comes
from trending `ATRV` across a full sleep period.

## Overnight 12 V measurement, 2026-09-02

The measurement the continuous-collector gate has been waiting on. Every
previous attempt was pinned at 13.9 V because the vehicle was plugged in and
measuring nothing; this one crossed into rest and stayed there for nearly seven
hours.

### Conditions

Vehicle polling processes: **zero**. `hummer-obd-voltage` sends only `ATZ`,
`ATE0`, `ATRV` and `ATCS`, asserted adapter-only at import, so nothing this
node did during the window reached the CAN bus. `ATCS` read `T:00 R:00` on
**all 127 samples**, which is the independent confirmation: the adapter
transmitted nothing and logged no bus errors for the entire measurement.

The watch kept running after this section was first written, and the complete
trace was retrieved from the node on 2026-09-02 once it came back online. It
holds **158 samples**, every one of them `T:00 R:00` with status `ok` and no
transport failure, so the zero-traffic claim now covers the longer file rather
than the 127-sample prefix it was written against. The figures below are
unchanged: they were re-derived from the full trace and reproduce exactly.

### Result

```text
05:49Z  13.9 V   DC-DC running
05:54Z  13.8 V   falling
05:59Z  12.5 V   DC-DC off, vehicle asleep
06:29Z  12.9 V   surface charge relaxed
...
12:45Z  12.8 V   still asleep, 6.8 h later
12:50Z  13.5 V   powered up again
```

| Measure | Value |
|---|---|
| Asleep window | 05:59Z to 12:45Z, **6.77 hours**, 82 samples |
| Range while asleep | 12.50 V to 12.90 V |
| Mean while asleep | 12.84 V |
| Settled slope (first 30 min excluded) | **-15.9 mV/hour** (12.90 to 12.80 V over 6.27 h) |
| Final resting voltage | **12.80 V** |

### A second sleep cycle, from the same trace

The full 158-sample file shows what happened next, and it is worth recording
because it is an *independent repeat* rather than more of the same window:

```text
12:45Z  12.8 V   end of the window measured above
12:50Z  13.5 V   brief power-up
12:55Z  13.5 V   still awake
13:00Z  12.7 V   asleep again
15:28Z  12.8 V   still asleep, 2.5 h later
```

| Measure | Sleep window 1 | Sleep window 2 |
|---|---|---|
| Span | 05:59Z-12:45Z, 6.77 h | 13:00Z-15:28Z, 2.47 h |
| Samples | 82 | 31 |
| Range | 12.50-12.90 V | 12.50-12.90 V |
| Settled slope (first 30 min excluded) | -15.9 mV/hour | **+0.0 mV/hour** (12.80 to 12.80 V over 1.97 h) |

The second window is the more interesting number. It is **flat** -- 12.80 V at
both ends, no measurable fall across two hours -- which is what the section
above predicted on physical grounds: resting voltage flattens once the surface
charge has relaxed, so a slope taken from a freshly-parked battery is mostly
relaxation and a slope taken later is mostly nothing. Two sleep cycles in one
day with the hardware attached, the second showing no measurable decline at
all, is a stronger result for the collector gate than either window alone.

**A caution about this trace.** The node has no RTC (`timedatectl` reports
`RTC time: n/a`), so its clock is set from NTP after boot. These timestamps are
trustworthy because the watch ran unbroken and ended *before* the node lost
power, but a trace that spans a reboot should not be assumed to be continuous
in time. The 18.5-minute gap at 02:52Z-03:11Z is the only interruption in this
one, and it is inside the awake period rather than either sleep window.

### What this does and does not establish

**Established.** The vehicle reaches sleep and *stays* asleep for seven hours
with the Pi and the OBDLink attached to an always-live port -- and, per the
second window above, does so again on the same day after a brief wake, which
rules out the first result being a one-off. The 12 V rail
ends the window at 12.80 V — squarely inside the healthy resting range for a
12 V lead-acid battery, not sagging. Nothing about the attached hardware
prevented sleep or visibly depleted the rail overnight.

**Not established: a parasitic-current figure.** A 6.8-hour window cannot
cleanly separate true drain from surface-charge relaxation, and the trace shows
exactly why: the voltage *rose* 0.4 V over the first half hour after the load
came off, which is relaxation, not charging. Reporting -15.9 mV/hour as a drain
rate would be reading a battery's own settling curve as a current draw, and
extrapolating it linearly to a day or a week would be worse — resting voltage
flattens, it does not fall at a constant slope. A real figure needs either a
much longer window or a current measurement in series with the supply.

**Still not established: sleep while actively polling.** This window had
polling stopped, which is the correct control and is not the condition the gate
is actually about.

### A note on the wake reading

The vehicle powered up to **13.5 V**, not the 13.9 V previously recorded with
the DC-DC converter running. The lower value may mean a charger engaging rather
than the vehicle entering Ready. It is recorded rather than interpreted.

That reading did, however, calibrate a threshold. `policy.wake_volts` had
defaulted to 13.0 V, which this trace shows sitting only 0.1 V above the
observed asleep maximum of 12.90 V — uncomfortably tight. It was raised to
13.4 V on review beforehand, and the measurement supports that: 13.4 sits 0.5 V
above the asleep maximum and below the 13.5 V wake.

## Resource result

The reference image was reduced from a desktop installation to a conservative
headless node while preserving networking, SSH, private VPN, Bluetooth,
Avahi/mDNS, SPI/GPIO, and all project dependencies:

| Metric | Validated result |
|---|---|
| Installed packages | 1656 to 1343 |
| Root filesystem | 4.2 GiB used of 29 GiB; 24 GiB free |
| Running services | 18 at the checkpoint |
| Failed services | 0 |
| Memory | 146 MiB used immediately after cleanup; about 200 MiB under active validation load |

The memory figures are workload snapshots, not guarantees.

## Open acceptance gate

Continuous collector autostart is **not accepted yet**, and the reason is now
narrower than it was.

**What has been observed.** With the Pi and the OBDLink attached to an
always-live OBD-II port and *no polling running*, the vehicle reached sleep
normally about five minutes after parking: 13.9 V with the DC-DC converter
running, 12.7 V once it stopped. That was measured with `hummer-obd-voltage`,
which sends only adapter commands and puts **zero bytes on the CAN bus**, so
the observation cannot have influenced what it observed.

**What is still unproven, and why the gate stays shut.**

1. ~~**Overnight 12 V stability.**~~ **Answered 2026-09-02.** The vehicle slept
   for 6.8 hours with the hardware attached and the rail ended at 12.80 V,
   inside the healthy resting range, with zero CAN traffic from this node
   throughout. What remains unquantified is a parasitic-*current* figure, which
   a window that short cannot separate from surface-charge relaxation — but
   nothing in the trace suggests a problem.
2. **Sleep behaviour while actively polling.** Every sleep observation to date
   had polling stopped. That is the correct control, but it is not the
   condition the gate is actually about: whether a continuous diagnostic loop
   keeps modules awake is exactly the question, and it has not been asked.

Until both are answered the correct state is:

```text
collector.enabled = false
hummer-collector.service = disabled / inactive
```

**The PiSugar2 pack does not change this measurement.** `ATRV` reads voltage at
the J1962 connector, which is the vehicle's 12 V rail, not the Pi's cell; the
drain question is unaffected. What the pack does change is the *consequence* of
a sag: the Pi now rides it out on its own cell and shuts down cleanly instead
of losing power mid-write, so the SD card and the SQLite database are no longer
hostage to the vehicle's rail.

This is an electrical/power-management gate, not a software test failure. The
tooling for the next step exists: `hummer-collector-trial.service` runs a
supervised, self-stopping trial, and `hummer-obd-voltage` can watch the rail
afterwards without touching the bus.

## Expanded command capability probe

At the owner's request, an exact-command mode was added and tested. It validates
the complete operator-supplied list before opening the serial device, preserves
command order, masks VIN output, and records byte-exact TX/RX data. A regression
test proves that one unsafe entry rejects the whole set before any byte is sent.

The following list was run on 2026-09-01, followed by a second `ATDP`/`ATDPN`
pair after protocol detection:

```text
ATZ ATE0 ATL0 ATS0 ATH1 ATAL ATSP0 ATDP ATDPN ATRV
0100 0120 0140 0160 0180
0900 0902 0904 0906
03 07 0A 0142 010D 015B
ATDP ATDPN
```

The successful transcript contains 27 request/response pairs, 58 JSONL lines,
15,677 bytes, zero corrupt records, and SHA-256:

```text
8aa78d77ab859708e237a6a564e94a1937c5ac0a8ab2e10b0cfc0302b3e1f805
```

The adapter was healthy and the vehicle was not in a data-serving state:

| Command group | Exact result |
|---|---|
| `ATZ` | echoed command, then `ELM327 v1.4b` |
| `ATE0`, `ATL0`, `ATS0`, `ATH1`, `ATAL`, `ATSP0` | `OK` |
| `ATDP` / `ATDPN`, before an OBD request | `AUTO` / `A0` |
| `ATRV` | `12.7V` |
| `ATDP` / `ATDPN`, after `0100` | `AUTO, ISO 15765-4 (CAN 29/500)` / `A7` |
| Mode 01 commands | `18DAF128 03 7F 01 22` |
| Mode 09 commands | `18DAF128 03 7F 09 22` |
| Mode 03, 07, 0A | `18DAF128 03 7F <service> 22` |

`7F <service> 22` is a negative response with code `0x22`
(`conditionsNotCorrect`). This proves that the request reached the vehicle
gateway, but the gateway would not serve standard diagnostic data in the
current ignition/sleep state. It is not `NO DATA`, a valid empty DTC response,
or a positive PID response. The parser and reviewer now classify this form
explicitly rather than reporting a structurally valid hex frame as `ok`.

No clear, control, write, security, enhanced Mode 22, or other forbidden
request appeared in the transcript.


## Passive CAN capture at the diagnostic connector, 2026-09-04

**Result: zero bytes in thirty seconds.** This is the decisive negative that
[PASSIVE_CAN_VALIDATION.md](PASSIVE_CAN_VALIDATION.md) predicted and asked to
have published whichever way it came out, and it is worth more than everything
in that document's findings table, because it is first-hand and about this
truck.

### Conditions

Parked, awake, stationary, accessory load about 3.9 kW. Pack 380.6 V at 75.4 %
state of charge, 12.9 V at the connector. The recorder was stopped and its
sessions pulled to a workstation first; it was restarted immediately afterwards
and resumed writing rows in a new session.

### What was transmitted, in full

Thirteen writes, sixty-five bytes, every one of them adapter configuration.
Reconstructed from the transcript's `tx` records rather than from the program's
intentions:

```text
ATZ  ATE0  ATL0  ATS1  ATH1  ATAL  ATSP7  ATCAF0  ATCS  STCMM0
STMA
<CR>
ATCS
```

`ATSP7` pins ISO 15765-4, 29-bit, 500 kbit/s rather than letting the adapter
auto-detect, because auto-detection discovers a protocol *by transmitting*.
`STCMM0` is receive-only monitoring: the adapter does not assert the dominant
acknowledgement bit that a normal CAN node puts on every frame it hears, so the
claim is "we did not transmit", not merely "we did not request". The lone `<CR>`
is the stop character, which travels over the UART to the adapter and not onto
CAN. Nothing outside that manifest appears in the transcript.

### What arrived

| | |
|---|---|
| Bytes received during the 30 s window | **0** |
| Raw-log records written during the window | 0 |
| Elapsed | 30.064 s, ended by the time bound, not the byte bound |
| Stop acknowledged by the adapter | yes |
| `ATCS` before | `T:00 R:00` |
| `ATCS` after | `T:00 R:00` |
| DTCs before (`03` / `07` / `0A`) | ~~none / none / none~~ — **`NO DATA` from all three**, see the correction below |
| DTCs after (`03` / `07` / `0A`) | ~~none / none / none~~ — **`NO DATA` from all three**, unchanged either side |
| Driver information centre | no new message |

Transcript: 32 JSONL records, 7,949 bytes, zero corrupt records, SHA-256

```text
aa3e57e59cdd94a43de267a25de01dede9aed5e6b00eba7aa7c9782d6c8b83af
```

The transmit error counter never moved off zero, which is the stop condition
that mattered most: it is consistent with having transmitted nothing, though it
is not by itself proof of it. The manifest above is the stronger evidence.

> **Correction, later the same day.** The DTC rows originally read
> "none / none / none". That was wrong, and wrong in the specific way this
> project warns about at length: all three services returned **`NO DATA`**, and
> `NO DATA` means *nothing replied* — it says nothing whatever about whether
> fault codes exist. The comparison either side of the capture is still valid,
> because it compares like with like, but "no DTCs" was not what had been
> measured. What had been measured was silence at that addressing.
>
> A genuine answer arrived at 01:50 UTC — see
> [the DTC finding](#dtcs-were-never-actually-read-until-2026-09-04) below.

### What this establishes, and what it does not

**Establishes.** The gateway forwards nothing unsolicited to pins 6 and 14 on
this vehicle, in this state, to a classical-CAN listener. Every byte this
project has ever obtained from this truck arrived because something asked for
it. There is no free stream to fall back on, and the research note's premise —
that passive monitoring might be a route around the enhanced-identifier problem
— is now measured rather than argued, and it is false here.

**Does not establish.** Nothing about whether the vehicle's internal networks
are busy; they certainly are, behind the gateway. Nothing about other states —
this was one thirty-second capture, parked and awake, and driving or charging
were not tested. Nothing about CAN FD, which this adapter cannot see at all.
And nothing about *why*: filtering, authentication, encryption and simple
silence all look identical from here.

**What it retires.** "Try sniffing the DLC" is no longer an open idea to be
picked up again on a slow evening. It was tried, under the conditions the
research note specified, and it returned nothing. The next person to have the
idea can read this row instead of spending a week on it.


## DTCs were never actually read until 2026-09-04

Every DTC check in this project's history returned `NO DATA`, and every one was
recorded as "no fault codes". That is the exact error
[the failure-shape rule](ACCESS_MATRIX.md#4-the-three-ways-a-request-fails)
exists to prevent, made by the person who wrote the rule, on the same day.

### What happened

Module `CD` and module `45` were being re-confirmed first-hand rather than cited
from a document. The gateway profile addresses module `45` — `ATSHDA45F1`,
`ATCRA18DAF145` — and leaves the adapter pointed there. The DTC read that
followed therefore went to module `45` specifically, and it answered:

```text
before (probe default addressing)   03 -> NO DATA        07 -> NO DATA        0A -> NO DATA
after  (addressed to module 45)     03 -> 18DAF145024300 07 -> 18DAF145024700 0A -> 18DAF1450 24A00
```

Decoded, the "after" frames are unambiguous positives:

| Service | Frame | Meaning |
|---|---|---|
| `03` stored | `18DAF145 02 43 00` | positive response `0x43`, **DTC count 0** |
| `07` pending | `18DAF145 02 47 00` | positive response `0x47`, **DTC count 0** |
| `0A` permanent | `18DAF145 02 4A 00` | positive response `0x4A`, **DTC count 0** |

**So the vehicle really does have zero fault codes** — that part of the old
conclusion survives. What did not survive is the evidence for it. Until this
frame arrived, the claim rested on silence.

### Why the difference

Addressing, not vehicle state. The probe's default DTC path did not reach a
module willing to answer service `03`; addressed explicitly to the gateway, all
three services answered immediately. This is the same lesson as
[CAN priority](CAN_PRIORITY.md): when something is silent, **vary how
you are asking before concluding anything about what you asked for.** Thirteen
identifiers at one priority was one data point about the priority. Three
services at one addressing was one data point about the addressing.

### What was corrected

* The passive-capture table above, which said "none / none / none".
* `docs/TELEMETRY_CATALOG.md`, which graded DTC reads **measured** on the
  strength of `NO DATA`. The grade is now correct for the first time, for a
  different reason than it was originally given.
* `hummer_obd.access`, whose freeze-frame entry cited the same non-evidence.

### Also confirmed in the same run, first-hand

Both were previously carried from `docs/PROBE_2026-09-03.md` rather than
measured in the session that cites them:

| Claim | Result, 2026-09-04 01:50 UTC |
|---|---|
| Module `CD` refuses everything at priority `0x18` | `22F187`, `2227C6`, `222AF5`, `222AF1` → **all `7F 22 31`** from `18DAF1CD`. Confirmed. |
| Module `45` holds none of the four ISO identifiers | `22F187`, `22F188`, `22F189`, `22F191` → **all `7F 22 31`** from `18DAF145`. Confirmed. |

A formed `7F 22 31` from a module's own address is the strongest available
proof that it is present and speaking service 22 — better than a positive
response would be, because it also shows the module parses the request rather
than echoing it. Both modules are reachable and neither holds anything asked of
them.
