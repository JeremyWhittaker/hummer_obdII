# Independent review of the September 18–21 changes

Reviewed on 2026-09-22. Scope: **`2a2f322..063f91d`**, 13 commits, 13 changed
files. This is the work since Codex's last verified checkpoint, not a claim
that every older part of the repository is defect-free. Three independent
reviewers covered scanner, watchdog, and dashboard; the parent checked their
findings against callers and reproduced the principal failures offline.

## Bottom line

There is substantial, useful progress: adapter startup synchronization, a
guarded candidate-watch tool, persisted Bluetooth recovery, an initial
six-module discovery campaign, and better live/replay presentation. The
separate command gates remain intact. **This batch does not add any newly
validated scanned DID to the recorder.** `safety.py`, `drive.py`, `enhanced.py`,
`confidence.py`, and the telemetry catalogue are unchanged from the checkpoint.

The next milestone should be **reliable operation and validated new signals**,
not another large sweep or more model decoration. Correct the operational and
freshness findings below, make scan evidence analyzable, then validate the
door/HVAC leads and investigate battery arrays.

**Review verdict: changes requested.** The independent review scorer retained
eight actionable finding groups; the raw-byte-loss contract violation blocks
an unconditional scanner acceptance. This does not mean a vehicle write or
security-access path was found: those prohibitions remain intact. The review
is complete; the reviewed code is not thereby approved for expanded operation.

No production source was changed for this review. No SSH, Pi service control,
adapter access, vehicle request, deployment, Git commit, or push was performed.
The local artifacts are a review and plan, not permission to execute the plan.

## What was reviewed

| Area | Commits | Assessment |
|---|---|---|
| Scanner startup, watch, diagnostics | `ed501c6`, `5d6e503`, `a910c33`, `1636f7a` | Useful, bounded additions; one byte-preservation race remains. |
| Bluetooth recovery | `79e99fe`, `6143fd7`, `f24a13a`, `249c406`, `e91e651` | Fixes the one-shot timer's lost strikes; restoration, concurrency and failure handling need more work. |
| Scan results and sourcing | `6cbd61d`, `7b3140c`, `1a368a2` | Valuable candidate inventory, but coverage, operating instructions and some analytical conclusions overstate the evidence. |
| Dashboard/live telemetry | `063f91d` | Strong missing/neutral/value distinctions and replay tests; full-page lifecycle still permits stale readings. |

The four changed test files were reviewed too. Existing tests are valuable,
but passing isolated helpers does not prove service interleavings or the full
browser render lifecycle.

## Findings requiring follow-up

P1 means before further expanded scanning or a claim of dependable live
telemetry; P2 means the next reliability/analysis increment. All fixes are
**deferred** because this request authorizes review and planning, not implementation.

### R1 — P1: watchdog restoration can undo an intentional scan handover

Evidence: `src/hummer_obd/btwatch.py:248` snapshots whether the recorder was
active; line 270 starts it after potentially slow repair commands.
`docs/DEEP_SCAN.md:238` leaves the watchdog running during a scan.
`src/hummer_obd/scan.py:115` explicitly notes that its tty flock does not
exclude the recorder, which does not take that lock.

A watchdog pass can observe “active,” then the operator stops the recorder and
starts a scanner, and the in-flight watchdog subsequently starts the recorder
again. The mock reproduction confirms a recorder start after the simulated
operator stop. Reset/rebind commands can also disrupt the diagnostic port.
Starting rather than restarting the service does not solve ownership of an
inactive service whose desired state changed during the repair.

Required change: an operation/maintenance lease shared by diagnostic tools,
recorder control and watchdog, acquired before handover and respected for the
whole operation. Test the actual interleaving, not just a recorder already
inactive when a watchdog pass begins. Restore only the state owned by that
lease, including operator cancellation.

### R2 — P1: scanner synchronization still has an unlogged-byte loss window

Evidence: `src/hummer_obd/scan.py:123` checks pending input, then delegates to
`SerialTransport.send()`, whose `src/hummer_obd/transport.py:144` clears input
unconditionally. The guard path has the same sequence at `scan.py:133`.

A delayed banner arriving after `in_waiting` reports zero but before that
clear is discarded without a raw RX record or an abort. The offline fake
serial reproduction dropped a 14-byte synthetic banner, accepted the subsequent
`OK`, and logged only `OK`. The new before-send check is useful but does not
meet the claimed byte-exact guarantee at this boundary.

Required change: a scanner send path that never silently clears input after
its explicit check. Late bytes must be preserved and subjected to strict reply
validation. Add check/clear-boundary tests for ordinary scanner and guard sends;
do not weaken the accepted reply shapes or shared default command gate.

### R3 — P1: newly drawn channels can bypass freshness in the latest view

