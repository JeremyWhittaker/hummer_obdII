# GM enhanced diagnostics on this vehicle

Status: **one identifier proven on this VIN.** 2026-09-02.

This document exists because the project previously said, in several places,
that high-voltage battery data could not be obtained through the OBD port. That
statement was wrong, and this file records both the correction and the evidence
that forced it.

---

## What is now proven

On 2026-09-02 at 22:33 UTC, with the vehicle awake (connector at 13.8 V), this
project sent a single UDS `ReadDataByIdentifier` request and received a
positive response:

```
request   2227C6            (service 22, identifier 0x27C6)
reply     142AF1CB 05 62 27 C6 D1 8A
```

Reading that reply left to right:

| Bytes | Meaning |
|---|---|
| `142AF1CB` | 29-bit CAN identifier: the responding module |
| `05` | ISO-TP single frame, five data bytes follow |
| `62` | positive response to service `22` (`0x22 + 0x40`) |
| `27 C6` | the identifier, echoed back |
| `D1 8A` | the data |

`0xD18A` = 53642. Applying the published scaling, 53642 / 655.35 = **81.85 %**
state of charge.

The read was repeated immediately and returned a byte-identical frame, so it is
reproducible rather than a one-off.

### It is live telemetry, not a constant

Eight supervised reads over thirty minutes, vehicle awake and parked at 13.8 V
throughout:

| Time (UTC) | Raw | `/655.35` |
|---|---|---|
| 22:33 | `D18A` | 81.85 % |
| 22:35 | `D18A` | 81.85 % |
| 22:38 | `D18A` | 81.85 % |
| 22:43 | `D042` | 81.35 % |
| 22:48 | `D042` | 81.35 % |
| 22:53 | `D042` | 81.35 % |
| 22:58 | `D042` | 81.35 % |
| 23:03 | `D042` | 81.35 % |

This matters more than the absolute number. A byte pair that merely *happened*
to fall in a plausible range would sit still; a field that steps downward while
the vehicle is awake and drawing its 12 V rail from the traction pack is
behaving the way a state of charge behaves. It is the difference between "this
number looks right" and "this number is a measurement".

Three things follow from the shape of the series.

**The field is quantised to 0.5 %.** The step is `0xD18A - 0xD042` = 328 counts,
and 328 / 655.35 = 0.5004 %. That is why five consecutive reads return identical
bytes and then the value moves all at once: the module reports state of charge
in half-percent increments, not continuously.

**The scaling is now constrained by the data, not only by the source.** A
328-count step landing on exactly one half of one percent is what `/655.35`
predicts. The competing interpretations of the same bytes give 536.42 (`/100`)
and 21036 (`/2.55`), and the same step becomes 3.28 and 128.6 respectively --
neither a round figure in any unit this signal could plausibly carry.

**The discharge rate is physically sensible.** One step occurred, somewhere
between 22:38 and 22:43, with no further movement in the following twenty
minutes. That is at most about 1 % per hour, which for a pack of this size is
on the order of a couple of kilowatts -- the right magnitude for an awake,
parked vehicle running its DC-DC converter and cabin electronics. A signal that
decoded to a plausible-looking percentage but implied an absurd power draw
would be a reason to doubt the decode; this one does not.

### The byte offset is confirmed, not assumed

The community profile specifies the field as `[B8:B9]`, and it had previously
specified `[B4:B5]` before a correction. Rather than trust either, the tool
computes every two-byte window and the documentation states which one matched.

Counting `B0` from the first byte of the **whole CAN frame** — identifier, then
PCI byte, then the response:

```
B0 B1 B2 B3  B4  B5 B6 B7  B8 B9
14 2A F1 CB  05  62 27 C6  D1 8A
                           ^^^^^ B8:B9
```

`B8:B9` lands exactly on `D1 8A`. The published offset is correct **under the
convention that counts the CAN header and the PCI byte**, which is the detail
that makes it reproducible instead of merely plausible. `tests/test_enhanced.py`
asserts this alignment against the captured frame.

### Outstanding: independent confirmation of the value

81.85 % is physically plausible and stable across reads, but it has **not yet
been compared against the dashboard or myGMC**. Until it has, the correct claim
is "the identifier returns a stable value that the published decoder maps into
a plausible range", not "the state of charge is 81.85 %". That check is the one
remaining step for this identifier.

