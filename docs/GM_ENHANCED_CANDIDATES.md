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

## Candidate identifiers

### Tier 1 — proven on this VIN

| Identifier | Signal | Request | Response | Decoder | Status |
|---|---|---|---|---|---|
| `0x27C6` | HV battery state of charge | `0x14DACBF1` | `0x142AF1CB` | `[B8:B9] / 655.35` | **positive response, reproducible; value not yet cross-checked against the dashboard** |

### Tier 2 and Tier 3

Research into further identifiers is ongoing. Nothing is added to
`ENHANCED_READ_DIDS` without a fetchable source that names the exact
identifier, and being listed as a candidate never means "safe to send" until
it has been through the same review.

---

## Provenance

The Tier 1 identifier comes from `vehicle_profiles/bt1/bt1.json` in
[`meatpiHQ/wican-fw`](https://github.com/meatpiHQ/wican-fw), fetched
2026-09-02 from `raw.githubusercontent.com`
(sha256 `26dc621adf2e6b2090798c41c2da45abb1db9d00429613b739942f4904e6600d`).
Its `car_model` field names this vehicle explicitly:

> `BT1: Hummer EV, Silverado EV, Sierra AV; BEV3: Cadillac Lyriq, Celestiq, Chevrolet Blazer EV, Equinox EV; Honda Prologue EV; Acura ZDX`

The profile's `pid_init` is a single semicolon-separated string. It is split
into individual commands here because this project's safety gate refuses
batched commands, so each one is validated and sent on its own.

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
