# Capabilities

What this node can actually do, what the connection actually exposes, and what
is deliberately out of reach. Every claim here is either backed by a byte-exact
transcript on the reference node or is explicitly marked unproven.

Last updated from live evidence: 2026-09-01.

## How to read this document

Every capability is in exactly one of three tiers.

| Tier | Meaning |
|---|---|
| **Proven** | Observed on this vehicle, with a preserved raw transcript |
| **Available, unproven** | The code and the safety gate allow it; it has never been run, or it has never answered |
| **Not available** | Blocked by policy, absent from this vehicle, or outside this project's scope |

"Proven" is deliberately strict. A command that returned `NO DATA` or a
negative response is evidence about the *vehicle*, not proof of a capability.

## 1. The connection

```text
Hummer EV diagnostic bus
  └─ SAE J1962 connector, pin 16 permanently live      [proven, section 6]
      └─ OBDLink MX+  (STN2255 chipset)
          └─ Bluetooth Classic, Serial Port Profile, channel 1
              └─ Raspberry Pi Zero 2 W, /dev/rfcomm0 @ 115200 8N1
                  └─ safety.validate_command()  ← every byte passes here
                      └─ append-only JSONL transcript, then decode, then SQLite
```

| Property | Value | Tier |
|---|---|---|
| Adapter | OBDLink MX+ r3.1.3 | Proven |
| Chipset firmware | STN2255 v5.12.4 (2025.12.15) | Proven |
| ELM compatibility layer | ELM327 v1.4b | Proven |
| Bootloader | 4.4 | Proven |
| Manufacturer string | OBD Solutions LLC | Proven |
| Bluetooth modem | BT24H, R15 | Proven |
| Link | Bluetooth Classic SPP, bonded and trusted, one SDP-confirmed Serial Port record | Proven |
| Vehicle protocol | ISO 15765-4, CAN 29-bit identifiers, 500 kbit/s (ELM protocol `7`) | Proven |
| Addressing | responses arrive as `18DAF1<ecu>`; `F1` is the tester | Proven |
| Responding ECUs | 8 on services 03/07, 5 on service 0A | Proven |

The adapter also reports lifetime counters (`STDIX`): power-on-reset count,
total run time, engine cranks and engine starts. On this vehicle the crank and
start counters read zero, which is what an EV should report.

## 2. What the adapter can do — without touching the vehicle bus

This is the most under-used capability in the project. These commands act on the
OBDLink itself and put **zero bytes on the vehicle CAN bus**, so they are safe to
run while the vehicle is asleep, locked, or under observation during a sleep
test.

| Command | Returns | Tier |
|---|---|---|
| `ATI` | ELM compatibility string | Proven |
| `AT@1` | manufacturer | Proven |
| `AT@2` | device identifier — this adapter answers `?` | Proven (unsupported) |
| `STI` | STN firmware version | Proven |
| `STDI` | device description | Proven |
| `STDIX` | extended identity: firmware, bootloader, IC revision, BT modem, serial, init date, POR count, total run time, engine cranks/starts | Proven |
| `STSN` | adapter serial number | Proven |
| `ATRV` | **12 V system voltage at the connector** | Proven |
| `ATCS` | CAN transmit/receive error counters | Proven |
| `ATDP` / `ATDPN` / `STPRS` | currently selected protocol | Proven |

### `ATRV` is a real telemetry channel

`ATRV` reads the voltage on the J1962 connector. It needs no protocol, no ECU,
and generates no CAN traffic. Observed values so far:

| Vehicle state | `ATRV` |
|---|---|
| Awake, DC-DC converter running | 13.9 V |
| Powered off, bus silent | 12.7 V – 13.0 V |

That is enough resolution to trend the 12 V battery while the truck sleeps,
which is precisely the measurement the continuous-collector power gate is
waiting on — and it can be taken **without ever waking the diagnostic bus**.
This is currently unused. See section 7.

## 3. What the vehicle exposes over standard OBD-II

### Service 01 — current data

The vehicle's own support bitmaps advertise exactly **14** PIDs:

```text
01 0D 1C 1F 20 21 30 31 40 42 60 80 A0 A6
```