---

## Addressing

| | |
|---|---|
| Protocol | ISO 15765-4, 29-bit, 500 kbit/s (`ATSP7`) |
| CAN priority | `0x14` (`ATCP14`) |
| Request identifier | `0x14DACBF1` — target `CB`, tester `F1` |
| Response identifier | `0x142AF1CB` |
| Flow control | `ATFCSH14DACBF1`, `ATFCSD300000`, `ATFCSM1` |

**Module `CB` is this vehicle's own Battery System Manager.** That is not taken
from the source: the truck reported it, when each address was queried behind
its own receive filter and answered service 09 PID `0A` for itself
(`CB` = `BSM-BatterySysMngr`, see
[enhanced PID validation](ENHANCED_PID_VALIDATION.md)). So an internet source
and a measurement from the vehicle independently agree that a battery
state-of-charge identifier lives at the battery manager — which is the closest
thing to independent corroboration available here, and it arrived from the
truck rather than from a second document.

Two details are easy to get wrong and both cost a working read:

**`ATCP14` is required.** `ATSH` carries only the low three bytes of a 29-bit
identifier. Without the priority byte the request leaves as `0x18DACBF1` and
the module does not answer.

**The response header is not the legislated form.** Legislated OBD replies on
this vehicle arrive as `18DAF1<ecu>`. This one arrives as `142AF1CB`: priority
`0x14`, second byte `0x2A` rather than `0xDA`. The project's frame parser
originally recognised only `18 DA`/`18 DB`, so it read byte 0 (`0x14`) as the
PCI of a multi-frame message, waited for continuation frames that never came,
and discarded the payload as incomplete. **The first real enhanced read was
captured and very nearly thrown away by our own decoder** — the raw transcript
is what saved it. `split_can_header` now recognises the GM form, guarded by a
PCI plausibility check so the widened pattern cannot swallow ordinary payload.

---

## Why this is allowed, given "keep Mode 22 blocked"

The standing instruction was to keep Mode 22 blocked and not to guess
identifiers. Both still hold, and the design enforces them structurally rather
than by convention.

Service `22` is **still refused** by `safety.validate_command`, which is the
gate every unattended path uses. The collector cannot transmit an enhanced read
no matter how it is configured. A second, deliberately *narrower* gate —
`safety.validate_enhanced_command` — accepts service `22` only for an
identifier enumerated in `ENHANCED_READ_DIDS`, and refuses every other service
including the ordinary read services the collector is allowed to send.

That the identifier came from a published source rather than from a guess is
the other half. `0x27C6` was not derived, incremented, or inferred; it was read
out of a profile that names this vehicle. The gate refuses `0x27C5` and
`0x27C7` — one step either side, exactly what a sweep would try next — and
`tests/test_enhanced.py` asserts that it does.

**There is no DID sweeping in this project and there will not be.** An ECU
asked for an identifier it does not have answers `7F 22 31`; an ECU asked for
thousands in sequence is being probed, and that is not something to do to
someone's vehicle.

---

## Reading a negative response

If a future identifier fails, the response code says *why*, and the four
meanings must not be collapsed into "it didn't work":

| NRC | Name | What it means here |
|---|---|---|
| `11` | serviceNotSupported | the module has no service 22 at all |
| `22` | conditionsNotCorrect | right identifier, wrong vehicle state — retry awake |
| `31` | requestOutOfRange | service 22 works, this identifier does not exist |
| `33` | securityAccessDenied | it exists and is protected |
| `34` | authenticationRequired | it exists and needs Global B authentication |

`31` is a *success for the method* even though it is a failure for the
identifier: it proves the module answered a service 22 request.

Note the collision hazard: a sleeping module on this truck already answers
legislated requests with `7F 01 22`, where the trailing `22` is the response
code `conditionsNotCorrect` — **not** service 22. A negative response to an
enhanced read has the shape `7F 22 xx`, where the middle byte is the service.

---

## Identifier registry

This table is the safety gate's own list, rendered from it. It fell thirty-six
commits behind the code once by being maintained by hand; it is generated now,
and a test fails if it drifts again.

<!-- BEGIN GENERATED IDENTIFIER REGISTRY -->

