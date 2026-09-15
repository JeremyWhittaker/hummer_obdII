# Going deeper: an authorized, supervised identifier scan

**Status as of 2026-09-15: the project's direction has changed.** Until now it
read only identifiers a public source named exactly, and it refused to scan the
service-22 identifier space on principle. The vehicle's owner has now
**explicitly authorized a bounded, supervised scan of service 22
(ReadDataByIdentifier) on their own truck**, because the sourcing routes are
exhausted (see [SOURCING_2026-09-04.md](SOURCING_2026-09-04.md)) and a scan is
the only remaining way to find the identifiers this project still wants — motor
speed and torque, inverter and stator temperatures, per-module pack detail,
door/lock/HVAC state.

This document is the single source of truth for that work. Where an older note
says "there is no DID sweeping in this project and there will not be", read it
together with this file: that stance held until 2026-09-15 and is the reason the
safeguards below exist, not a prohibition the owner still stands behind.

Read [SAFETY.md](SAFETY.md) first. **Nothing here relaxes a single
write/control/security prohibition.** The scan is read-only service 22 and
nothing else. What changed is only that "which identifiers may service 22 ask
for" moves from *an exact allowlist* to *a full range*, under supervision, on a
tool no unattended path can reach.

---

## 1. What the owner authorized, precisely

On 2026-09-15 the owner said, in their own words, that they are "totally fine
querying modules if we know the command we are going to run is a legitimate
command", and — after confirming that a DID scan is only UDS service `0x22`
ReadDataByIdentifier with a varying two-byte identifier — told this project to
"build the entire thing and start figuring this out."

So the authorization is specific and it is bounded:

* **Authorized:** sending `22 XX XX` (ReadDataByIdentifier) for identifiers
  across a range, to the vehicle's own named modules, **read-only**, while the
  truck is **parked**, on a **supervised** tool that a person starts
  deliberately.
* **Still forbidden, exactly as before:** every write, control, actuation,
  reset, security-access, communication-control, routine and session-control
  service (`04 08 10 11 14 27 28 2E 2F 31 34–38 3B 3D 3E 83 84 85 87`). The
  scan gate must refuse all of them, and `FORBIDDEN_SERVICES` stays the
  independent second barrier.
* **Still not authorized:** UDS `0x10` DiagnosticSessionControl. Some
  identifiers only answer inside an extended or programming session; reaching
  those means sending `0x10`, which changes module state and is out of scope.
  A scan stays in the default session. If an identifier answers `7F 22 7E/7F`
  (subFunction/service not supported *in active session*) or `33/34`
  (security/authentication), that is the finding — record it and move on. Do
  **not** try to open a session to get at it.

Everything below is the safe way to carry out exactly that, and no more.

## 2. Why this is legitimate, not "random codes"

