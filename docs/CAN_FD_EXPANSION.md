# CAN FD: what buying hardware would and would not get us

Status: **decision-prep. Nothing bought, nothing chosen, nothing approved.**
This page exists so that if the question ever comes up under time pressure —
a fast-charge session to catch, a fault to chase, a weekend free — the
comparison is already done and the safety rule is already written down.

Everything the collector reads today comes through an OBDLink MX+, which
implements **Classical CAN only**. That is the ceiling recorded in
[TELEMETRY_CATALOG.md](TELEMETRY_CATALOG.md) and
[GM_ENHANCED_CANDIDATES.md](GM_ENHANCED_CANDIDATES.md), and it is real. What is
*not* established is that the ceiling is the thing standing between this project
and more data. Read the next section before the hardware table.

---

## The rule, stated once

> **Never connect anything to a pair inside the vehicle until GM service
> information or measured physical-layer evidence identifies that specific bus
> and its bitrate.**

Not "until it looks like CAN". Not "until the connector pinout is posted on a
forum". Identified, from service information or from an oscilloscope on the
actual pair in this actual truck.

The reason is not caution for its own sake. A CAN transceiver connected to a
pair it has the wrong bitrate for does not politely fail: it transmits error
frames at every mismatched bit, and a node that keeps doing so takes the segment
with it. On a vehicle where propulsion, braking and steering share
infrastructure with everything else, "which bus did I just join" is not a
question to answer empirically by joining it. The same applies to a CAN FD
controller placed on a classical segment, or a classical controller on an FD
one — an FD frame looks like a protocol violation to a classical node, which
duly reports it as an error.

Two corollaries, both load-bearing:

* **A listen-only or receive-only mode does not make the wrong bitrate safe.**
  Receive-only stops acknowledgement bits; it does not stop bit-level error
  signalling in every controller, and it does not stop the physical act of
  loading the pair. It is a mitigation, not a licence.
* **The OBD-II connector is not "inside the vehicle" in this sense.** Pins 6 and
  14 are a diagnostic port designed to be connected to. Anything behind the
  gateway is not.

---

## The thing worth being clear about first

**We do not have evidence that CAN FD at the diagnostic connector would show us
anything new.** The findings this project has accumulated point the other way:

* The gateway is a boundary, not a bottleneck.
  [PASSIVE_CAN_VALIDATION.md](PASSIVE_CAN_VALIDATION.md) found no public
  evidence of usable broadcast traffic at the DLC on any 2024+ GM Global B
  vehicle, and the one public GM pack decode
  (`gm_global_a_high_voltage_management.dbc`) came from a tap **behind the
  forward camera on the previous-generation platform** — not from a diagnostic
  connector.
* `hummer-obd-passive` measured that directly on this truck rather than arguing
  about it, and **it returned zero bytes in thirty seconds** parked and awake.
  The interesting hypothesis after a silent DLC is not "it was speaking FD" — it
  is that the gateway forwards nothing unsolicited, which no adapter fixes.

So the honest framing of a purchase is: an FD interface buys the *ability* to
sit on an FD segment. It does not buy access to one. Access is the hard rule
above, and nothing in the table below relaxes it.

There is one thing an FD interface at the DLC would settle cheaply, and it is
worth naming because it is the only cheap positive: **whether the diagnostic
connector itself negotiates FD at all.** That is a fact about this vehicle that
nobody has written down, it is obtainable at the port we are already allowed to
touch, and a negative closes the question permanently.

---

## The four options

Specifications below are from vendor documentation as understood at the time of
writing and are **not verified against a purchase**. Confirm every line marked
*(verify)* directly with the vendor before ordering — this table is for shaping
a decision, not for placing an order.