<!-- Generated from hummer_obd.safety.ENHANCED_READ_DIDS by
     hummer_obd.registry.render_registry(). Do not edit by hand:
     tests/test_registry.py fails when this drifts from the code. -->

The safety gate currently admits **35 identifiers**.
Being listed here means *may be transmitted*, not *is known to work on
this vehicle*. Results live in the tiers below and in
[Probe, 2026-09-03](PROBE_2026-09-03.md).

The **level** column comes from `hummer_obd.confidence`, which is keyed
identically to the gate:

* **0** — sourced only
* **1** — answers here
* **2** — decoded
* **3** — cross-validated **-- production telemetry starts here**
* **4** — cross-validated in more than one state

A level below 3 is not a telemetry reading, whatever its column
is called. Level 2 is the dangerous one: a plausible number with a
confident-looking unit and nothing behind it.

| Identifier | Level | Answers at | Seen in | Provenance |
|---|---|---|---|---|
| `0x0046` | **2** | `CB` | parked, driving, charging | temperature -- meatpiHQ/wican-fw vehicle_profiles/gmc/sierra-ev.json (TMP_A), BT1 platform family |
| `0x2414` | **4** | `17` | parked, driving, charging | HV pack current candidate -- OBDb/Cadillac-LYRIQ PR #14, LYRIQ_HVBAT_A, signed 16-bit / 20 amps, negative = charging. Ships test vectors (0xFE39 -> -22.75 A, 0x0012 -> 0.9 A) which the formula reproduces exactly. UNMERGED, 2025 Lyriq BEV3, not BT1 |
| `0x2429` | **3** | `17` | parked, driving, charging | nominal battery voltage per its source, CONTRADICTED HERE -- OBDb/Cadillac-LYRIQ PR #14 (LYRIQ_HVBAT_NOMINAL_V), hdr DA17, 16-bit / 64 volts, max 1023. The source calls it the constant rated pack voltage rather than a live measurement, so a value that does not move is the expected result and not a failed decode. UNMERGED, 2025 Lyriq BEV3, not BT1 |
| `0x2709` | **1** | `CB` | parked, driving, charging | A/C compressor temperature candidate -- meatpiHQ/wican-fw issue #884, BEV3 Bolt, UNMERGED, not BT1 |
| `0x27AF` | **4** | `CB` | parked, driving, charging | HV battery energy remaining -- meatpiHQ/wican-fw vehicle_profiles/gmc/sierra-ev.json (HV_CAPACITY_R). Sierra EV is BT1, the same platform family bt1.json groups with the Hummer EV |
| `0x27B5` | **1** | `CB` | parked, driving, charging | thermal-management distance candidate -- meatpiHQ/wican-fw issue #884, BEV3 Bolt, UNMERGED, not BT1 |
| `0x27BB` | **2** | `CB` | parked, driving, charging | thermal-management energy candidate -- meatpiHQ/wican-fw issue #884, BEV3 Bolt, UNMERGED, not BT1 |
| `0x27BF` | **2** | `CB` | parked, driving, charging | charge-cycle regeneration-related field candidate -- meatpiHQ/wican-fw issue #884, BEV3 Bolt, UNMERGED, not BT1 |
| `0x27C0` | **2** | `CB` | parked, driving, charging | distance since full charge -- meatpiHQ/wican-fw vehicle_profiles/gmc/sierra-ev.json (DIST_SINCE_FULL_CHARGE), BT1 platform family |
| `0x27C6` | **4** | `CB` | parked, driving, charging | HV battery state of charge -- meatpiHQ/wican-fw vehicle_profiles/bt1/bt1.json, a profile whose car_model names the Hummer EV explicitly; independently attested in vehicle_profiles/gmc/sierra-ev.json |
| `0x27C7` | **3** | `CB` | parked, driving, charging | remaining range -- meatpiHQ/wican-fw vehicle_profiles/gmc/sierra-ev.json (RANGE), BT1 platform family |
| `0x2885` | **4** | `17`, `1D`, `1E` | parked, driving, charging | HV traction pack voltage candidate -- meatpiHQ/wican-fw issue #884, DMCM1_BATTERY_PACK_VOLTAGE, [B4:B5]/100 volts, min 0 max 500. UNMERGED single-author report, 2027 Bolt BEV3, not BT1 |
| `0x2AF1` | **1** | `CB` | parked, driving, charging | battery module temperature candidate -- meatpiHQ/wican-fw issue #884, BEV3 Bolt, UNMERGED, not BT1 |
| `0x2AF5` | **4** | `CB` | parked, driving, charging | HV battery cell voltage average/minimum/maximum -- OBDb/Chevrolet-Equinox-EV signalsets/v3/default.json, hdr DACB, three 16-bit fields divided by 10000, volts |
| `0x2B43` | **1** | `CB` | parked, driving, charging | HV battery state of charge, 8-bit -- OBDb/Chevrolet-Equinox-EV signalsets/v3/default.json, hdr DACB, byte * 100 / 255, percent |
| `0x33E5` | **3** | `17`, `1D`, `1E` | parked, driving, charging | drive motor control module battery voltage -- OBDb/Chevrolet-Equinox-EV signalsets/v3/default.json, hdr DA1D (this vehicle names 1D as DMC2-DriveMotorCtrl2), byte / 10, volts |
| `0x40E5` | **2** | `40` | parked, driving, charging | battery coolant temperature 1 candidate -- OBDb/Cadillac-LYRIQ PR #14, module 40, UNMERGED, not BT1 |
| `0x40E6` | **2** | `40` | parked, driving, charging | battery coolant temperature 2 candidate -- OBDb/Cadillac-LYRIQ PR #14, module 40, UNMERGED, not BT1 |
| `0x4124` | **1** | `40` | parked, driving, charging | HV battery temperature B candidate -- OBDb/Cadillac-LYRIQ PR #14, module 40, UNMERGED, not BT1 |
| `0x4127` | **3** | `40` | parked, driving, charging | HV battery temperature A candidate -- OBDb/Cadillac-LYRIQ PR #14, module 40, UNMERGED, not BT1 |
| `0x4149` | **1** | `40` | parked, driving, charging | EVSE advertised/pilot current candidate -- OBDb/Cadillac-LYRIQ PR #14, module 40, UNMERGED, not BT1 |
| `0x416C` | **2** | `40` | parked, driving, charging | HV battery group voltage 1 candidate -- OBDb/Cadillac-LYRIQ PR #14, module 40, UNMERGED, not BT1 |
| `0x416D` | **1** | `40` | parked, driving | HV battery group voltage 2 candidate -- OBDb/Cadillac-LYRIQ PR #14, module 40, UNMERGED, not BT1 |
| `0x416E` | **1** | `40` | parked, driving | HV battery group voltage 3 candidate -- OBDb/Cadillac-LYRIQ PR #14, module 40, UNMERGED, not BT1 |
| `0x434F` | **1** | `40` | parked, driving, charging | HV battery temperature candidate -- OBDb/Cadillac-LYRIQ PR #14, module 40, UNMERGED, not BT1 |
| `0x4A7A` | **3** | `28` | parked, driving | wheel speed, four corners -- OBDb/Cadillac-LYRIQ test fixture DA28.224A7A, one byte per wheel FL/FR/RL/RR, km/h |
| `0x4A7C` | **2** | `28` | parked, driving | brake pressure -- OBDb/Cadillac-LYRIQ test fixture DA28.224A7C, (byte - 10) * 100, kPa |
| `0x4C2D` | **2** | `28` | parked, driving | steering wheel angle -- OBDb/Cadillac-LYRIQ test fixture DA28.224C2D, signed 16-bit * 0.022, degrees |
| `0x4C2F` | **2** | `28` | parked, driving | lateral acceleration -- OBDb/Cadillac-LYRIQ test fixture DA28.224C2F, signed 16-bit * 0.0015928, g |
| `0x4C30` | **3** | `28` | parked, driving | longitudinal acceleration -- OBDb/Cadillac-LYRIQ test fixture DA28.224C30, signed 16-bit * 0.0015928, g |
| `0x5401` | **2** | `CB` | parked, driving, charging | DC charger power -- meatpiHQ/wican-fw vehicle_profiles/gmc/sierra-ev.json (CHARGER_DC_PWR), BT1 platform family |
| `0xF187` | **0** | — | probe only | ISO 14229-1 vehicleManufacturerSparePartNumber -- standard identification DID, used here to test reachability, not content |
| `0xF188` | **0** | — | probe only | ISO 14229-1 vehicleManufacturerECUSoftwareNumber -- standard identification DID, used here to test reachability |
| `0xF189` | **0** | — | probe only | ISO 14229-1 vehicleManufacturerECUSoftwareVersionNumber -- standard identification DID, used here to test reachability |
| `0xF191` | **0** | — | probe only | ISO 14229-1 vehicleManufacturerECUHardwareNumber -- standard identification DID, used here to test reachability |

