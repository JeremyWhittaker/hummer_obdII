# Roadmap

What is finished, what is moving, what is waiting on the physical world, and
what will not be built here.

This file exists because the interesting constraints on this project are not
software constraints. Most of what is left undone is blocked on a measurement
nobody has taken yet, or on a fault a healthy truck has not developed. Saying
which is which is the point: "not implemented" and "cannot be proven" are very
different states, and a roadmap that blurs them invites someone to build the
wrong thing.

Nothing moves out of *Blocked* because it has been waited on long enough. Each
blocked item below names the evidence that would release it.

## Where this stands, 2026-09-04

**Start with [the access matrix](docs/ACCESS_MATRIX.md).** It is generated from
the code and says what can and cannot be reached, with a command that checks each
claim. This file says what is *planned*; that one says what is *true*.

Two very different lists follow. The first is the plan this project set itself
and finished. The second is a larger external proposal, most of which is not
done, and the reasons differ item by item — which is the part worth reading.

## Done — the six-goal plan, complete

| Goal | Outcome |
|---|---|
| **G1** Reproducible correlation tooling | `hummer-obd-decode`. The project can re-derive its own findings; the first thing it did was **disprove** one of them (a published −0.81 correlation that was really −0.09 on a corrected corpus) |
| **G1b** Charge-aware analysis | `analyze.py` detects a charge and reports energy added, mean and peak power by two independent routes, SoC gained, cell-spread drift |
| **G1c** Cross-session trends | `--trend`: cell spread, implied capacity, efficiency and series-cell count across every committed session |
| **G2** Collect what the census proved supported | All nine service-01 PIDs module `17` advertises, including PID `01` — the malfunction lamp and stored-fault count, added last |
| **G3** Passive CAN capture | `hummer-obd-passive`, behind two gates of its own. Run on the vehicle: **zero bytes in 30.1 s** |
| **G4** Source drive-motor identifiers | Two sweeps found **nothing** — no public source names motor RPM, torque, inverter temperature or power limits for any GM Ultium vehicle. Written up in `docs/SOURCING_2026-09-04.md` |
| **G5** Confidence registry | `hummer_obd.confidence`: all 35 identifiers graded 0–4, key-parity with the gate asserted, level-3 claims recomputed from the corpus by tests |
| **G6** Three documentation defects | All fixed, plus `docs/CAN_FD_EXPANSION.md` created |

Unplanned work the same day, each caught by measurement rather than review:
the wake threshold was wrong twice; `0x2429` was decoded as a voltage and is
not one; fault codes had never actually been read (`NO DATA` was being recorded
as "no codes"); a "5.4 °F corpus span" figure had quadrupled unnoticed; and an
adversarial pass found five stale claims in prose.

## The expansion proposal, item by item

An external research report proposed a much larger programme. Its central
judgement — *keep pushing read-only OBD, do not pivot away from the OBDLink* —
is right and is what this project is doing. Its status here:

| Item | State | Why |
|---|---|---|
| **A** Passive differential capture | **Tool built, captures need a driver** | `hummer-obd-passive-diff` compares two transcripts offline. The event captures — fob lock, door, HVAC, charge start — need someone at the vehicle to *cause* the event, so they cannot be run remotely. The baseline is done and returned **zero bytes**, which weakens the prior considerably: see below |
| **B** Read-only frontier: gateway, second BSM, BCM | **Done** | `gwm-45-p18` and `bsm-cd-p18` were both run first-hand on 2026-09-04. Every identifier returned `7F 22 31` from the module's own address — present, speaking service 22, holding none of them. Module `40`'s nine identifiers already answer and are captured every cycle |
| **C** Controlled physical correlation | **Built; needs a human at the vehicle** | `hummer-obd-experiment` records what a person observed beside each session — ambient temperature, dashboard SoC, what the charger's own display said. Its load-bearing field is `label_source`: a label *derived from the session CSV* does not break the circle of correlating the truck's numbers against its other numbers, and the schema refuses to let an inferred label carry an outside measurement. 23 sidecars exist and **every one is marked inferred**, because nobody was standing at the vehicle with a thermometer |
| **D** Charge-session study | **Half done** | `analyze.py` already produces a charge report rather than a meaningless distance-and-efficiency one, and the recorder captures everything relevant every cycle. What is missing is a *labelled* charge with independent truth values written down beside it. Needs a charge event |
| **E** Dedicated GNSS | **Not started — needs hardware** | No GPS receiver on the node. Nothing in software gets around that |
| **F** Uniden R8 BLE collector | **Not started — needs hardware access** | The e-paper display already *consumes* R8 state from a separate collector (`docs/R8_DISPLAY.md`); the collector itself does not exist here and needs the detector paired |
| **G** OnStar command broker | **Deliberately not here** | Jeremy's explicit scope decision: this repository stays read-only. A cloud broker is a different trust domain — stored credentials, bidirectional by design — and belongs in its own repository, not wired into an unattended node |
| **H** GM Service Information checklist | **Written** | `docs/GM_SERVICE_INFORMATION.md` — what to retrieve privately against the VIN before any internal-bus work is even evaluated |
| **I** MDI2 / GDS2 workflow | **Written** | `docs/OEM_DIAGNOSTIC_WORKFLOW.md` — GM's own tool as a truth oracle for the raw fields, human-operated, on a separate machine |
| **J** Safety boundary unchanged | **Enforced and machine-checked** | Session changes, security access, writes, actuator control, routines, resets, replay and identifier sweeping are all refused by every gate, and `docs/ACCESS_MATRIX.md` shows each one refused rather than asserting it |

