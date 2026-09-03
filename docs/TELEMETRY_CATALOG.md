# Telemetry catalog

**The single authoritative list of what this node can read from this vehicle.**

If you are an agent or a reviewer trying to work out this project's
capabilities, read this file. Everything else is supporting detail:
[module map](GM_MODULE_MAP.md) for topology, [enhanced
candidates](GM_ENHANCED_CANDIDATES.md) for provenance and rejected leads,
[safety](SAFETY.md) for the gates, [validation](VALIDATION.md) for the power
measurements.

Last measured 2026-09-03. Vehicle: 2024 GMC Hummer EV, GM BT1/Ultium.
Adapter: OBDLink MX+ (STN2255) over Bluetooth SPP to a Raspberry Pi Zero 2 W.
Link: ISO 15765-4, 29-bit CAN, 500 kbit/s.

## Evidence levels

Each signal carries one, and they mean different things:

| Level | Meaning |
|---|---|
| **measured** | Read from this VIN, decoded, and cross-checked against an independent quantity |
| **read** | Read from this VIN and decodes to a plausible value, but nothing independent confirms the scaling |
| **raw** | The module answers, the bytes are stored, the meaning is *not* claimed |
| **refused** | The module answered `7F`, and the response code is recorded |
| **absent** | No sourced identifier exists — not "impossible", "not found" |

---

## 1. High-voltage battery — module `CB` `BSM-BatterySysMngr`

| Signal | Identifier | Scaling | Unit | Level |
|---|---|---|---|---|
| State of charge | `0x27C6` | `[B4:B5] / 655.35` | % | **measured** |
| Energy remaining | `0x27AF` | `[B4:B5] / 100` | kWh | **measured** |
| Estimated range | `0x27C7` | `[B4:B6] / 103` | mi | **measured** |
| Distance since full charge | `0x27C0` | `[B4:B6] / 16.09344` | mi | **read** |
| Temperature | `0x0046` | `(B4 - 40) * 1.8 + 32` | °F | **read** |
| Cell voltage average | `0x2AF5` | `[B0:B1] / 10000` | V | **measured** |
| Cell voltage minimum | `0x2AF5` | `[B2:B3] / 10000` | V | **measured** |
| Cell voltage maximum | `0x2AF5` | `[B4:B5] / 10000` | V | **measured** |
| Cell spread | derived | `(max - min)` | mV | **measured** |
| 26-element array | `0x2B43` | none applied | — | **raw** |
| Charger field | `0x5401` | none applied | — | **raw** |

### Charge/discharge power is derived, not read

`0x5401` is published as "charger DC power" with a two-byte `/4350` scaling.
**That is wrong for this vehicle.** It answers with a *single* byte, reads
`0x96` at idle and `0x93` during an 7.8 kW AC charge — non-zero when idle, and
not scalable to the measured rate. It is stored raw and no equation is applied.

Power is instead taken from the slope of `0x27AF`:

```
power_kw = Δenergy_kwh / Δhours
```

That field moves smoothly and with high resolution — 80 distinct values across
ten minutes of charging — which makes its slope a sound measurement. Positive is
charging.

**The slope is taken over a 60-second window, not between consecutive samples**,
and the reason is worth recording. `energy_kwh` is quantised to 0.01 kWh. At a
~7 second cycle a single quantum is about 5 kW, so a consecutive-sample slope
alternated between 9.53 and 4.76 kW while the true rate was a steady 7.8 — right
on average, useless instant to instant. Measured live on a charging vehicle
before and after:

```
consecutive samples   9.53  4.76                                  (+-60%)
60-second window      6.99  7.46  7.77  7.34  7.51  8.06  7.96    (~7.6)
```

Validated against a real AC charging session at 7.81 kW derived offline from the
CSV. The column is empty rather than zero until there is enough history: a
placeholder zero would read as "not charging".

### Why these decodes are trusted

They were never fitted to one another, yet they agree:

| Cross-check | Computation | Result | Independent expectation |
|---|---|---|---|
| range ÷ SoC | 269.04 / 0.8085 | **333 mi** at full | 329 mi EPA |
| energy ÷ SoC, three separate occasions | — | **191.0 / 191.7 / 191.7 kWh** | consistent pack capacity |
| cell voltage vs SoC | 4.02 V at 80.85 % | correct for an NMC cell | — |
| cell ordering | min < avg < max | holds exactly | a wrong offset breaks it |
| charging | energy, SoC and all cell voltages rose together while power read +7.8 kW | internally consistent | — |

---

## 1b. Traction pack voltage and current — module `17` `DMCM-DriveMotorCtrl`

| Signal | Identifier | Scaling | Unit | Level |
|---|---|---|---|---|
| Traction pack voltage | `0x2885` | `[B0:B1] / 100` | V | **measured** |
| Traction pack current | `0x2414` | `signed16 / 20`, negative = charging | A | **measured** |
| Instantaneous HV power | derived | `pack_v * pack_a / 1000` | kW | **measured** |

This was the project's single largest gap and it is now closed. Measured during
an AC charging session: **388.60 V**, **−20.95 A**, **−8.14 kW**.

Both identifiers come from **unmerged, single-author, BEV3 sources** — weaker
provenance than anything else in this catalog. They are trusted anyway, because
the vehicle and the existing measurements corroborated them:

| Check | Result |
|---|---|
| Magnitude | 388.6 V is a 400 V-class Ultium pack. The 12 V rail read 13.6 V at the same moment, so this is not the low-voltage domain mislabelled |
| Sign convention | current was **negative** while plugged in and charging, exactly as the source states |
| `0x2414` against its own test vectors | `0xFE39 → −22.75 A` and `0x0012 → 0.9 A`, both reproduced exactly |
| **Against a wholly independent measurement** | volts × amps gave **8.14 kW**; the charge power derived from the *energy field's slope* — a different identifier, a different module, a different method — gave **~7.7 kW**. Agreement within 6 %, and in the correct direction: pack DC power exceeds usable energy gain by the conversion and thermal losses |