<!-- END GENERATED IDENTIFIER REGISTRY -->

## Candidate identifiers

> **The tiers below are historical narrative; the `Level` column above is the
> answer.** The tiers were written as identifiers were tried and they record
> *when* and *how* each one was proven, which is worth keeping. What they cannot
> do is stay current: they are prose, and prose is what fell thirty-six commits
> behind the code the first time. The generated table is rendered from
> `hummer_obd.confidence`, which is keyed to the safety gate and asserted equal
> to it, and a test recomputes its level-3 cross-validations from the committed
> sessions. When a tier heading and a `Level` cell disagree, the cell is right.

### Tier 1 — proven on this VIN

All six answered with a positive response on 2026-09-02 at 23:20 UTC, vehicle
awake and parked at 13.8 V. All are addressed to `0x14DACBF1` and answer on
`0x142AF1CB` — the same Battery System Manager throughout.

| Identifier | Signal | Decoder | Raw | Value |
|---|---|---|---|---|
| `0x27C6` | HV battery state of charge | `[B4:B5]/655.35` | `CEFA` | 80.85 % |
| `0x27AF` | HV energy remaining | `[B4:B5]/100` | `3C52` | 154.42 kWh |
| `0x27C7` | Remaining range | `[B4:B6]/103` | `006C3F` | 269.04 mi |
| `0x27C0` | Distance since full charge | `[B4:B6]/16.09344` | `0000FE` | 15.78 mi |
| `0x0046` | Temperature | `(B4-40)*1.8+32` | `51` | 105.8 °F |
| `0x5401` | Charger DC power | `[B4:B5]/4350` | `00` | 0.00 kW |