### What the zero-byte capture does to item A

The report was written before that capture ran, and it changes the odds. Thirty
seconds of receive-only monitoring, parked and awake, returned **nothing at
all** — not sparse traffic, zero bytes. The gateway forwards nothing unsolicited
to pins 6 and 14 in that state.

That does not close item A, because the differential idea specifically targets
*event-triggered* traffic and only one state was tested. But it does reorder the
work: **run two or three more captures during actual events before investing
anything further in analysis.** If a fob lock and a charge start also produce
zero bytes, the passive path is finished and the diff tool will have cost an
afternoon rather than a fortnight. That is why the tool treats an empty
comparison as a first-class result and says so in its own output.

## Blocked on physical conditions

Each names the evidence that would release it. Nothing moves out of this section
for having been waited on.

| Waiting on | Would release |
|---|---|
| **A charge session, labelled** | The EVSE fields (`0x4149`) and the group-voltage fields (`0x416C/D/E`), all of which were read while parked and unplugged — the state that says least about them |
| **A cold morning** | The thermal fields. The corpus spans 23.4 °F but the module-40 thermal identifiers cover only 9.0 °F of it, and the best correlation any of their byte windows reaches against `temp_f` is +0.69 — a direction, not a scaling |
| **A DC fast charge** | Charge taper behaviour, and whether `0x5401` means anything at rates an AC charge cannot reach |
| **A fault occurring** | Freeze-frame contents. Service 02 works and there is nothing to read: verified positively on 2026-09-04, `43 00` / `47 00` / `4A 00`, count zero from module `45` |
| **Someone at the vehicle** | The event captures for item A |
| **A GM Service Information subscription** | Any evaluation of an internal-bus tap. See `docs/GM_SERVICE_INFORMATION.md` |
| **Continuous collector autostart** | Whether the vehicle still sleeps while a collector polls. Every sleep observed so far had polling stopped, which is the correct control and not the condition being gated |

## Deliberately out of scope

Not "later". Not here.

- **OnStar and GM cloud commands.** A different system entirely, reached with
  account credentials rather than a diagnostic connector. If it is ever built,
  it belongs in a separate service with isolated credentials and its own
  command allowlist. Putting an authenticated cloud client in a repository
  whose entire claim is that it cannot write to the vehicle would destroy that
  claim, whatever the code actually did.

- **Radar telemetry.** Same reasoning and the same answer: a separate service
  with isolated credentials. It shares nothing with this node except the owner.

- **Remote commands** — lock, unlock, preconditioning, remote start. This node
  has no vehicle write authority of any kind, and that is a design property
  rather than a missing feature.

- **DTC clearing (mode 04), actuator and control tests (mode 08), and the UDS
  write/control/security/reset/routine set.** Permanently forbidden, not
  configuration options. There is no flag.

- **Raw transcript upload.** Rejected at config load. Raw frames spell out
  VINs, calibration IDs and ECU names in ASCII, so the transcript never leaves
  the Pi. Uploading at all is disabled today, with an empty endpoint and an
  uploader that refuses to start.
