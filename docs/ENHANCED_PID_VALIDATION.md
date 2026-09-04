# Enhanced PID validation plan

Status: **one identifier has now cleared this bar and been read from the
vehicle.** See [GM enhanced candidates](GM_ENHANCED_CANDIDATES.md) for the
result and [SAFETY.md](SAFETY.md) for the change record. The rest of this
document stands unchanged and still governs every *further* identifier.

Mode `22` remains rejected by `validate_command`, the gate every unattended
path uses; the supervised read went through a second, narrower gate. No
candidate identifier value appears anywhere below — placeholders (`22 XX XX`)
are used deliberately, because inventing a plausible-looking identifier is
exactly the mistake this document exists to prevent. The only literal
identifiers written here are the two endpoints of the sweep this document
forbids.

### How `0x27C6` scored against the bar below

Honesty about process matters more than a clean story, so: this identifier was
**not** approved by filling in the paper record below and then transmitting.
It was transmitted once, under supervision, on the strength of a single
published source that named this vehicle explicitly. That is a deviation from
the written procedure and is recorded as one.

What made it defensible, and what the bar had not anticipated:

* **The module address corroborated itself.** The source addresses the request
  to module `CB`. This vehicle had already told us, through its own per-address
  service 09 PID `0A` query, that `CB` is `BSM-BatterySysMngr` — its Battery
  System Manager. A source found on the internet and a measurement taken from
  the truck independently agree that a battery state-of-charge identifier lives
  at the battery manager. That is the "independent corroboration" the bar asks
  for, arriving from the vehicle rather than from a second document. The module
  map above had already predicted it in as many words: *"two are battery system
  managers, which is where any pack, cell or thermal identifier would live if
  one is ever validated."*
* **The response validated the scaling.** The bar rejects an equation "inferred
  from one observed value". Repeated reads five minutes apart returned
  different values that move smoothly in the range a percentage occupies, which
  tests the equation in a way a single reading cannot.
* **One correction to the bar itself.** The "Request header" row requires
  `18DA<ecu>F1` and rejects anything else. That is too narrow: GM enhanced
  diagnostics on this platform use CAN priority `0x14`, so the correct physical
  header is `14DACBF1`, and the reply arrives as `142AF1CB` rather than
  `18DAF1CB`. The row should be read as "physical addressing to a single named
  module, never the functional broadcast `18DB33F1`" — which the request
  satisfied.