#### A note on units: the source states none

`sierra-ev.json` gives only a parameter name and a formula. It has no unit
field, no range, and no description. Every unit in the table above is therefore
**derived here, not published**, and the derivations differ in strength:

| Unit | How it was arrived at | Strength |
|---|---|---|
| `%` for `0x27C6` | `/655.35` is 16-bit full scale (65535/100) | strong |
| `°F` for `0x0046` | the formula `(B4-40)*1.8+32` is literally a Celsius-to-Fahrenheit conversion | strong |
| `mi` for `0x27C7` | range ÷ SoC gives 333 against a 329 mi EPA figure; in km it would give 333 km against 529 km | strong, empirical |
| `mi` for `0x27C0` | the divisor 16.09344 is the kilometre-to-mile ratio scaled by ten | strong |
| `kWh` for `0x27AF` | the name is `HV_CAPACITY_R`, and energy ÷ SoC gives a pack figure of the right order | moderate |
| `kW` for `0x5401` | the name is `CHARGER_DC_PWR`; the value was zero, so nothing tests the scale | **weak — untested** |

The charger reading is the weakest entry in the table and is marked as such: a
zero tells us the identifier exists and the vehicle was not charging, and
nothing whatsoever about whether `/4350` is the right divisor. That will only be
settled by reading it during a charge.

#### They cross-check each other

This is the part that carries the weight. Six values decoded with six different
published equations were not fitted to one another, yet they agree:

| Derived from | Computation | Result | Independent expectation |
|---|---|---|---|
| range ÷ SoC | 269.04 / 0.8085 | **333 mi at 100 %** | EPA rating for this vehicle is 329 mi |
| energy ÷ SoC | 154.42 / 0.8085 | **191 kWh usable** | right order for this pack |
| energy ÷ range | 154.42 / 269.04 | **574 Wh/mi** | matches this truck's real-world efficiency |
| charger power | 0.00 kW | not charging | SoC was falling across the series, as it must be if nothing is charging |
| temperature | 105.8 °F | plausible | Phoenix, early September, mid-afternoon local |

A range figure that lands within 1.2 % of the published EPA rating, computed
from *two* identifiers whose scalings came from a community JSON file, is not
something a wrong decode produces. Neither is a charger reading of exactly zero
on a vehicle whose state of charge is measurably falling.