| | comma.ai **red panda** | **PiCAN FD Duo** (SK Pang) | **Waveshare 2-CH CAN FD HAT**, isolated variant | **MDI2 + GDS2** (GM dealer tooling) |
|---|---|---|---|---|
| What it is | USB CAN FD interface built for vehicle work | Raspberry Pi HAT, dual CAN FD over SPI | Raspberry Pi HAT, dual CAN FD over SPI | GM's own diagnostic interface plus its software |
| Channels | 3 *(verify)* | 2 | 2 | vehicle-wide, via the DLC |
| Host fit | USB — works from the Pi, also from a laptop | native HAT on the existing Pi Zero 2 W *(verify SPI/pin fit)* | native HAT | Windows laptop; not a Pi accessory |
| Galvanic isolation | **not isolated** *(verify)* | depends on variant *(verify)* | **isolated** — the reason to prefer it | n/a |
| Software route | SocketCAN / comma's own tooling; large community | SocketCAN (`mcp251xfd`) | SocketCAN (`mcp251xfd`) | vendor software only |
| Rough cost class | high two / low three figures | low three figures | low-to-mid two figures | **subscription**, plus interface hardware |
| Gets us data the MX+ cannot | only if an FD segment is reachable | same | same | **yes — but see below** |
| Fits this repo's model | yes, read-only usage is a choice | yes | yes | **no** |

### Reading the table

**Isolation is the differentiator, not channel count.** A non-isolated interface
puts the Pi's ground and the vehicle's ground in common. On a bench that is
tidy; on a vehicle with a 400 V pack, a shared ground between a mains-powered
laptop and a vehicle bus is the failure mode that damages equipment on both
sides. If a HAT is bought, buy the isolated one. This is also why the red panda
being USB matters in its favour — it interposes a USB link rather than a Pi
header — and why *(verify)* against its own isolation claim is worth doing
properly.

**MDI2 + GDS2 is a different category and mostly a different project.** It is
the only option that would definitively read everything, because it is what the
dealer uses and it authenticates. It is also: subscription-priced per period,
Windows-only, and — decisively for this repository — **a bidirectional tool
whose entire value is that it can command the vehicle.** Every line of
[SAFETY.md](SAFETY.md) exists to make commanding structurally impossible here.
Introducing a tool that does it by design does not extend this project; it
replaces its safety model with "be careful". If GDS2 is ever wanted, it belongs
on a separate machine, in a separate session, with a human driving it — not
wired into a collector that runs unattended for hours.

---

## What would have to be true before buying anything

In order. Each one is cheap and each one can end the question.

1. ~~**Run `hummer-obd-passive` at the DLC, awake and driving.**~~ **Done, and
   it came back zero.** Thirty seconds parked and awake on 2026-09-04 returned
   no bytes at all
   ([VALIDATION.md](VALIDATION.md#passive-can-capture-at-the-diagnostic-connector-2026-09-04)).
   That is the strong result, and it points *away* from every interface in the
   table: the gateway forwards nothing unsolicited, and no adapter changes what
   a gateway chooses to forward. Driving and charging remain untested, so this
   is not yet the whole answer — but it is the answer for the state that was
   easiest to imagine being chatty.
2. **Establish whether the DLC negotiates CAN FD at all.** This is the only
   question in this document that hardware answers cheaply and safely, at a
   connector we are already permitted to use.
3. **Identify a target segment from service information, not from a pinout
   post.** Which pair, in which connector, at which arbitration and data
   bitrates. Without this, the rule above forbids the connection, and no
   purchase changes that.
4. **Decide what would be done with the data.** The pack signals this project
   most wants — current, per-cell detail beyond min/mean/max, module-level
   temperature — would still need decoding, and no Ultium DBC exists publicly.
   An FD tap that yields undecodable traffic is a more expensive version of the
   position we are already in.

If steps 1–3 all come back favourable, the recommendation is the **isolated
Waveshare HAT** for a fixed in-vehicle installation on the existing Pi, or the
**red panda** if the work is exploratory and wants a laptop and a community's
tooling behind it. That recommendation is provisional and depends entirely on
evidence that does not exist yet.

---

## What this page deliberately does not do

It does not recommend a purchase, authorise a connection to any internal pair,
or treat CAN FD as the project's next step. The next step is
`hummer-obd-passive` and it costs nothing.

Its purpose is narrower: to make sure that if the decision is ever made, it is
made against this comparison and against the rule at the top, rather than at
2 a.m. with a truck plugged in and a charge session ending.