| PID | Meaning | Tier |
|---|---|---|
| `01` | monitor status since DTCs cleared | **Proven** — answered; deliberately left undecoded |
| `0D` | vehicle speed | **Proven** — 0 to 94 km/h over a real drive |
| `1C` | OBD standard conformance code | **Proven** — `5` |
| `1F` | run time since engine start | **Proven** — 1144 s |
| `20` | support bitmap for PIDs 21–40 | **Proven** |
| `21` | distance travelled with MIL on | **Proven** — 0 km |
| `30` | warm-ups since codes cleared | **Proven** — 2 |
| `31` | distance travelled since codes cleared | **Proven** — 19 km |
| `40` | support bitmap for PIDs 41–60 | **Proven** |
| `42` | control module voltage | **Proven** — 13.712 V, from all 8 ECUs |
| `60` | support bitmap for PIDs 61–80 | **Proven** |
| `80` | support bitmap for PIDs 81–A0 | **Proven** |
| `A0` | support bitmap for PIDs A1–C0 | **Proven** |
| `A6` | **odometer** | **Proven** — 2146.6 to 2157.2 km, corroborated by PID `31` |

All fourteen advertised PIDs have now been read. `01` is the one deliberate
omission from decoding: it is a composite of MIL state and readiness monitors,
and the scalar sample shape cannot represent it without lying about it. Its raw
bytes are preserved in the transcript.

Until 2026-09-01 only three of these had ever been requested: the probe used a
fixed generic list that overlapped this vehicle in three places, so eight
advertised PIDs — including the odometer — were never asked for. The probe now
reads whatever the vehicle's own support bitmap advertises.

Equally important is what is *absent*. The vehicle does **not** advertise
`5B` (hybrid/EV battery remaining life) or `46` (ambient air temperature); both
answered `NO DATA` when asked directly. There is no standard-OBD path on this
vehicle to state of charge, pack voltage, pack temperature, cell balance,
charge state, or range. That gap is the entire motivation for
[enhanced PID validation](ENHANCED_PID_VALIDATION.md), and it is not something
a better decoder can fix.

### Service 09 — vehicle information

| Item | Result | Tier |
|---|---|---|
| `0902` VIN | 17 characters, decoded and masked outside the raw log | **Proven** |
| `0904` calibration IDs | `135240240857240850` | **Proven** |
| `090A` ECU names | `GatDMCBCMBSMBSMDMCDMCBSC` → Gateway, DMC, BCM, BSM, BSM, DMC, DMC, BSC | **Proven** |
| `0900` support bitmap | advertises 4 items: `02`, `04`, `06`, `0A` | **Proven** |
| `0906` CVN | calibration verification numbers returned from 6 modules | **Proven** |
| `0908`, `090B` | not advertised by this vehicle | Not available |

### Every module's answer, not just the first

A single request on this vehicle is answered by up to eight modules, each
reporting its **own** value. Asking for control module voltage (`0142`) returns
eight different voltages:

```text
ecu 45  13.747 V     ecu CB  13.693 V
ecu 17  13.571 V     ecu 1D  13.500 V
ecu 40  13.875 V     ecu 1E  13.524 V
ecu CD  13.726 V     ecu 28  13.910 V
```

That is a 0.41 V spread across the vehicle — the voltage drop between modules
on the harness, which is a real measurement and not noise.

Until 2026-09-01 the decoder returned the *first* matching frame and discarded
the rest, so seven of those eight readings never reached the database. The raw
transcript always held them; the queryable data did not. `decode_pid_per_ecu`
now returns one value per responding module, `samples.ecu` records which module
gave it, and the database keeps every one.

This matters beyond voltage. Any PID several modules answer is a *distribution*,
not a number: "the pack controller said one thing and the drive unit said
another" is the observation, and collapsing it to one value silently picks a
winner.

### The module map

Eight modules answer, and service 09 PID `0A` names them in full:

| # | Name reported by the vehicle |
|---|---|
| 1 | `Gateway Module - GWM` |
| 2 | `BSM-BatterySysMngr` |
| 3 | `DMCM-DriveMotorCtrl` |
| 4 | `DMC2-DriveMotorCtrl2` |
| 5 | `DMC3-DriveMotorCtrl3` |
| 6 | `BCM-BodyControl` |
| 7 | `BSCM-BrakeSystem` |
| 8 | `BSM-BatterySysMngr` |

Three drive-motor controllers and two battery-system managers, which is what a
tri-motor Ultium truck should report.

In 29-bit addressing a reply arrives as `18DAF1<ecu>`. Eight distinct source
addresses answered services 03 and 07:

```text
17  1D  1E  28  40  45  CB  CD
```

Every address is now named, measured behind a per-address receive filter:

| Address | Module | | Address | Module |
|---|---|---|---|---|
| `17` | `DMCM-DriveMotorCtrl` | | `40` | `BCM-BodyControl` |
| `1D` | `DMC2-DriveMotorCtrl2` | | `45` | `Gateway Module - GWM` |
| `1E` | `DMC3-DriveMotorCtrl3` | | `CB` | `BSM-BatterySysMngr` |
| `28` | `BSCM-BrakeSystem` | | `CD` | `BSM-BatterySysMngr` |

An earlier version of this document called address `28` the gateway, inferred
from it being the only module still answering while the vehicle shut down.
That was wrong: `28` is the brake system controller and `45` is the gateway.
The module that stays reachable longest during shutdown is the brake
controller.

**The pairing is closed.** What follows is kept as the record of how.

Previously: The order of the `090A` reply is not
guaranteed to match the order in which modules answered service 03, so address
`45` cannot yet be called `BCM-BodyControl` or anything else with certainty. An
enhanced-PID request has to be addressed to a specific ECU, so a definitive
address-to-name map is a prerequisite for
[ENHANCED_PID_VALIDATION.md](ENHANCED_PID_VALIDATION.md).

Closing it needed **no new command authority**, and is now implemented.
`AdapterSession.ecu_name_map()` sets an `ATCRA18DAF1<addr>` receive filter,
requests `090A`, attributes the reply to that address, and restores the
unfiltered state in a `finally` block — leaving a filter in place would make
every later request silently see only one module. `ATCRA` was already on the
allowlist and accepts a full 29-bit address.

`ATSH` cannot be used for this: its allowlist pattern caps at six hex digits and
a 29-bit request header needs eight. The map itself is still **unproven** — it
needs the vehicle awake.

### Services 02 and 06 — added 2026-09-01

| Service | What it returns | Tier |
|---|---|---|
| `02` freeze frame | the snapshot an ECU stored alongside a DTC | **Proven** — `020000` advertises 4 PIDs: `02 0D 1F 20`. No frame exists to read while the vehicle has no DTCs |
| `06` on-board monitoring | per-monitor test results | **Proven** — answers, and advertises **zero** monitor IDs on this vehicle |

Both are standard SAE J1979 *read* services from the same specification as
`01`/`03`/`07`/`09`/`0A`. Unlike Mode 22 they need no vendor identifier to be
guessed. They passed the change-control process in [Safety](SAFETY.md), but the
vehicle was asleep when they were added, so they are **permitted and not yet
proven on this vehicle** — no live request has been made.

Freeze frame is additionally gated by behaviour rather than by the gate: the
probe always asks `020000` (what a frame *would* hold) but requests individual
frame readings only when a DTC read actually returned stored codes. With zero
DTCs there is no freeze frame to fetch.

What a freeze frame would contain on this vehicle is already known: `02` (the
DTC that set it), `0D` (speed), `1F` (run time), and the `20` bank pointer. It
would not carry pack or thermal state.

Service 06 decoding is deliberately partial. The unit-and-scaling identifier
table covers only the identifiers that can be stated confidently from the
standard; an unrecognised one yields a null scaled value with the raw counts
preserved, rather than a plausible number derived from a guessed scale factor.

### Services 03 / 07 / 0A — diagnostic trouble codes

| Service | Responding ECUs | Result | Tier |
|---|---|---|---|
| `03` stored | 8 | zero codes | **Proven** |
| `07` pending | 8 | zero codes | **Proven** |
| `0A` permanent | 5 | zero codes | **Proven** |

