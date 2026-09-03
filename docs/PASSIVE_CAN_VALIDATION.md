# Passive CAN monitoring: a negative research result

Status: **research note. No code, no allowlist change, nothing approved.**
`ATMA` is rejected by [`src/hummer_obd/safety.py`](../src/hummer_obd/safety.py)
today, [`src/hummer_obd/capabilities.py`](../src/hummer_obd/capabilities.py)
puts it to the live gate in `GATE_REFUSE_SAMPLES` so that a weakened allowlist
would show up in the capability report as an accepted command, and this
document changes neither. It exists for the opposite reason to an approval: to
record what was searched for, what was not found, and what the single bounded
experiment worth running would look like — so the next person with this idea
spends an evening on it instead of a month.

[The enhanced PID validation plan](ENHANCED_PID_VALIDATION.md) lists passive
monitoring "as a candidate for evaluation, not as a fallback that can be
reached for because Mode 22 is blocked". This is that evaluation. The short
version: the fallback is probably not there, one useful fact was confirmed
along the way, and the research had limits worth stating out loud.

## Why this was investigated

Standard OBD-II on this truck does not expose its battery. Service `01` support
on this VIN advertises fourteen PIDs — `01 0D 1C 1F 20 21 30 31 40 42 60 80 A0
A6` — and none of them is a high-voltage signal. `5B` (hybrid/EV battery pack
remaining life) is not advertised; neither is `46` (ambient air temperature).

> **This section has been overtaken by events, and the correction is left
> visible rather than rewritten away.** When it was written, the premise was
> that the pack was unreachable and passive monitoring was the only route left.
> That premise is now false. On 2026-09-02 and 2026-09-03 this vehicle answered
> fourteen enhanced identifiers across four modules, including state of charge,
> remaining energy, range and per-cell voltage statistics. The column below is
> updated; the reasoning that follows it is preserved as written, because a
> research note whose conclusion moved is more useful with the move shown.

| Signal wanted | Why it matters | Status on this VIN |
|---|---|---|
| State of charge | The single number the display exists to show | **Obtained** — enhanced read `0x27C6`, not passively |
| Pack voltage | Health and charge state of the traction battery | **Still not obtained.** `0x33E5` reads ~13.1 V from each drive motor controller, which is the 12 V domain, not the pack |
| Pack current / power | Draw while driving, rate while charging | **Still not obtained** — no sourced identifier found |
| Pack temperature | Thermal behaviour under load and fast charge | Partly: `0x0046` returns a temperature the vehicle holds, semantics not yet confirmed |
| Cell balance / delta | The earliest visible sign of a failing module | **Obtained** — `0x2AF5` gives cell average, minimum and maximum; measured spread 4.9–5.3 mV |
| DC fast-charge state | Session progress and taper behaviour | `0x5401` responds but returns one byte where the source describes two; untested during an actual charge |
| Remaining range | The vehicle's own estimate, logged over time | **Obtained** — `0x27C7` |

So the honest position has inverted. Passive monitoring was investigated as the
route *around* a blocked Mode 22; Mode 22 turned out not to be blocked in the
way assumed, and it delivered most of the list above. What remains genuinely
missing — pack voltage, pack current, instantaneous power — is missing because
**no source names an identifier for it**, not because the path is closed.

That changes what a passive capture is *for*. It is no longer a fallback for
data we cannot otherwise reach. It is now a narrower question: does the
diagnostic connector carry any unsolicited broadcast traffic at all? A quiet
result would say the gateway does not forward much to this connector — it would
not say the vehicle's internal networks are quiet. The experiment is still
worth running, and it is still not approved, for exactly the reasons below.

## What was searched for, and what was found

Every row below is a negative except the last, and the negatives are the point.

| Question | Finding | Source | Confidence |
|---|---|---|---|
| Does GM Global B gateway-isolate the OBD-II DLC? | Effectively yes, from the consumer's point of view | openpilot vehicle-support wiki: all 2024+ GM EVs unsupported, "Global B ... has encrypted messaging. Currently there is no way around this" | High for the outcome, medium for the mechanism |
| Is there an opendbc DBC for Global B, Ultium or Hummer EV? | Zero. Every GM DBC in the repository is `gm_global_a_*`; no Hummer, Lyriq or Silverado EV platform entry exists | The opendbc repository file listing | High |
| Is there published Hummer EV CAN reverse engineering? | None findable | Live GitHub code and repository search | Medium |
| Is there a citable Mode 22 DID for Ultium? | None carrying an identifier, a target ECU **and** a scaling equation together | The evidence bar in [ENHANCED_PID_VALIDATION.md](ENHANCED_PID_VALIDATION.md), applied to everything found | Medium-high |
| Is GPS reachable from the DLC? | Realistically no | No GPS signal appears in any GM Global-A, Rivian or Tesla Model 3 DBC; location lives in the OnStar/VCIM telematics domain | Medium |

