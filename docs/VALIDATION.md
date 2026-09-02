# Validation record

Last hardware-backed validation: 2026-09-01.

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

Latest result:

```text
pytest:   237 passed
unittest: 237 tests / 223 subtests passed
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
| `7F <service> 22` from the gateway only | gateway alive, refusing: `conditionsNotCorrect` |
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

Continuous collector autostart is **not accepted yet**. No full vehicle
sleep/wake cycle or ignition-switched Pi power behavior has been documented.
Until that physical test is complete, the correct state is:

```text
collector.enabled = false
hummer-collector.service = disabled / inactive
```

This is an electrical/power-management gate, not a software test failure.

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