A valid empty DTC response is a positive result. It is distinct from `NO DATA`
and from a `7F <service> 22` negative response, and the decoder classifies all
three separately.

### Vehicle response states we have actually observed

Knowing how the truck refuses is as useful as knowing how it answers.

| Observed | Meaning | When |
|---|---|---|
| Positive data from 5–8 ECUs | vehicle serving diagnostics | vehicle awake |
| `7F <service> 22` from ECU `28` only | `BSCM-BrakeSystem` is alive and refusing: `conditionsNotCorrect`. It is the module that stays reachable longest during shutdown -- not the gateway, which is `45` | vehicle shutting down / not in a data-serving state |
| `NO DATA` with `ATCS T:00 R:00` | protocol correct, request transmitted, **no ECU on the bus at all** | vehicle fully asleep |
| `SEARCHING... / UNABLE TO CONNECT` | auto-detect could not find any protocol | vehicle fully asleep |

The `ATCS T:00 R:00` reading is what makes the last two unambiguous: zero CAN
transmit and receive errors means the adapter spoke correctly and the bus was
simply silent. That distinguishes "vehicle asleep" from "wiring or adapter
fault", which otherwise look identical.

## 4. What the software can do

| Capability | Where | Tier |
|---|---|---|
| Fail-closed command allowlist, no runtime bypass | `safety.py` | Proven |
| Independent forbidden-service denylist | `safety.py` | Proven |
| Refusal of command batching (`\r`, `\n`, `;`, NUL) | `safety.py` | Proven |
| Byte-exact append-only transcript, hex **and** base64, fsync'd | `rawlog.py` | Proven |
| Guarded serial transport; rejected commands write zero bytes | `transport.py` | Proven |
| Capped exponential reconnect backoff | `transport.py` | Proven |
| Per-ECU 29-bit ISO-TP reassembly, fail-closed on truncation | `decode.py` | Proven |
| UDS/OBD negative-response decoding | `decode.py` | Proven |
| VIN masking everywhere outside the raw log | `decode.py` | Proven |
| Exact operator-supplied command sets, validated all-or-nothing before the port opens | `probe.py --commands` | Proven |
| Offline replay of a transcript with no hardware | `probe.py --replay` | Proven |
| WAL-mode SQLite with a local upload-queue marker | `storage.py` | Proven |
| One-shot collector cycle | `collector.py --once` | Proven |
| Sleeping-vehicle idle backoff instead of escalation | `collector.py` | Proven |
| Bounded self-stopping collector trial | `collector.py --max-cycles/--duration-s` | Proven — two bounded drive sessions |
| Zero-CAN-traffic 12 V watch | `voltage.py` | Proven — running during a sleep observation |
| Sanitized offline capabilities report | `capabilities.py` | Proven — run on the node against the live database |
| Local export for external/AI ingestion | `export.py` | Proven — jsonl and csv exported from the live database, per-module attribution included |
| Bluetooth recovery for an already-bonded adapter; cannot pair, trust or remove | `btdiscover.py` | Proven |
| E-paper status page, full refreshes only, unchanged frames skipped | `display/status.py` | Proven |
| PiSugar2 cell monitoring | `battery.py` | Proven — reads the live pack; chip identified by measurement, not by label |
| Graceful shutdown on low battery | `battery.py` | **Held in dry-run**: a halt does not cut PiSugar power and the node would not restart itself. See the known limitation in [RUNBOOK](RUNBOOK.md) |
| Hardware-free PTY/ELM simulator | `tests/elm_simulator.py` | Proven |

## 5. Not available

Each of these is a deliberate boundary, not an oversight.