Three of those confidence ratings deserve their qualifier spelled out.

**The mechanism is less certain than the outcome.** The openpilot wiki says
*encrypted messaging*, which is not the same claim as *the gateway forwards
nothing to the DLC*. Those are different architectures with the same practical
consequence for a listener on pins 6 and 14, and nothing found in this research
distinguishes them. The honest statement is: the community with the most
experience of GM buses treats 2024+ GM EVs as closed, and does not describe a
workaround. Whether the barrier is filtering, authentication, encryption, or
all three is not established here.

**Absence in opendbc is a strong signal, not a proof.** opendbc is the largest
maintained public collection of vehicle CAN definitions, and it is where a
working Global B decode would be expected to land. Its silence means no
published, maintained decode exists. It does not mean nobody privately has one.

**The GPS finding is an argument, not a measurement.** It rests on the shape of
several other manufacturers' buses plus the architectural observation that
telematics modules own location, and it was not tested against this vehicle.
It is offered as a reason not to plan on GPS, not as proof it is absent.

### The nuance that keeps this fair

comma.ai has decoded GM high-voltage pack data before. The opendbc file
`gm_global_a_high_voltage_management.dbc` carries per-cell battery voltages for
the Bolt EUV. So the technique is real, it has been done on a GM EV, and the
resulting signals are exactly the ones this project wants.

Two things stop that from transferring:

* it is Global **A**, the previous-generation architecture, not the Global B
  bus in this truck; and
* even on Global A the tap is behind the forward camera, in the middle of the
  vehicle's own network, not at the OBD-II connector.

So the correct summary is not "nobody can read GM pack data". It is: **the
technique works, and the access does not transfer.** A result obtained from a
harness spliced into a camera connector on a different platform says nothing
about what a diagnostic connector on this platform will hand over.

## The one positive finding: silence is genuinely available

This settles the safety question, and only the safety question.

The vendor's own *OBDLink Family Reference and Programming Manual* documents
`STCMM`, which sets how the adapter's CAN controller behaves while monitoring:

| Mode | Documented behaviour |
|---|---|
| `0` | Receive only — no CAN ACKs (default) |
| `1` | Normal node — with CAN ACKs |
| `2` | Receive all frames, including errors — no CAN ACKs |

Mode `0` is the default, and it is the one that matters here. On CAN, a
listening node normally asserts a dominant bit in the ACK slot of every frame
it receives correctly. That is a transmission. It is short, it is not addressed
to anyone, and it is invisible in any frame log — and it still means the
adapter is driving the bus, which is precisely what this project promises it
does not do. `voltage.py` already draws that line for the 12 V watch, where the
guarantee is narrower than read-only: **nothing reaches the vehicle at all**.
A monitor mode that ACKs would fail that test.

`STCMM 0` is therefore the difference between "we only read" and "we do not
transmit", and it is documented by the manufacturer of the hardware in this
project rather than inferred from behaviour. That is the strongest class of
source this project recognises for an adapter-side claim.

The same manual marks `ATMA`, `ATMR` and `ATMT` deprecated in favour of `STM`
and `STMA`. So `ATMA` remaining refused by the gate is not an oversight to be
tidied up later: it is a deprecated command, and if a monitor command is ever
allowlisted it should be the ST-family one, chosen deliberately and named
exactly.

What this finding does **not** do is make passive monitoring likely to work. It
removes the transmission objection. It says nothing about whether there is
anything to hear.

## Honest limits of this research

Stating these plainly matters more than the findings do, because a negative
result oversold is just a positive result with the sign flipped.

* **Live search engines, Reddit and GM-Trucks were bot-blocked from the
  research sandbox.** Scattered forum claims therefore cannot be ruled out.
  What can be said is narrower and, for this project's purposes, more useful:
  nothing citable exists in the places that matter — opendbc, GitHub, and
  vendor documentation. A forum post would not clear the evidence bar in
  [ENHANCED_PID_VALIDATION.md](ENHANCED_PID_VALIDATION.md) anyway, so the
  blocked sources could have changed the odds but not the decision.
