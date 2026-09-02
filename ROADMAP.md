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

## Done

Each of these is proven on the reference vehicle unless the entry says
otherwise. `docs/CAPABILITIES.md` holds the per-capability evidence, and
`CHANGELOG.md` holds the sequence.

| Milestone | State |
|---|---|
| **The read-only node** — Bluetooth SPP/RFCOMM to an OBDLink MX+, byte-exact JSONL transcripts, per-ECU ISO-TP reassembly for 29-bit CAN, WAL-mode SQLite, e-paper status | Proven, running on the node |
| **The safety gate** — an allowlist every serial byte passes, plus `FORBIDDEN_SERVICES` as an independent second barrier; modes 04, 08 and the UDS write/control set can never be transmitted | Proven and tested; `safety.py` is not modified without the five-part process in `docs/SAFETY.md` |
| **Per-ECU capture** — one request, up to eight answers, each attributed to the module that gave it, with `samples.ecu` recording which | Proven: eight modules answered `0142` across a 0.41 V spread |
| **Schema v3** — additive-only migration from v1 or v2, verified against a copy of the real database before the original was touched | Proven on the live node, backup taken first; `cycle_id` lands NULL, never a fabricated 0 |
| **Services 02 and 06** — standard J1979 reads admitted through change control, needing no guessed identifier | Proven. `0600` advertises zero monitor IDs; `020000` advertises `02 0D 1F 20` |
| **The capabilities report** — sanitized, offline, opens no serial device and SQLite read-only, safe to run mid-observation | Proven against the live database |
| **The export** — deterministic JSONL/CSV/JSON a notebook, a spreadsheet or a language model can ingest without this repository | Proven against the live database |
| **Bounded collector trials** — `--max-cycles` / `--duration-s`, sliced waits, a supervising systemd unit with no `[Install]` section and a rootless equivalent | Proven on a moving vehicle: 0 to 94 km/h, 539 valid samples, stopped itself cleanly after 422 cycles |
| **The 12 V watch** — `ATRV` sampled with a guarantee narrower than the rest of the project's: not "read-only" but "nothing reaches the vehicle at all" | Proven; caught the sleep transition at 13.9 V to 12.7 V |
| **The PiSugar2 cell watch** — chip identified by measurement, built around its refusals, no I2C write anywhere in the module | Monitoring proven; the identifying read was 4.05 V on the IP5209 pair (`docs/RUNBOOK.md`), and the low-cell action is armed as `stop-collector` |
| **Package install on the node** — the seven `hummer-obd-*` console scripts now exist, so the runbook's commands run as written | Fixed and verified |

## In progress

- **Trending 12 V across a full sleep period, unplugged.** The watch is
  written, armed, and has already produced one result — five minutes after
  parking, 13.9 V to 12.7 V, with zero CAN traffic generated to observe it.
  What is missing is duration and conditions, not tooling. See *Blocked*
  below for why the runs so far do not count.

- **Solving the node's return, so unattended operation stops depending on a
  shutdown.** The research is done and the conclusion is that a better
  shutdown is the wrong target: a PiSugar2 cannot power the Pi back on, and a
  halted Pi in a truck needs somebody to walk out to it. The shape of the
  answer is vehicle power, a read-only root, and the pack as a UPS that never
  cuts, with WAL and fsync bounding a dirty cut to seconds of data. None of
  that is built.

- **Repository hygiene.** This file, `CHANGELOG.md`, `LICENSE` and
  `.github/workflows/quality.yml` are the first pass. Both lint steps are
  advisory today, for two different reasons. `ruff check` passes as of the
  commit that dropped three unused imports, so making *it* blocking is now a
  one-line change waiting on a decision rather than on work. `ruff format
  --check` still reports 34 of 36 files, so making *that* blocking is a
  follow-up that starts with a formatting commit of its own — not with
  loosening the rules, and not mixed into an unrelated change.

- **One decoder path is unguarded.** `supported_freeze_frame_pids()` has no
  direct test: setting its `skip` back to 0 — the exact one-byte regression
  that was already fixed once — passes the entire suite. Everything else that
  has been mutated in review died against a test. Named here rather than left
  in a review comment, because an untested decoder is how a plausible wrong
  number gets published.

## Blocked on physical conditions

These are not backlog items. Each one names what would release it.

### Continuous collector autostart

`collector.enabled` stays `false` and `hummer-collector.service` stays
disabled. Two separate things are unproven, and either one alone keeps the gate
shut:

1. **Overnight 12 V stability with the hardware attached.** Every attempt so
   far was made with the truck plugged in, which holds the rail up and
   therefore measures nothing. The measurement has to be taken unplugged,
   across a full sleep period.
2. **Whether the vehicle still sleeps while the collector is *actively
   polling*.** What has been observed is sleep with the hardware attached and
   polling stopped. That is the correct control, and it is not the condition
   the gate is about. A 2-second polling loop keeping the modules awake is
   exactly the failure this gate exists to prevent, and it has not been ruled
   out.

**Releases it:** both measurements, taken unplugged, in that order — the
voltage trend first, then a bounded polling trial with the sleep behaviour
observed. Not one, not either.

### Freeze frame contents

Service 02's request path is proven and the support bitmap has answered, so
what a frame on this truck *would* hold is known: `02 0D 1F 20` — the DTC that
set it, speed, run time, and the 21-40 bank pointer. What has never been done
is read an actual frame, because a frame exists only once a DTC is stored and
this vehicle has none.

**Releases it:** the truck developing a fault of its own. Inducing one to
exercise a decoder is not acceptable and is not a plan; this item may stay
blocked permanently, which is the correct outcome for a healthy vehicle.

### Mode 22 enhanced PIDs

Rejected by the gate, and the reason is evidentiary rather than technical. No
citable identifier for this platform exists publicly with both an ECU address
and a scaling equation attached — and an identifier without those two things is
not an identifier, it is a guess with a hex number in front of it. Sweeping
candidates to see what answers is forbidden: a response that decodes to a
plausible number is the most likely way to be wrong without noticing.

**Releases it:** a sensor or PID export produced from *this VIN* — a Car
Scanner or OBDLink profile export — followed by the full evidence bar in
`docs/ENHANCED_PID_VALIDATION.md`. That export is the strongest available
source because it describes this truck rather than a nominally similar one, and
it is still only one source.

### Passive CAN monitoring

`ATMA` and the `STM` family are rejected by the gate today, and the research
says this is very likely a dead end rather than a fallback: a 2024+ GM Global B
vehicle does not appear to hand out broadcast traffic at the OBD-II connector,
and no Ultium DBC or identifier exists in any of the places such a thing would
live. The full negative result, its limits, and the one bounded experiment
worth running are in `docs/PASSIVE_CAN_VALIDATION.md`.

**Releases it:** a single 30-second capture on a parked truck with someone
standing next to it, which would replace a researched guess with a measured
"zero frames on this VIN". Budget it as a low-probability experiment, not as a
planned data source, and note that the capture itself needs the five-part
change-control process first.

### GPS and location

Not blocked on software: location is not an OBD-II service and this vehicle
does not expose it on the bus, so no amount of decoding produces a position.

**Releases it:** external hardware — a receiver on the Pi — which is a
different project with its own privacy consequences, since a position log is
far more sensitive than a voltage log.

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