Evidence: `dashboard.html:5398` calls `showFrame()` to position the normal map
marker; line 4944 sets `state.frame`. `renderVehicle()` at line 4285 treats any
frame as replay. The newly added cell readings at line 4474, wheel deviations
at line 4401 and torque at line 4516 then bypass the live signal freshness
rules. The unchanged-map fast path at line 5142 retains this frame.

Chromium reproduced this at both 1440×1000 and 390×844, with synthetic GPS
history, `Latest session` still selected, and no user scrubbing: the header
said **Vehicle asleep**, while captions said **Cells right now** and
**Right now: regenerating** using old readings. The API's current derived
values were correctly withheld. Explicit historical replay worked when all
required inputs were present.

The frame-lifecycle flaw predates this batch; routing the new channels through
it extends the defect into the new feature. Required change: explicit
live-versus-replay state, not truthiness of `state.frame`. Test fresh → stale,
GPS loss, an unchanged track, session changes, and recovery through the full
DOM render/poll cycle. Historical samples should remain viewable as historical.

### R4 — P2: reload cooldown also disables cheap recovery

Evidence: `src/hummer_obd/btwatch.py:233` returns before every action during
strikes 10–38 while a stack fault is still indicated. Reproduced at strike 10:
no reconnect, rebind or restoration attempt. This is roughly 29 timer
intervals without watchdog remediation if the preceding reload only partly
succeeded. It is not proof every such incident causes a 29-minute outage;
other components may recover independently.

Required change: rate-limit the expensive driver reload, not unrelated bounded
recovery and restoration. Test partial failure of each rung and subsequent
timer invocations, not only the eventual number of reloads.

### R5 — P2: recorder restoration intent is lost after a failed start

Evidence: `btwatch.py:185` converts failed/unknown recorder status into “not
wanted”; line 270 ignores the result of the restoration start. A repair which
stops a previously active recorder, then fails to start it, leaves the next
timer invocation seeing `inactive` and treating that as intentional.

The two-pass mock made one failed start on the first repair and no start on
the second. This is an incomplete restoration fix, not a claim that the old
version restored the recorder correctly.

Required change: persist a bounded, cancellable restoration obligation across
timer processes until health is verified, distinguish unknown from explicitly
stopped, and integrate it with R1 so recovery never overrides a later manual
stop or scan lease.

### R6 — P2: unknown observations undermine the new escalation evidence rules

Two related gaps need explicit state semantics:

- `btwatch.py:227` uses `controller is not True`. Both devices can be known
  disconnected while a separate controller inspection fails; `None` then
  authorizes daemon restart/reload. Reproduced with `controller=None` and
  `wedged=False`. Distinguish positive evidence of an absent/down controller
  from an inspection failure rather than treating both identically.
- `btwatch.py:210` keeps prior strikes on unknown device state, and line 374
  persists them with a fresh timestamp. Unknown samples can preserve old
  strikes indefinitely, contrary to the “consecutive”/five-minute expiry
  explanation. Reproduced: two strikes, ten unknown samples, then a reset on
  the next known-down sample. The in-memory retention predates this change;
  refreshing it across one-shot timer processes makes the new persistence
  contract incomplete.

### R7 — P1: the new runbook recommends an unauthorized unattended scan

`docs/SCAN_RESULTS_2026-09-20.md:114` recommends “parked, unattended.” This
contradicts `DEEP_SCAN.md:153` and the owner's bounded, supervised scan intent.
The scanner's guards are not a replacement for supervision, and a model
supervisor monitoring a job is not automatically an authorization for an
unattended vehicle experiment. Correct the recommendation before it becomes
an agent's operating instruction. This review does not infer that earlier
scans were unattended; their execution was not audited here.

### R8 — P2: research recommendations exceed what the evidence can establish

- **Coverage:** `DEEP_SCAN.md:405` says every section-6 range completed, but
  `SCAN_RESULTS_2026-09-20.md:31`, line 71 and line 104 list a much smaller
  subset. Drive-controller `4xxx`, wider battery ranges and almost all full
  identifier spaces remain. Call this an initial targeted pass, not completion
  of the original search plan.
- **Missing time series:** `SCAN_RESULTS_2026-09-20.md:99` proposes correlating
  new chassis hits with existing drive wheel speeds without new traffic.
  That works only for candidates already captured at overlapping times with
  useful variation. An old wheel-speed series cannot supply a missing
  candidate series. `DEEP_SCAN.md:435` correctly acknowledges the missing
  scan-JSONL ingestion/alignment path; `analyze.py:70` and `decode_fields.py:270`
  substantiate that limitation.
- **Overstrong negative conclusion:** `SCAN_RESULTS_2026-09-20.md:43` rules out
  a per-cell array in the scanned ranges after trying a finite set of widths,
  scales and offsets. The supported conclusion is “not identified by those
  hypotheses in that capture.” Packed fields, headers, partitions, different
  operating states and other encodings remain untested. The dashboard repeats
  a stronger “NOT available on this truck” claim at `dashboard.html:4734`.