* **"Hummer EV is Global B" was not pinned to a primary GM citation.** It is
  well corroborated across secondary sources and is consistent with the
  openpilot support matrix, the model year and the platform, and nothing found
  contradicts it. It is still second-hand. The calibration IDs recovered from
  this VIN (`135240240857240850`) identify the build precisely; a primary GM
  document tied to those would settle it, and none was located.
* **Nothing here was measured on the vehicle.** This is a literature result. It
  predicts what a capture would show; it is not a capture.
* **A negative search result decays.** Global B decodes may be published later.
  The findings above are as of this document's date and should be re-checked
  before anyone concludes they are still true.

## The bounded experiment that would settle it

If this is ever run, it is run exactly once, in this shape, and it answers one
question: **does the DLC carry broadcast traffic on this vehicle at all?**

| Parameter | Value |
|---|---|
| Duration | 30 seconds, hard-bounded in code, not by the operator watching a clock |
| Vehicle state | Parked, in park, key on, stationary, attended by someone who can pull the adapter |
| Transmission | None onto CAN. `STCMM` set to `0` and its `OK` recorded before monitoring begins |
| Logging | Byte-exact, append-only, into `logs/raw/*.jsonl` like every other transcript |
| Before and after | `ATCS` (CAN error counters) recorded on both sides of the capture |
| Repetition | None until the transcript has been reviewed offline |

On `STCMM` verification: the manual documents `STCMM` as a setting command, and
no read-back for it was confirmed. So "verified at 0" in practice means set
explicitly to `0` and the adapter's `OK` captured in the transcript, rather
than interrogated. That is weaker than a read-back and should be recorded as
such.

On `ATCS`: it reports the controller's transmit and receive error counters. A
transmit error counter that moves off zero is positive evidence that the
adapter attempted to transmit, which would be a hard stop. A counter that stays
at zero is *consistent with* silence but does not prove it, because a
successfully transmitted ACK is not an error. The real guarantee is `STCMM 0`
and the vendor's documentation of it; `ATCS` is the cheap corroborating check,
and the only thing that would settle it conclusively is a second listener or a
scope on CAN_H and CAN_L, which is out of scope for this project.

One engineering property has to be written down before the capture, not
discovered in the data: **the capture is lossy by construction.** The adapter
prints frames as ASCII over an RFCOMM link the node opens at 115200 baud. A
29-bit frame with eight data bytes is roughly two dozen printed characters, so
the link ceiling is a few hundred frames per second, while a 500 kbit/s
powertrain bus can produce thousands. Frame counts from this capture are
therefore not a measurement of bus load, and no conclusion may be drawn from
them beyond presence or absence.

### What counts as a positive result

Frames appear, from multiple distinct arbitration identifiers, with payload
bytes that differ between frames of the same identifier. That is the necessary
precondition for everything downstream, and it is *all* it is.

A positive result authorises exactly one thing: keeping the transcript and
analysing it offline. It does not identify a signal, it does not authorise a
second capture, and it certainly does not authorise a decoder. Mapping an
arbitration identifier to state of charge is a fresh evidence problem with the
same standard as the Mode 22 identifier bar — and it is a harder one, because
broadcast frames are unlabelled. A byte that happens to move like a percentage
is the most likely way to be wrong without noticing.

### What counts as a negative result

Any of the following, each of which ends the line of investigation:

* nothing is printed for the full 30 seconds;
* only a small fixed set of identifiers appears with static payloads, which is
  what a gateway forwarding nothing but diagnostic plumbing looks like;
* the adapter reports an error, or refuses `STCMM`; or
* the adapter reports a bus error condition rather than frames.

A negative here is a genuinely useful outcome and should be published in
[docs/VALIDATION.md](VALIDATION.md) in masked, curated form, exactly like a
positive one. "Zero frames at the DLC on this VIN" is a measured fact about
this vehicle, and it is worth more than everything in the findings table above,
because it is first-hand.

## What would have to change to run it

All three of the following, in one reviewed change. Any one of them alone is
not a plan.

1. **The five-part change-control process in [SAFETY.md](SAFETY.md).** Written
   justification for why the command is read-only for this protocol and
   vehicle; an allowlist update that does not weaken `FORBIDDEN_SERVICES`;
   tests proving unsafe variants and command batching still transmit zero
   bytes; an offline acceptance path against the PTY/ELM simulator with an
   assertion on the exact transmitted command list; and a supervised first live
   use with byte-exact logging and transcript review.