#### The byte-offset conflict is resolved

The two source profiles disagreed: `bt1.json` says `[B8:B9]`, `sierra-ev.json`
says `[B4:B5]`, for the same identifier. The verification pass flagged this as
an unsettled hazard. **The vehicle settled it.** Counting `B0` from the ISO-TP
PCI byte — that is, excluding the four-byte CAN header — every one of the six
lands exactly where `sierra-ev.json` says, including the one-byte and
three-byte fields:

```
2227C6   05 62 27 C6 CE FA          B4:B5 = CE FA
2227AF   05 62 27 AF 3C 52          B4:B5 = 3C 52
2227C7   06 62 27 C7 00 6C 3F       B4:B6 = 00 6C 3F
2227C0   06 62 27 C0 00 00 FE       B4:B6 = 00 00 FE
220046   04 62 00 46 51             B4    = 51
225401   04 62 54 01 00             B4    = 00
```

The two conventions differ by exactly four — the CAN header length — so they
describe the same bytes. `bt1.json` counts from the start of the whole frame;
`sierra-ev.json` counts from the PCI byte. Neither is wrong; they are stated
against different origins, and nothing in either file says which. Six frames of
three different lengths agreeing on one convention is what makes this a finding
rather than a preference.

### Tier 2 — BEV3 identifiers that answered on this BT1 vehicle

These come from `OBDb/Chevrolet-Equinox-EV`, which is **BEV3, not BT1**. They
were tried because they address module addresses this vehicle has already named
for itself, and because the same file's entry for `0x27C6` (16-bit, `*100/65535`)
is arithmetically identical to the `/655.35` this vehicle had already confirmed
— an independent third source agreeing on an identifier we had measured.
`7F 22 31` was the expected outcome. All three answered instead.

| Identifier | Module | Signal | Raw | Value |
|---|---|---|---|---|
| `0x2AF5` | `CB` BSM | cell voltage avg / min / max | `9CF9 9CEB 9D1C …` | 4.0185 / 4.0171 / 4.0220 V |
| `0x2B43` | `CB` BSM | 26-byte array (see below) | `C7C6C7…C9C8` | 198–201 per element |
| `0x33E5` | `1D` DMC2 | DMCM battery voltage | `83` | 13.1 V |

#### Cell voltages, and why the decode is trustworthy

`0x2AF5` is the most valuable identifier found so far, because cell balance is
the earliest visible sign of a failing module and was listed in this project's
own documentation as unobtainable.

The decode is not merely plausible, it is *constrained*. The published labels
are average, minimum and maximum, in that order, and the observed values are:

```
avg 4.0185 V    min 4.0171 V    max 4.0220 V
```

`min < avg < max` holds exactly. A wrong byte offset would break that ordering
almost every time, so the ordering surviving is evidence the offsets are right.
The magnitude corroborates it independently: a nickel-manganese-cobalt cell sits
near 4.0 V at 80 % state of charge, and this pack was independently measured at
80.85 % moments earlier by a different identifier.

**Cell spread is 4.9 mV**, which is a very tightly balanced pack.

Four further bytes (`740F B317`) arrive in the same response and are *not*
explained by the source, which describes only three fields. Under the same
scaling they read 2.9711 V and 4.5847 V — suggestively like limits rather than
measurements, but that is a guess and is recorded as one. They are stored raw
and left undecoded.

#### `0x2B43` is an array, not the scalar the source describes

`OBDb` describes `0x2B43` as a single byte scaled `*100/255` to a percentage.
This vehicle returned **26 bytes**, values `198`–`201`, in a multi-frame
response. The source's interpretation does not fit, so it is not applied.

Under the published scaling the elements span 77.65 %–78.82 %, while the
high-resolution identifier `0x27C6` read 80.85 % at the same moment. Whether
this is per-module state of charge, a cell-group array, or something else is
**unresolved**, and guessing would be exactly the error this project keeps
warning about. The bytes are recorded; the meaning is not claimed.

That a source's scaling turned out to be wrong for this vehicle, on the very
same run where two others turned out to be right, is the argument for storing
raw frames rather than decoded values.

### Tier 2b — chassis dynamics from the brake system controller