Service `22` is defined by ISO 14229-1 as a pure read. It has no sub-function
that writes: the write counterpart is service `2E`, which is and stays in
`FORBIDDEN_SERVICES`. Every request the scan sends is the same command this
project already uses to read state of charge (`22 27 C6`) and pack voltage
(`22 28 85`); only the two identifier bytes change. A module asked for an
identifier it does not implement answers `7F 22 31` (requestOutOfRange) and does
nothing else. Community identifier databases (OBDb) and open tools (Caring
Caribou's `dump_dids`) are built by doing exactly this.

The old objection was never that the *command* was unsafe — it was that
industrialising it (65,536 unreviewed requests, a negative-response storm, the
shape of an intrusion to a logging gateway, and the risk of provoking session
state) was not something to do to a vehicle without the owner's say-so. The
owner has now given it, for their own truck. The safeguards in section 5 are
what remain of that objection: they keep the scan slow, parked, watched, and
reversible.

## 3. What we can already read (the starting point)

The recorder logs 64 columns (`drive.COLUMNS`) from six modules. The proven,
decoded signals and the 17 still-raw fields are catalogued in
[TELEMETRY_CATALOG.md](TELEMETRY_CATALOG.md) and graded in
`confidence.CONFIDENCE`. The scan is aimed at the gaps that catalogue lists
under "Not obtained": motor RPM/torque/phase current, inverter and stator
temperature, per-module pack temperature and state of health, contactor state,
suspension height, rear-steer/CrabWalk, and body signals (doors, locks,
lighting, HVAC). None of those has a public identifier; a scan is how their
identifiers get found.

## 4. The vehicle's modules and how service 22 is addressed

Eight modules the truck named for itself (`census.json`, service 09 PID 0A),
and the CAN priority each answers service 22 at — established by asking every
module at both priorities (see [CAN_PRIORITY.md](CAN_PRIORITY.md)):

| Module | Name | Answers 22 at | Notes |
|---|---|---|---|
| `17` | DMCM-DriveMotorCtrl | `0x14` and `0x18` | front/primary drive unit; richest so far |
| `1D` | DMC2-DriveMotorCtrl2 | `0x14` and `0x18` | |
| `1E` | DMC3-DriveMotorCtrl3 | `0x14` and `0x18` | never scanned; addressed by nothing today |
| `28` | BSCM-BrakeSystem | `0x14` only | `7F 22 11` at `0x18` |
| `40` | BCM-BodyControl | `0x18` only | silent at `0x14`; the body-signal candidate |
| `45` | Gateway-GWM | `0x18` (speaks 22, holds little) | answered `7F 22 31` to every DID tried |
| `CB` | BSM-BatterySysMngr | `0x14` and `0x18` | the battery manager |
| `CD` | BSM (second) | returns `7F 22 31` at both | present, serves 22, held nothing tried |

The addressing preamble for a module, exactly as `drive.AddressGroup` already
builds it (this is proven plumbing, do not reinvent it):

```
ATCP<pri>            priority byte: 14 or 18  (drive.AddressGroup.priority)
ATSHDA<ecu>F1        set 29-bit request header, e.g. ATSHDA17F1
ATCRA<filter>        receive filter for that module's reply
ATFCSH<pri>DA<ecu>F1 flow-control header  (set AFTER ATSH, see the comment)
ATFCSD300000         flow-control data
ATFCSM1              flow-control mode
```

Then the read itself: `22XXXX`. A positive reply echoes the identifier as
`62 XX XX <payload…>` (possibly multi-frame, reassembled by
`decode.parse_reply` / `split_can_header`, which already understands this
vehicle's `14 2A F1 CB …` priority-0x14 framing). `enhanced.candidate_scalings`
and `candidate_triples` turn a payload into every candidate window so the
operator can find which one tracks a known quantity.

Negative responses and what each means (`decode.negative_response_name`):

| `7F 22 XX` | meaning | scan action |
|---|---|---|
| `11` | serviceNotSupported | module has no service 22 at this priority — stop scanning it here |
| `22` | conditionsNotCorrect | right service, wrong vehicle state — note; may answer while driving (out of scope for now) |
| `31` | requestOutOfRange | **the common one**: 22 works, this DID does not — record and continue |
| `33` | securityAccessDenied | the DID exists and is protected — record, do not pursue |
| `34` | authenticationRequired | exists, needs Global B auth — record, do not pursue |
| `78` | responsePending | module is working on it — the adapter auto-retries; treat as a slow positive |
| `7E`/`7F` | not supported *in active session* | exists behind another session — record, do **not** send `0x10` |

## 5. The safeguards the scan must implement

These are non-negotiable and are the whole reason the owner's authorization is
safe to act on.

1. **A separate gate.** Add `validate_scan_command` to `safety.py`. It accepts
   an adapter command (by delegating to `validate_command`, exactly as
   `validate_enhanced_command` does) **or** a bare `22XXXX` where `XXXX` is any
   four hex digits — and nothing else. It must refuse every service in
   `FORBIDDEN_SERVICES` and every service that is not `22`. It must **not** be
   reachable from `validate_command` or `validate_enhanced_command`, and it must
   **not** widen `ALLOWED_OBD_MODES` or `ENHANCED_READ_DIDS`. Give the transport
   this validator explicitly, the same way `enhanced.py` does; the default stays
   the unattended gate. Assert at import that `validate_command("2200FF")` still
   raises — the unattended collector must never gain service 22.
2. **Parked only.** Refuse to run unless the vehicle is stationary. Check
   `010D` (speed) is 0 before starting and abort the scan if speed ever reads
   non-zero. A scan adds diagnostic traffic; it has no business on a moving
   truck.
3. **Supervised, opt-in per run.** Dry-run by default (print the range and the
   addressing, open no serial device); transmit only with `--confirm`, the same
   pattern as `hummer-obd-enhanced`.
4. **Paced, not flooded.** One request at a time, with a small inter-request
   delay (start ~50–100 ms of headroom beyond the adapter's own round trip).
   Never pipeline. The BT link caps at a few requests per second anyway; do not
   fight it.
5. **DTC bracketing.** Read stored/pending/permanent DTCs (`03`/`07`/`0A`,
   addressed to module 45 as the probe does — it answers all three, count zero
   today) **before** the scan, after each chunk, and at the end. If any module
   sets a code, **stop** and report it. A lost-communication or
   invalid-request code that appears during the scan is the signal to back off.
6. **Stop on the unexpected.** A normal scan sees only positive `62…` replies
   and the negative codes in the table above. Anything else — `BUS ERROR`,
   `CAN ERROR`, a busy-repeat storm, a module that goes silent when it was
   answering, `UNABLE TO CONNECT` — stops the scan for that module and is
   reported. Do not retry into a wall.
7. **Resumable.** Persist progress (last identifier reached, per module) so a
   scan can stop and resume without redoing thousands of reads. A full
   0000–FFFF per module is ~65k requests; at a few per second that is hours, so
   it will span sessions and charge windows.
8. **Byte-exact raw log.** Every TX/RX pair to the append-only raw log before
   parsing, exactly as every other path does. The value of the scan is
   re-decoding later from what the truck actually said.
9. **Output stays local and private.** Write results under `evidence/scans/`,
   which is already git-ignored by `evidence/.gitignore` (deny-by-default).
   Scan output holds raw diagnostic frames and must never be committed. Publish
   only curated, masked findings into the catalogue, per SAFETY.md's
   publication rules.

## 6. Suggested scan order (fastest path to something new)

A full space is exhaustive but slow. Bias the first passes toward where this
platform is known to keep things, then fill in:

1. **Module 17, ranges the vehicle already uses:** `24xx`, `27xx`, `28xx`,
   `2Axx`, `33xx`, `4xxx`, `54xx`. Module 17 is the front drive unit and the
   most likely home of motor/inverter telemetry.
2. **Modules 1D and 1E**, same ranges — three drive controllers, and 1E has
   never been read at all.
3. **Module 40 (BCM) at `0x18`**, ranges `40xx`–`43xx` and `4xxx` — the body
   signals (doors, locks, lighting, HVAC) live here if anywhere.
4. **Module 28 (BSCM) at `0x14`**, `4Axx`/`4Cxx` and neighbours — suspension
   height and rear-steer are chassis functions.
5. **Module CB (BSM)**, `2xxx` — per-module pack temperature and state of
   health.
6. Then the full `0000–FFFF` on each, resumable, to catch anything outside the
   guessed ranges.

Correlate every positive against something independently observable, the way
the project already validates: motor RPM should track wheel speed, a door DID
should flip when a door is opened, an HVAC DID should move when the climate is
turned on. `hummer-obd-decode` already correlates raw windows against the
recorded columns — feed new findings through it. A plausible number with a
confident unit and no cross-check is the thing to avoid, as `0x2429` (a
"voltage" that was really torque) and `0x5401` both taught this project.

## 7. Concrete build checklist for the next agent

- [x] `safety.validate_scan_command` + import-time assertions + tests in
      `tests/test_safety.py` (mirror the enhanced-gate tests: accepts `22XXXX`
      for arbitrary XXXX, refuses every forbidden service, refuses non-22,
      refuses batching; and `validate_command` still refuses `22XXXX`).
- [x] A new module `src/hummer_obd/scan.py` and entry point
      `hummer-obd-scan` in `pyproject.toml`. Reuse `drive.AddressGroup`
      addressing, `SerialTransport(validator=validate_scan_command)`,
      `RawLog`, `decode.parse_reply`, `enhanced.candidate_scalings`.
- [x] Args: `--module`, `--priority`, `--start`, `--end`, `--device`,
      `--delay-ms`, `--resume`, `--output-dir evidence/scans`, `--confirm`
      (dry-run without it). Speed check + DTC bracketing built in.
- [x] Resume state file per module/priority/range under `evidence/scans/`.
- [x] Tests for the scan runner against the PTY/ELM simulator
      (`tests/elm_simulator.py`) — assert it sends only `22XXXX` reads, stops
      on a non-zero speed, stops on a DTC, and resumes from state. No vehicle
      needed for the suite.
- [x] Regenerate `docs/ACCESS_MATRIX.md` from `access.py` after adding the gate
      (it is generated — do not hand-edit it), and add a `scan` access level
      row describing the supervised scan path.
- [ ] Update `confidence.py`/catalogue only when a scanned identifier is
      cross-validated on this truck — never on a bare positive response.
      **Deferred:** no live scan or new cross-validated hit is claimed by the
      scanner build. Offline simulator acceptance is not vehicle validation.

## 8. Operating the scan on the node (read-only, no secrets here)

The recorder `hummer-drive.service` opens `/dev/rfcomm0` while the vehicle is
awake, so it holds the port the scan needs. Stop it first and restart it after
(the operator has passwordless rights for exactly these two, per the node's
sudoers): `sudo systemctl stop hummer-drive` before a scan,
`sudo systemctl start hummer-drive` after. Stopping the recorder ends the
session in progress (rows already written are kept); it does not touch the
vehicle. The RFCOMM bind (`hummer-rfcomm.service`) and the Bluetooth watchdog
(`hummer-btwatch`) stay as they are.

Deploy code to the node with `scripts/deploy.sh` (rsyncs source; never restarts
services, never ships secrets or raw logs). Run the scan under the node's
`PYTHONPATH=…/src` the same way the other tools run. Keep the truck **plugged
in to charge** during a scan so the multi-hour bus activity does not draw down
the 12 V battery, and keep it **parked**.

### Scanner operating procedure

Run from the checkout, not an arbitrary working directory. The default is
module `17`, priority `14`, range `2400–24FF`, 75 ms **additional** delay after
each completed exchange, and a DTC chunk of 16 identifiers. `--priority` defaults
to `18` for `40`/`45`, otherwise `14`. Only the eight census module addresses
and priorities `14`/`18` are accepted; no automatic priority search occurs.

```bash
# Safe even while the recorder is running: no serial I/O or files created.
PYTHONPATH=src python3 -m hummer_obd.scan --module 17 --priority 14 --start 2400 --end 2400
```

Only after offline acceptance, and with the vehicle **parked, plugged in and
attended**, the operator can run this on the node. The first live check is ONE
identifier; inspect its local transcript and guards before choosing a wider
range. These privileged commands are for the operator, not agent execution:

```bash
cd /home/jeremy/hummer-obd || exit
(
    sudo systemctl stop hummer-drive || exit
    trap 'sudo systemctl start hummer-drive' EXIT
    PYTHONPATH=src python3 -m hummer_obd.scan \
        --module 17 --priority 14 --start 2400 --end 2400 --confirm
)
```

The exit trap attempts to restore the recorder after success or a normal
scanner error; it cannot survive node power loss or SIGKILL. Always verify
`systemctl is-active hummer-drive` afterward. Do not clear DTCs if a scan stops
on one. Leave Bluetooth binding/watchdog services alone. Never use `--confirm`
on a moving vehicle, even though the scanner also checks speed itself.

Use the same command plus `--resume --confirm` to continue an interrupted
range. An existing state cannot be overwritten by starting without `--resume`.
Each module/priority/range has its own `.state.json` and `.raw.jsonl`; for example
`module-17-priority-14-2400-24FF.state.json`. A different range is a separate
experiment, not a continuation. Delay, timeout and chunk size can be changed on
resume without changing the recorded scope.

`--output-dir` may select a subdirectory **under the current checkout's
`evidence/scans/` only**, with symlinks refused. New files are private (0600).
The raw transcript is append-only and includes `did_result` events containing
the full payload and candidate scalings, as well as byte-exact TX/RX records.
Each result is flushed/fsynced before its atomic resume cursor advances. After
a crash between those operations the last request may repeat; a completed
result is never skipped merely because its decoded value looked uninteresting.
Do not print/publish raw logs: a full scan includes identification DIDs such as
`F190`, which may contain a VIN. Console output contains status, not payloads.

Safety checks use the unchanged ordinary gate on the same locked physical
port; the scan gate itself still refuses `010D`, `03`, `07` and `0A`.
`010D` from `17` at `18` must return zero before every DID. Missing speed is
not zero — service 01 can sleep even while charging (CAN_PRIORITY.md), in
which case this scanner correctly refuses to proceed. No alternate speed
source, ignition/session command or override is attempted.

DTC checks address `45` at `18` before the range, every `--chunk-size` reads
(1–32), and after normal completion or service-not-supported. Even an existing
DTC prevents the scan. A completed all-zero response is required for all three
services; absence or malformed counts are not a clean bill of health. On an
unsafe stop (motion, bus failure, silence, DTC, interruption), **no further
requests** are sent, including postflight reads. The aborted state explicitly
records that final bracketing was not completed. Resume always re-establishes
the guards; a durable `11` finding cannot resume into more DIDs at that module.

Only complete, header-attributed classical-CAN responses are accepted. An
unaccompanied `78` does not advance the cursor; up to three pending frames may
precede a terminal response in the same adapter transaction. The first `21`
(busyRepeatRequest) stops the run rather than retrying toward a storm. Locked
or other-session responses are recorded, never pursued. `--delay-ms` is bounded
to 50–2000 and `--timeout` to 0.1–10 seconds. Requests are strictly serial; the
extra speed/addressing/DTC traffic is included in pacing. Measure throughput
on the first small range rather than extrapolating a bare-loop request rate.

### Acceptance and interpreting hits

The hardware-free acceptance suite is `python3 -m pytest -q`; scanner-specific
coverage is in `tests/test_scan.py` and gate isolation in `tests/test_safety.py`.
Generated access documentation is checked with
`PYTHONPATH=src python3 -m hummer_obd.access --check`.

A positive response establishes only that a payload was returned at this
module/priority/state. No units or new recorder fields are inferred. Keep
separate timestamped observations for deliberate parked door/HVAC changes and
repeat only the candidate DID in fresh, bounded experiments. Motor-speed
correlation cannot be established by this parked-only scanner. Do not drive
with it running to obtain variation.

`hummer-obd-decode` currently consumes **session CSVs**, not scanner JSONL, and
its current CSV reader preserves only known raw columns. Feeding it a raw scan
log is not a validation workflow. New-hit ingestion/alignment and independent
observable-state cross-validation remain a follow-up after actual hits exist;
do not promote a DID to `ENHANCED_READ_DIDS`, `drive.py`, or the confidence
catalogue before that work. Candidate scalings alone are not evidence.

## 9. What this does not change

- The collector still uses `validate_command`, which refuses service 22.
  The existing `hummer-drive` recorder uses `validate_supervised_command`, the
  union of ordinary reads and the **exact** enhanced DID allowlist. Neither
  acquires the new scan gate or any new identifier from this change.
- `ENHANCED_READ_DIDS` still governs what the recorder *logs*; an identifier
  the scan discovers is added there (and decoded) only after it is
  cross-validated on this vehicle.
- Every write/control/security/reset/session prohibition is intact and still
  machine-checked.
- Raw data still never leaves the node; only masked, curated findings are
  published.