2. **An allowlist entry for the specific monitor command, as an exact string.**
   `STCMM0` and one ST-family monitor command, named literally — never a
   pattern such as `^STM.*$`, which would re-open a command family the way a
   `^22[0-9A-F]{4}$` rule would re-open the identifier space. `ATMA` stays
   refused: it is deprecated by the vendor, and the capability report asserts
   its refusal.
3. **A separate, explicitly bounded capture path in the transport.** This is
   the part that is easy to miss, and it is not optional.

`SerialTransport.send()` implements request/response framing: it writes one
command and reads until the adapter's `>` prompt or a timeout, then returns the
whole buffer as one reply. A monitor command does not fit that shape. It
returns a *stream* — no prompt arrives until monitoring is stopped — so
`send()` would block for its full command timeout and then return a truncated
buffer flagged as a timeout, which is both wrong and quietly wrong. Reusing it
would also mean the entire capture sits in memory before a single byte reaches
the raw log, which breaks the rule that raw bytes are written before anything
parses them.

A capture path would instead need to stream to the raw log as bytes arrive, be
bounded by wall-clock **and** by byte count so neither a chatty bus nor a stuck
read can run away, and stop the way the vendor documents — by sending a
character to the adapter, which travels over the UART to the OBDLink and not
onto the CAN bus. It should be a separate operator-run entry point in the shape
of `voltage.py`, with its own fixed command tuple asserted at import time, and
it must not be reachable from the collector. The collector polls on a schedule;
a streaming capture that no scheduled process can start is a capture that
cannot accidentally become continuous.

## Rollback and stop conditions

**Stop immediately, mid-capture, if any of these occur.** Pull the adapter
first and ask questions afterwards:

* `ATCS` shows a transmit error counter that has moved off zero;
* the adapter reports a bus-off, bus error or wiring-fault condition;
* the vehicle's driver information centre shows any new message;
* any vehicle behaviour changes — lighting, HVAC, drive readiness, doors; or
* the capture exceeds its byte bound before its time bound, which means the
  assumptions behind the bound were wrong and the bound is what saved it.

**Reverting the software.** Revert the allowlist entry first, then the capture
path and its tests, then any configuration naming a monitor command. The
acceptance after revert is that the gate rejects `STM`, `STMA`, `STCMM0` and
`ATMA` alike, and that the existing safety tests pass unchanged. A partial
revert that leaves a monitor command allowlisted is not a rollback.

**Proving the vehicle is unaffected.** A code revert cannot un-send anything,
which is why the gate exists. The vehicle-side check is the same baseline
comparison used for Mode 22 in
[ENHANCED_PID_VALIDATION.md](ENHANCED_PID_VALIDATION.md): re-run services `03`,
`07` and `0A` and confirm zero stored, pending and permanent codes from the
same eight, eight and five responding modules; confirm `010D`, `011F` and
`0142` still return plausible values; confirm `ATRV` and control-module voltage
are consistent with the earlier probe in a comparable vehicle state; confirm
the driver information centre shows no new message; and archive the byte-exact
transcript with its SHA-256 so what happened stays reviewable regardless of
what the repository looks like afterwards.

If any check differs from the baseline, preserve the transcripts and write it
up as an incident. Do not send a follow-up request to investigate.

## Bottom line

Budget this as a low-probability experiment, not as a planned data source.

The evidence says a 2024+ GM Global B vehicle is treated as closed by the
people best placed to open it and gives no reason to expect usable broadcast
traffic at the OBD-II connector — the barrier itself is not pinned down, per
the mechanism caveat above. The one public precedent for decoding GM pack data
came from a tap somewhere else on a previous-generation platform, and no
citable Ultium identifier or DBC exists in any of the places such a thing would
live. Against that, the only confirmed positive is that if the experiment is
ever run, it can be run without transmitting — which is a precondition, not a
result.

Do not build a decoder pipeline on the hope. Do not add a monitor command to
the allowlist speculatively, "so it is ready". Do not let a plausible-looking
byte in a capture become a displayed number. The 30-second capture is worth
running once, some day when the truck is parked and someone is standing next to
it with half an hour to spare, because a measured "zero frames on this VIN"
would be worth more than this entire document. Until then, the honest position
is the one this project already holds: the high-voltage battery is not visible
from the OBD-II port on this vehicle, and nothing found in this research
changes that.