All five answered on 2026-09-02, addressed to `0x14DA28F1` and answering on
`0x142AF128`. Address `28` is `BSCM-BrakeSystem`, which this vehicle names for
itself.

| Identifier | Signal | Raw | Value (parked) |
|---|---|---|---|
| `0x4A7A` | wheel speed, four corners | `00000000` | 0, 0, 0, 0 km/h |
| `0x4A7C` | brake pressure | `0A` | 0 kPa |
| `0x4C2D` | steering wheel angle | `02AF` | 15.11° |
| `0x4C2F` | lateral acceleration | `0000` | 0.0000 g |
| `0x4C30` | longitudinal acceleration | `FFF8` | −0.0127 g |

Every reading is what a stationary vehicle should produce: all four wheels at
zero, the brake pedal released, the front wheels left slightly turned, no
lateral acceleration, and a longitudinal figure within a hundredth of a g of
level ground. Values that are individually plausible *and* jointly consistent
with the vehicle's actual physical state are much harder to get by accident
than a single number in range.

**These scalings are the best-evidenced in this document.** The source is not a
stated formula but a set of test fixtures pairing a *captured response* with its
*expected decoded value*. That means the equation could be derived from the
vectors and then checked against every one of them, which it was:

```
224A7C   0x0A -> 0 kPa      0x0E -> 400     0x0F -> 500     0x10 -> 600
         (byte - 10) * 100 reproduces all four exactly

224C2D   0x002D -> 0.99     0x24F7 -> 208.186
         0xE407 -> -157.542 0xFF15 -> -5.17
         signed16 * 0.022 reproduces all four exactly, including both signs

224C2F   0xFF50 -> -0.28033     0xFFFC -> -0.00637
         signed16 * 0.0015928 reproduces both exactly
```

A formula recovered from data and confirmed against every available vector is a
stronger claim than one copied out of a file, because a transcription error
survives copying but not arithmetic.

#### A correction

An earlier version of this document listed these identifiers under Tier 3 as
"not sourced", on the grounds that `OBDb/Cadillac-LYRIQ`'s signalset returns
`{"commands": []}`. That was **wrong, and wrong in the direction that loses
information**: the signalset is an empty stub, but the repository's
`tests/test_cases/2024/commands/` directory holds the identifiers along with
real captured frames. Checking one file in a repository and concluding the
repository is empty is exactly the sort of shortcut this project's evidence
rules exist to prevent, and it was caught only because the question was asked a
second time.

The same directory's three `DACB` identifiers are the ones this vehicle had
already answered, so the fixture set was three-for-three on this truck before
any of these five were sent.

### Tier 2c — the same identifiers aimed at sibling modules

This vehicle names three drive motor controllers and two battery system
managers. Until 2026-09-03 only one of each had ever been asked anything.
Pointing an *already-sourced* identifier at a sibling module of the same type is
not guessing an identifier — the identifier still comes from a source, and only
the destination changes to a module the vehicle named for itself.

| Identifier | Module | Result |
|---|---|---|
| `0x33E5` | `17` `DMCM-DriveMotorCtrl` | `84` → 13.2 V |
| `0x33E5` | `1E` `DMC3-DriveMotorCtrl3` | `83` → 13.1 V |
| `0x27C6` `0x27AF` `0x2AF5` `0x2B43` | `CD` second `BSM-BatterySysMngr` | `7F 22 31` on all four |

**All three drive motor controllers answer, with different values.** A 0.1 V
spread across `17`, `1D` and `1E` is what independent measurements of the same
rail look like; three identical bytes would have suggested a shared constant or
a gateway artefact instead. It remains a 12 V reading and must not be called
pack voltage.

**Module `CD` refuses every identifier that works on `CB`** — and that is the
more valuable half. `7F 22 31` is `requestOutOfRange`: a properly formed UDS
negative response, on `CD`'s own response identifier, rather than silence. So
`CD` **is** alive and **does** serve service 22; it simply does not hold `CB`'s
identifiers. A module with no service 22 would have answered `7F 22 11`; an
asleep or unreachable one would have answered nothing.

The two battery managers are therefore not mirrors of one another, and `CD` is a
proven service-22 responder whose entire identifier space is undiscovered. Full
topology in [the module map](GM_MODULE_MAP.md).

### Tier 3

