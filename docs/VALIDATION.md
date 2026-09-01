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
pytest:   126 passed
unittest: 126 tests / 150 subtests passed
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
- all 126 tests plus the safety-gate smoke check passed on the Pi.

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