The requirement that continues to bind, without exception, is the one in
[Why identifier sweeping is forbidden](#why-identifier-sweeping-is-forbidden).
One sourced identifier was sent once. Nothing was iterated, and the gate
refuses the identifiers immediately adjacent to it.

This is the written form of the first requirement in the change-control rules of
[the safety model](SAFETY.md): an explanation of why a command would be
read-only for *this* protocol and *this* vehicle, written before the command is
added rather than after it is tried.

## The link, as confirmed live

These facts come from supervised probes of this vehicle, not from
documentation about the platform. The curated, masked summary of those probes
is published in [docs/VALIDATION.md](VALIDATION.md); the byte-exact transcripts
they are drawn from stay private on the node.

| Property | Confirmed value |
|---|---|
| Vehicle | GMC Hummer EV, GM Ultium platform |
| Adapter | OBDLink MX+ (STN2255) over Bluetooth SPP to a Raspberry Pi |
| Protocol | ISO 15765-4, CAN 29-bit identifiers, 500 kbit/s (ELM protocol 7) |
| Response framing | 4-byte identifier before the ISO-TP PCI byte |
| Physical response form | `18DAF1<ss>`, where `F1` is the tester and `<ss>` the responding ECU |
| Modules answering `03`/`07` | eight distinct modules, zero codes each |
| Modules answering `0A` | five distinct modules, zero codes each |
| Module observed refusing (`7F <svc> 22`) | address `28`, `BSCM-BrakeSystem` |
| Service 09 PID `0A` | full module names, mapped to addresses (below) |
| Service 09 PID `04` | calibration IDs `135240240857240850` |

### The module map, measured

This was an open prerequisite when this document was written and is now
closed. Each address was queried behind its own `ATCRA18DAF1<addr>` receive
filter and answered service 09 PID `0A` for itself, so every pairing is
measured rather than inferred from the order of a concatenated string:

| Address | Module | | Address | Module |
|---|---|---|---|---|
| `17` | `DMCM-DriveMotorCtrl` | | `40` | `BCM-BodyControl` |
| `1D` | `DMC2-DriveMotorCtrl2` | | `45` | `Gateway Module - GWM` |
| `1E` | `DMC3-DriveMotorCtrl3` | | `CB` | `BSM-BatterySysMngr` |
| `28` | `BSCM-BrakeSystem` | | `CD` | `BSM-BatterySysMngr` |

**An earlier version of this document called address `28` the gateway.** That
was an inference from `28` being the only module still answering `7F <svc> 22`
while the vehicle shut down, and it was wrong: `28` is the brake system
controller and the gateway is `45`. The module that stays reachable longest
during shutdown is the brake controller. That correction matters here more than
anywhere else in the project, because an enhanced-PID request has to be
addressed to a specific module, and addressing the wrong one is exactly the
class of mistake this document exists to prevent.

It is also a worked example of the rule below: the original pairing looked
reasonable, was derived from real observed behaviour, and was still wrong.
Only the per-address query settled it.

Two further consequences matter. Three of the eight modules are drive motor
controllers and two are battery system managers, which is where any pack, cell
or thermal identifier would live if one is ever validated. And the calibration
IDs identify this build: a source claiming to describe this vehicle should be
checked against them rather than against the model name.

## Why this is deferred

[docs/SAFETY.md](SAFETY.md) states the rule plainly: service `22` is deferred,
not permitted. It is read-only *in principle* — the service reads a data
identifier and returns its value — but "read-only in principle" is a property
of the service, not of a particular request. A guessed identifier is an
unreviewed request to an unknown ECU. Two things follow.

* The identifier space is manufacturer-defined. On a GM Ultium vehicle, what a
  given identifier means, which module owns it, and whether it is even a data
  identifier rather than a routine or control record is not knowable from the
  standard. It is knowable only from evidence.
* The addressee is unknown until it is proven. This vehicle answered service
  `03`/`07` from eight distinct modules and service `0A` from five. Sending an
  unproven identifier physically addressed to the wrong one of them is not a
  read; it is an unspecified request whose handling is undocumented.

A third-party app label is not evidence. An Internet PID list is not evidence.
Neither is a language model's recollection of one. All three are unattributed
copies of each other often enough that agreement between them proves nothing.
What counts as evidence is defined below, and the bar is deliberately higher
than "it looked right when someone tried it".

## What we would gain

This section is the entire motivation, stated honestly: standard OBD-II on this
truck does not expose its battery.

Service `01` support on this VIN advertises exactly 14 PIDs:

```text
01 0D 1C 1F 20 21 30 31 40 42 60 80 A0 A6
```

Of those, three have been proven to return values in a supervised probe: `0D`
(vehicle speed), `1F` (run time since engine start) and `42` (control module
voltage). `5B` (hybrid/EV battery pack remaining life) is **not advertised** by
this vehicle, and neither is `46` (ambient air temperature). The concrete gap
is therefore:

| Signal we want | Standard OBD-II availability on this VIN |
|---|---|
| State of charge | Not available — `5B` not advertised |
| Pack voltage | Not available |
| Pack current / power | Not available |
| Pack temperature | Not available |
| Cell balance / cell delta | Not available |
| DC fast-charge state and rate | Not available |
| Remaining range | Not available |
| Ambient air temperature | Not available — `46` not advertised |

Everything the node can honestly display today about the battery is the 12 V
control-module voltage, which says nothing about the high-voltage pack. That is
the whole reason enhanced identifiers are interesting, and it is not a good
enough reason on its own to transmit an unproven request.

## Evidence bar per identifier

Each candidate identifier needs a complete record before it is proposed for the
allowlist. Incomplete records are not "partially approved"; they are rejected
until complete. **Two independent sources are required for every row that is
marked as needing corroboration, and a single application's sensor list is ONE
source.**

| Required artifact | What the record must state | The record fails review if |
|---|---|---|
| Identifier | The exact request bytes, written as `22 XX XX` with no wildcards | It is expressed as a range, a pattern, or "the `22 XX` family" |
| Target ECU address | The single responding module address, quoted from the private transcript's own `18DAF1<ss>` response headers for this VIN, and shown to be one of the modules this vehicle actually answered from | The address is not traceable to a transcript line, the target is "the powertrain module" by name only, or the request is functional |
| Request header | The exact physical-addressing header for that module (`18DA<ecu>F1`), never the functional header `18DB33F1` | It proposes broadcasting the identifier to every module at once |
| Expected response length | Byte count of the positive response payload after the `62 XX XX` echo, and whether it is single-frame or ISO-TP multi-frame | Length is unknown, or "varies" |
| Scaling and offset | The full equation from raw bytes to engineering value, including byte order, signedness, resolution and offset | Only a resolution is given, or the equation is inferred from one observed value |
| Units | The engineering unit and the plausible range for this vehicle | Units are guessed from the name of the signal |
| Source | Where the record came from, named specifically, with the date and the vehicle or document it describes | The source is "a forum", "the Internet", or unattributed |
| Independent corroboration | A second source that agrees on **both** the identifier **and** the equation, obtained independently of the first | The second source is a copy, mirror, or restatement of the first |

The corroboration requirement is the one that does the work. Two sources that
agree on the identifier but disagree on scaling are a failed corroboration, not
a rounding difference: it means at least one of them is describing a different
vehicle, a different model year, or a different module.

A per-candidate record looks like this, and is filled in on paper:

```text
identifier:      22 XX XX
target ECU:      <one address from the observed set>
request header:  18DA<ecu>F1
response:        62 XX XX <n bytes>, <single-frame | multi-frame>
equation:        value = (A * 256 + B) * <resolution> + <offset>
units:           <unit>, expected range <lo>..<hi>
source 1:        <named source, date, vehicle/document>
source 2:        <independent named source, date>
agreement:       identifier YES/NO, equation YES/NO
```

One further prerequisite belongs on the same page rather than being discovered
at the vehicle: the adapter-side addressing this would need must be expressible
under the *existing* gate. `ATSH`, `ATCRA`, `ATCF` and `ATCM` are already
allowlisted, but `ATSH` is allowed with three to six hex digits — the low
three bytes of a 29-bit identifier, which is the ELM convention — and the
fixed priority byte of a 29-bit header is set with a separate command that is
**not** allowlisted today. Whether physical addressing to a single module is
reachable without a second gate change must be answered on paper, before step
(e), not by sending something and watching what happens.

## Acceptable evidence sources, ranked

1. **A sensor or PID export Jeremy produces from this vehicle.** A Car Scanner
   or OBDLink profile export, taken from this VIN, is the strongest available
   source because it describes the truck in question rather than a nominally
   similar one. It is still only one source.
2. **A published GM service procedure.** A manufacturer service document that
   names the identifier and its scaling for this platform and model year. This
   is authoritative about intent even where it is silent about the specific
   build.
3. **A second independent community database.** Acceptable as corroboration
   only when it agrees on the identifier *and* the equation, and only when it
   can be shown to be independent of source 1 — a different maintainer, a
   different collection method, not a fork or a scrape of the first.

Explicitly rejected as evidence, at any rank:

* **A single forum post.** One person's screenshot of one vehicle, with no
  method and no second observer.
* **A language model's answer**, including this one. It reproduces the same
  unattributed lists and cannot distinguish a well-sourced identifier from a
  widely-copied guess.
* **"Try it and see."** Sending a candidate to find out what it does is not a
  source of evidence; it is the act the evidence is supposed to authorise. A
  response that decodes to a plausible number is not proof of meaning — a
  plausible number is the most likely way to be wrong without noticing.

## Why identifier sweeping is forbidden

Iterating `22 00 00` through `22 FF FF` — or any bounded subset of it — is
banned outright, and would remain banned even if the results were interesting.

* **It is 65,536 unreviewed requests.** Every single one of them fails the
  first change-control rule in [SAFETY.md](SAFETY.md), which requires a written
  explanation per command. A sweep is the explicit refusal to write one.
* **It produces a `requestOutOfRange` storm.** The overwhelming majority of
  identifiers are unsupported, and each one costs the module a negative
  response. This vehicle has already demonstrated that it will return `7F <svc>
  22` (`conditionsNotCorrect`) when it does not want to serve diagnostics; a
  sweep converts that from a single observation into sustained bus load
  directed at one module.
* **Some GM modules treat diagnostic traffic as session state.** A sustained
  request stream can hold or provoke a diagnostic session, and session state
  has side effects that a read-only project has no business inducing — up to
  and including modules that behave differently while a tester is perceived to
  be present.
* **A sweep is indistinguishable from an attack.** To a gateway that logs, an
  exhaustive identifier enumeration from an aftermarket adapter is precisely
  the shape of an intrusion attempt. Producing that record on a vehicle,
  deliberately, is not defensible with "I only wanted the state of charge".

The project's premise is that unknown commands are rejected rather than
guessed at. A sweep is guessing, industrialised.

## Staged plan

Each stage completes and is reviewed before the next one starts. No stage is
skipped because an earlier one "obviously" succeeded.

| Stage | Action | Vehicle involved | Exit condition |
|---|---|---|---|
| a | Receive the sensor/PID export from Jeremy for this VIN, and recover the responding-module addresses from the private transcript | No | Export archived privately alongside the raw logs; the eight/eight/five responder addresses written down from actual `18DAF1<ss>` response headers |
| b | Map candidate fields to identifiers on paper | No | Every candidate has a complete record per the evidence bar, with two independent sources |
| c | Write decoder plus tests against **synthetic** frames | No | Tests pass with no adapter and no vehicle present; equations exercised at range boundaries |
| d | One reviewed `safety.py` change | No | Only the specific proven identifiers are added, as exact `22XXXX` strings |
| e | One supervised live request | Yes | Single request, logged byte-exact, vehicle stationary and attended |
| f | Transcript review | No | Response reviewed offline and reconciled against the predicted length and value |
| g | Update [docs/VALIDATION.md](VALIDATION.md) | No | Result published in masked, curated form |

Notes that constrain the stages:

* **Stage c is where the work happens.** Decoding is developed and proven
  against synthetic ISO-TP frames constructed from the documented response
  shape. If the decoder cannot be written and tested without the vehicle, the
  evidence was not complete enough to justify the request.
* **Stage d adds an allowlist of exact strings, never a pattern.** A rule such
  as `^22[0-9A-F]{4}$` re-opens the entire identifier space and is precisely
  the change this document exists to forbid. The forbidden-service checks are
  not touched, and tests must prove that unlisted `22` requests, batched
  variants, and functional addressing still transmit zero bytes.
* **Stage e is one request.** Not one per identifier, not a small batch — one,
  physically addressed to one module, with the vehicle stationary, in park, and
  a person present who can disconnect the adapter.
* **Stage f gates stage e's repetition.** A second live request is not sent
  until the first transcript has been reviewed offline and the decoded value
  reconciled with the predicted range. An unexpected length, an unexpected
  negative response code, or a value outside the predicted range stops the
  process and returns to stage b.

## A passive alternative that needs no Mode 22

There is an approach that would obtain broadcast EV data without transmitting
any diagnostic request at all. `ATMA` (monitor all) and the `STM`-family
monitoring commands put the adapter in a receive-only mode: it listens to CAN
traffic the vehicle is already broadcasting and prints frames. Nothing is
addressed to any module, no identifier is guessed, and no diagnostic session is
implied.

That property makes it worth evaluating on its own merits, precisely because
the risk it removes is the one this document is about. It is also not free of
work: broadcast frames are unlabelled, so identifying which arbitration ID
carries state of charge is its own evidence problem, and monitor mode changes
how the adapter handles the bus.

**This was not approved when it was written. It is now, and it has been run.**
On 2026-09-03 a bounded, supervised passive capture path was built as
`hummer-obd-passive` ([`src/hummer_obd/monitor.py`](../src/hummer_obd/monitor.py))
behind **two gates of its own**, and on 2026-09-04 it recorded **zero bytes in
thirty seconds** at the connector — the decisive negative. Details in
[the passive CAN validation note](PASSIVE_CAN_VALIDATION.md) and
[the validation record](VALIDATION.md#passive-can-capture-at-the-diagnostic-connector-2026-09-04).

What did **not** change, and is the part this paragraph should have been
protecting: `ATMA` and `STM` are still rejected by the current gate exactly like
any other unlisted adapter command — and so are `STMA`, `STCMM0`, `STCMM1` and
`STCMM2`. `_ALLOWED_AT_EXACT` was never widened, because it feeds
`validate_command`, which is the unattended collector's gate. All six are put to
that live gate in `capabilities.GATE_REFUSE_SAMPLES`, so a future widening flips
a published report entry from refused to accepted in plain sight.
Enabling either would require the same five-part change-control process in
[SAFETY.md](SAFETY.md) — written justification, an allowlist update that does
not weaken the forbidden-service checks, tests proving unsafe variants still
transmit zero bytes, an offline/simulated acceptance path, and a supervised
first live use with byte-exact logging and transcript review. It is listed here
as a candidate for evaluation, not as a fallback that can be reached for
because Mode 22 is blocked.

## Rollback

Rollback covers two different things, and only one of them is a code change.

**Reverting the software.** Revert the reviewed `safety.py` allowlist change
first, then the decoder module and its tests, then any configuration that names
an enhanced identifier in the polling set. After the revert, the acceptance is
that the gate rejects every `22XXXX` string — including the identifiers that
were briefly allowed — and that the existing safety tests pass unchanged. A
partial revert that leaves one identifier allowlisted is not a rollback.

**Proving the vehicle is unaffected.** A code revert cannot un-send a request,
which is why the gate exists in the first place. The vehicle-side check is a
comparison against the pre-change baseline recorded in
[docs/VALIDATION.md](VALIDATION.md):

* re-run services `03`, `07` and `0A` and confirm zero stored, pending and
  permanent codes, from the same eight, eight and five responding modules;
* confirm the standard reads that worked before still work and still return
  plausible values (`010D`, `011F`, `0142`);
* confirm `ATRV` and control-module voltage are consistent with the earlier
  probe, with the vehicle in a comparable state;
* confirm the vehicle's own driver information centre shows no new message; and
* archive the byte-exact raw transcript of the live request and record its
  SHA-256, so that what was sent remains reviewable regardless of what the
  repository looks like afterwards.

If any of those checks differs from the baseline, the correct response is to
stop, preserve the transcripts, and treat it as an incident to be written up —
not to send a follow-up request to investigate.