Nothing is added to `ENHANCED_READ_DIDS` without a fetchable source that names
the exact identifier, and being listed as a candidate never means "safe to
send" until it has been through the same review.

Deliberately **not** added, despite appearing in research output:
* **`OBDb/GMC` priority-14 identifiers.** There are 198 of them, but every one
  targets module `11`, which is not among the eight modules this vehicle names.
* **`0x8334`**, reported against Bolt platforms. Sources disagree on scaling
  and it is not BT1. Still excluded.

> **`0x2885` was listed here too, and that was wrong.** It was added, it was
> sent, and on 2026-09-03 it answered from modules `17`, `1D` and `1E` with a
> pack voltage that cross-validates against the pack energy and state-of-charge
> route to better than a percent — the reading the commit history calls "the
> project's largest gap, closed". This line told a reader to distrust the
> project's headline result for as long as it stood. The lesson is not about
> `0x2885`: an exclusion list is a claim like any other, and nothing was
> re-reading it when the claim stopped being true. Its row is in the generated
> table above.

**A lesson from `0x27C7`.** An earlier version of this project used `0x27C7` in
its tests as the example of a fictional identifier "a sweep would try next",
because it sits one step from `0x27C6`. It is not fictional — it is the range
identifier, and it is now Tier 1. Nearness to a real identifier is no evidence
in either direction, which is exactly why the rule is enumeration from a source
and never distance from something that worked.

---

## Provenance

The addressing and `0x27C6` come from `vehicle_profiles/bt1/bt1.json` in
[`meatpiHQ/wican-fw`](https://github.com/meatpiHQ/wican-fw), fetched
2026-09-02 from `raw.githubusercontent.com`
(sha256 `26dc621adf2e6b2090798c41c2da45abb1db9d00429613b739942f4904e6600d`).
Its `car_model` field names this vehicle explicitly:

> `BT1: Hummer EV, Silverado EV, Sierra AV; BEV3: Cadillac Lyriq, Celestiq, Chevrolet Blazer EV, Equinox EV; Honda Prologue EV; Acura ZDX`

The profile's `pid_init` is a single semicolon-separated string. It is split
into individual commands here because this project's safety gate refuses
batched commands, so each one is validated and sent on its own.

The other five identifiers come from `vehicle_profiles/gmc/sierra-ev.json` in
the same repository (sha256
`19ca7a20fa73de8ebca445ead27df119d4fc5ca394bd8241a13bd2784dc985ef`), whose
`car_model` is `GMC: Sierra EV`. That is not this vehicle — but `bt1.json`'s own
`car_model` groups the Sierra EV with the Hummer EV on the BT1 platform, and the
profile targets the identical request and response identifiers, so it is
addressing the same module in the same way. Every one of its identifiers
answered on this truck.

**The addressing was taken from `bt1.json`, not from `sierra-ev.json`, and that
matters.** `sierra-ev.json` opens with `ATSP6` — ISO 15765-4 *11-bit* — while
using a 29-bit header, which cannot be right for this vehicle and is most
likely a defect in that file. `bt1.json` selects `ATSP7`, sets the priority byte
with `ATCP14`, and configures flow control. Taking the headers from one profile
and the identifiers from the other is deliberate.

---

## What this does not change

* **The gateway is still a boundary.** One module answering one identifier does
  not mean the DLC exposes the vehicle's internal networks.
* **CAN FD is still out of reach.** The adapter implements Classical CAN only.
* **GPS is still not on this path.** Location remains an OnStar/telematics
  concern and stays out of this read-only collector.
* **Nothing here is a write.** Service `22` is a read. Every write, control,
  reset, security and actuator service remains in `FORBIDDEN_SERVICES`.

---

## Reproducing it

Dry run first — this transmits nothing and does not open the serial device:

```bash
PYTHONPATH=src python3 -m hummer_obd.enhanced --profile bt1
```

Then, with the vehicle **awake and attended**, and the dashboard state of
charge noted at the same moment:

```bash
PYTHONPATH=src python3 -m hummer_obd.enhanced --profile bt1 --confirm \
    --raw-log logs/enhanced-raw.jsonl \
    --output evidence/enhanced-bt1.json
```

The connector voltage is recorded alongside every read on purpose: a `NO DATA`
at 12.8 V (asleep) and a `NO DATA` at 13.8 V (awake) are different results and
must not be filed as the same one.