That last row is the one that matters. Two unmerged sources for a different
platform were confirmed by a measurement taken a completely different way.

Both power figures are kept side by side in the recorder rather than averaged.
Two independent routes to one quantity is what exposed the `0x5401` mislabel;
disagreement between them is a signal worth seeing.

---

## 2. Chassis dynamics — module `28` `BSCM-BrakeSystem`

| Signal | Identifier | Scaling | Unit | Level |
|---|---|---|---|---|
| Wheel speed, four corners | `0x4A7A` | one byte each, FL/FR/RL/RR | km/h | **read** |
| Brake pressure | `0x4A7C` | `(B0 - 10) * 100` | kPa | **read** |
| Steering wheel angle | `0x4C2D` | `signed16 * 0.022` | ° | **read** |
| Lateral acceleration | `0x4C2F` | `signed16 * 0.0015928` | g | **read** |
| Longitudinal acceleration | `0x4C30` | `signed16 * 0.0015928` | g | **read** |

These carry the **best-evidenced scalings in the project**. The source is not a
stated formula but test fixtures pairing a captured frame with its expected
value, so each equation was *derived from the vectors* and re-checked against
every one — including both signs. A transcription error survives copying but not
arithmetic.

---

## 3. Drive motor controllers — modules `17`, `1D`, `1E`

| Signal | Identifier | Scaling | Unit | Level |
|---|---|---|---|---|
| Module supply voltage ×3 | `0x33E5` | `B0 / 10` | V | **read** |

All three answer independently: 13.2 / 13.1 / 13.1 V. This is the **12 V
domain**, not traction-pack voltage, and must never be presented as one.

---

## 4. Standard OBD-II

Functional broadcast, priority `0x18`, header `DB33F1`.

| Signal | Request | Level |
|---|---|---|
| Vehicle speed | `01 0D` | **measured** |
| Odometer | `01 A6` | **measured** |
| 12 V supply, per responding module | `01 42` | **measured** |
| Run time, distance since codes cleared, distance with MIL on, warm-ups | `01 1F` `01 21` `01 31` `01 30` | **read** |
| Stored / pending / permanent DTCs | `03` `07` `0A` | **measured** — zero on this vehicle |
| VIN, calibration IDs, CVNs, module names | `09 02` `09 04` `09 06` `09 0A` | **measured** |
| Freeze frame | `02` | service proven; no frame exists (no DTC) |
| On-board monitor results | `06` | proven; vehicle advertises **zero** monitor IDs |

---

## 5. Node health

12 V connector voltage (`ATRV`, adapter-only, reaches no CAN bus), CAN
transmit/receive counters (`ATCS`), PiSugar2 cell voltage over I²C, Bluetooth
link state, e-paper status panel.

---

## 6. Refused, and what the refusal told us

| Target | Result | What it means |
|---|---|---|
| `0x27C6` `0x27AF` `0x2AF5` `0x2B43` at module `CD` | `7F 22 31` requestOutOfRange | `CD` **is** alive and **does** serve service 22 — it returned a formed UDS refusal, not silence. It simply does not hold `CB`'s identifiers. The two battery managers are not mirrors, and `CD`'s identifier space is entirely undiscovered. |

---

## 7. Not obtained

Stated as "no sourced identifier found", never as "impossible" — this project
has already been wrong once in that direction.

| Signal | Status |
|---|---|
| Motor RPM, torque, phase current, motor/inverter temperature | absent |
| Per-module battery temperature, coolant, state of health, contactor state | absent |
| Suspension height, rear-wheel steering, CrabWalk state | absent |
| Door, lock, lighting, HVAC state (module `40`, never asked) | absent |
| Gateway/network state (module `45`, never asked) | absent |
| GPS / location | **not an OBD service.** Needs a USB GNSS receiver or the OnStar path. |
| Raw internal CAN-FD | **hardware ceiling.** The MX+ implements Classical CAN only. |
| Lock, unlock, climate, preconditioning | out of scope by design — cloud broker, never this node |

---

## 8. How it is collected

| Mode | Command | Behaviour |
|---|---|---|
| **Service** | `hummer-drive.service` | Records a 26-column decoded CSV whenever the vehicle is awake; sends **only `ATRV`** while it sleeps. Rows are flushed and `fsync`ed as taken, so a session survives the vehicle cutting power. Enabled at boot. |
| One-shot | `hummer-obd-drive --confirm --max-cycles N` | Bounded manual session |
| Single identifier | `hummer-obd-enhanced --profile <p> --confirm` | Supervised one-shot, dry run by default |
| Standard only | `hummer-obd-collector` | Unattended collector. **Cannot send service 22.** |
| Offline | `hummer-obd-capabilities` | Report that opens no serial device |

## 9. What it will never do

Service `22` is refused by `validate_command`, the gate the unattended
collector uses, and an **import-time assertion fails the build** if anyone adds
it. Enhanced reads go through a second, narrower gate accepting an exact
enumeration of identifiers — currently 14 — and refusing everything else,
including the identifiers immediately either side of ones that work. A test
walks all 768 identifiers around the allowlist and asserts the accepted set is
exactly the allowlist.

`04`, `08`, `10`, `11`, `14`, `27`, `28`, `2E`, `2F`, `31`, `34`–`38`, `3B`,
`3D`, `3E`, `83`, `84`, `85`, `87` are permanently forbidden. There is no
runtime bypass, no wildcard, and no identifier sweeping — an ECU asked for
thousands of identifiers in sequence is being probed, and that is not something
this project does to someone's vehicle.