| Not available | Why |
|---|---|
| **Mode 22 enhanced GM/Ultium PIDs** | Rejected by the safety gate. Identifiers are unproven on this VIN and this project does not guess them. Plan: [ENHANCED_PID_VALIDATION.md](ENHANCED_PID_VALIDATION.md). Note that modes 02 and 06 were added instead precisely because they are standard and need no guessing |
| **State of charge, pack voltage, pack temperature, range** | Not exposed over standard OBD on this vehicle. Requires Mode 22 or passive CAN monitoring |
| **GPS / location** | No GPS receiver on the Pi, and location is not available over OBD |
| **OnStar / GM cloud data** | Different system entirely. Belongs in a separate broker with isolated credentials and a command allowlist, never in this read-only OBD node |
| **Remote commands** (lock/unlock, preconditioning, remote start) | Out of scope by design. This node has no vehicle write authority of any kind |
| **DTC clearing (Mode 04)** | Permanently forbidden. Not a configuration option |
| **Actuator/control tests (Mode 08)** | Permanently forbidden |
| **UDS write/control/security/reset/routine** | Permanently forbidden |
| **Passive CAN monitoring** (`ATMA` / `STM`) | Receive-only and genuinely interesting, but not on the allowlist. Would need the full five-part change-control process in [SAFETY.md](SAFETY.md) |
| **Continuous collector autostart** | Gated on the power/sleep result, not on software. See section 6 |
| **Raw transcript upload** | Rejected at config load. Raw logs can contain an unmasked VIN and never leave the Pi |
| **Any upload at all, today** | `upload.enabled = false` and the endpoint is empty. The uploader refuses to start |

## 6. The power picture

This is the only thing blocking continuous polling, and it is now much better
understood.

**The OBD-II port on this vehicle is always live.** Two independent sources
agree:

1. The adapter's own `STDIX` counters showed a continuous power-on session of
   over seven hours spanning periods when the vehicle was off.
2. The owner directly observed that after the truck powered itself off, the
   OBD-II port and attached devices still had power.

Consequences:

- The Pi and the OBDLink draw from the 12 V battery whenever they are attached,
  including while the truck sleeps. This is a parasitic load.
- The truck powered itself off normally while the adapter was connected and
  shortly after diagnostic traffic. Adapter presence alone did not prevent
  shutdown. This is encouraging but is **not** the same as proving that a
  2-second polling loop lets the modules reach deep sleep.
- The correct next measurement is a 12 V trend over a full sleep period, which
  `ATRV` can supply with zero bus traffic.

**Observed so far**: with polling stopped, the vehicle reached sleep about five
minutes after parking (13.9 V to 12.7 V), watched with zero CAN traffic.

**Still unproven**: overnight 12 V stability with the hardware attached — every
attempt so far was made while the truck was plugged in, which holds the rail up
and measures nothing — and whether the vehicle still sleeps while the collector
is *actively polling*, which is the condition the gate is actually about.

Until both are answered, the accepted state remains:

```text
collector.enabled = false
hummer-collector.service = disabled / inactive
```

## 7. What to do next, in order

1. ~~Read the advertised PIDs we never asked for~~ — **done 2026-09-01.** All
   fourteen now read; odometer decodes at 2146.6 km.
2. ~~Decode `0900` and read the advertised service 09 items~~ — **done.**
3. **Trend `ATRV` across a sleep cycle** with `hummer-obd-voltage`. Zero CAN
   traffic, directly measures the parasitic-drain question that gates
   everything else. First observation started 2026-09-01.
4. **Run `hummer-obd-probe --max` on an awake vehicle.** This is now the single
   highest-value action outstanding. It exercises, for the first time on this
   truck: service 06 monitor results, per-module attribution of every PID, the
   ECU address-to-name map, and freeze frames if any DTC exists. Modes 02 and
   06 stay "permitted but unproven" until it runs.
5. **Run the bounded collector trial** (`--duration-s`, conservative interval)
   only after the voltage trend looks acceptable, then confirm the vehicle still
   sleeps.
6. **Only then** consider `collector.enabled = true`.
7. Enhanced PIDs remain gated on the evidence bar in
   [ENHANCED_PID_VALIDATION.md](ENHANCED_PID_VALIDATION.md).

## Reproducing this report

```bash
hummer-obd-capabilities --root .          # sanitized, touches no serial device
```

It reads only the configuration, the safety gate, existing evidence summaries,
raw-transcript metadata and the SQLite database. It never opens `/dev/rfcomm0`,
so it is safe to run at any point in a sleep test.