- **Overbroad motion prerequisite:** `SCAN_RESULTS_2026-09-20.md:82` groups
  inverter/stator temperatures with RPM and says they are all zero stationary.
  Temperature candidates can be studied through natural parked thermal
  changes. Even the existing torque evidence documents nonzero readings at
  zero reported speed (`confidence.py:612`). Do not infer that all thermal
  research must wait for a moving experiment, or deliberately provoke torque
  while parked to bypass the existing rules.
- **Premature promotion language:** lines 65 and 118 promise that one repeat
  promotes four body signals. Repetition is necessary evidence, not a
  guaranteed result. Door, courtesy-light, wake-state and load confounders,
  payload bit meaning, freshness and false positives still need testing.

## Important older limitations and test gaps

These are not attributed as regressions to the reviewed commits:

- Missing charging-state data crashes replay at `dashboard.html:4354`
  (`cordHere.plugged` when `cordset()` returned null). Reproduced in both
  browser viewports; blame identifies `81f2568`, before this review baseline.
  The resulting captions stay at the previous sample.
- The live `array_2af1` fallback at `dashboard.html:4327` reads a retained
  signal value without checking its stale status. The old code did this too.
- `rawFraction()` at `dashboard.html:782` sends raw strings through numeric-only
  `sigValue()`, then requires a string; the thermal-tint path returns null.
  The same code exists in the baseline.
- Live torque parsing accepts arbitrary-width hex while replay demands two
  bytes. Synthetic `580600` becomes an enormous live count but no replay
  reading. The reviewed corpus has only two-byte samples; treat this as a
  robustness/parity test gap, not evidence of corrupted vehicle data.
- There is no locally versioned watchdog service/timer beside the recorder
  and RFCOMM units. The installed sandbox/timer claims need a later read-only
  deployed-config audit and reproducible unit provisioning.

## Performance and usability

The module-array delta encoding correctly distinguishes absent, unchanged and
explicit null. However, it saves bytes compared with repeating the new array,
not compared with the old API, which did not send it.

Using the actual old and new `_history()` on the same locally available
1,168-row session, the 600-row compact-JSON window grew from **278,238 to
335,943 bytes (+20.7%)**. The HTML grew from **535,413 to 600,989 bytes**.
This is measurable extra work on the recurring poll; it is not a Pi latency
benchmark and does not by itself prove a timeout. Add payload, serialization
and end-to-end loading budgets, and separate current-state polling from stable
replay history.

The mobile layout had no horizontal overflow, and historical selection,
scrubbing with complete inputs, and section controls worked. The explanation
paragraphs dominate the mobile page; keep a short reading/status/units summary
visible and put derivation/provenance in expandable details. Colour ramps are
observed distributions, not diagnostic fault thresholds. Physical module
positions, direct measured motor RPM and torque units must not be implied by
the drawing.

## Verification and limits

- Full suite: **1,500 passed, 1 skipped, 3,864 subtests passed**, 106.82 s.
- Changed Python/test-file Ruff checks, generated access-matrix check and
  baseline-to-HEAD whitespace check passed.
- Offline service and serial mocks reproduced R1, R2, R4, R5 and R6; no real
  service command or serial device was used by those reproductions.
- Full Chromium page checks at desktop/mobile sizes confirmed R3 and the old
  missing-charging-state crash. Complete-input replay updated cell, wheel and
  effort captions correctly with no JavaScript exceptions.
- Screenshots were inspected, but software-WebGL capture was not consistently
  clean, and external map tiles were intentionally unavailable. This is
  **not** deployed-device visual, mobile-GPU or performance acceptance.
- Local prior session CSVs were available. The September 18–20 scan/watch raw
  transcripts were **not** available in this checkout. The published 7,424 /
  912 / 33 counts and all-zero guard claims are therefore reported findings,
  not independently re-audited live evidence. Current Pi code, services,
  connectivity and readiness are **NOT OPERATIONALLY VERIFIED** by this review.
- Source spot-check: the cited [LYRIQ PR #13](https://github.com/OBDb/Cadillac-LYRIQ/pull/13)
  and [PR #14](https://github.com/OBDb/Cadillac-LYRIQ/pull/14) exist, are open,
  and name the relevant charging/battery family. They are candidate provenance,
  not Hummer validation; #14's nominal-voltage claim for `2429` is exactly why
  platform labels must not override this truck's measured evidence. This was
  not a new exhaustive sourcing campaign.

Private reproducibility artifacts are in `.foreman/review-repros.py`,
`.foreman/review-ui-smoke.py`, and the review worker/checklist records. Only
synthetic fixtures were added. Existing dirty `.gitignore`, `allrepos.txt` and
`cm.json` were left untouched. No raw frames or vehicle identifiers were
included in this report.

See [the forward plan](ROADMAP_2026-09-22.md) for sequencing, acceptance gates
and an explicit disposition of the review items.
