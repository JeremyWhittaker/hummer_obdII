# Hummer telemetry expansion

## What the project already does

The strongest part of this reader is its vehicle-specific evidence chain:
allowlisted diagnostic identifiers, byte-exact captures, per-module decoding,
and a confidence registry. `drive.GROUPS` and `drive.COLUMNS` define the actual
recorder inventory; `confidence.CONFIDENCE` distinguishes a field that merely
answers from one independently cross-validated. Standard OBD reads cover only
part of the useful EV data. The dedicated drive recorder already collects
enhanced battery, pack-power, chassis and body-controller data.

That makes reading the recorder's output the useful expansion point during a
drive. A second serial client would compete with the recorder. Discovering an
unknown identifier or assigning an attractive meaning to raw bytes would need
new evidence; neither is necessary to make the measurements already collected
more useful.

The initial analysis found gaps between capture and presentation:

- The principal live interface was terminal text, with no browser view or
  interactive session/power-history selection.
- The text snapshot measured signal age relative to the final row. A browser
  needs wall-clock freshness too, or a stopped file can appear current.
- Combining independently last-seen voltage and current can create power that
  was never measured in one row. Derived pairs need coherent samples.
- Integrating across missing readings overstates coverage. Clamping each
  endpoint separately at a power sign change overstates both energy directions.
- Whole-session electrical draw includes parked loads. A distinct observed
  moving/stationary budget makes that difference explicit.

## Added interface and calculations

`hummer-obd-dashboard` serves a self-contained page and two GET endpoints:

| Endpoint | Result |
|---|---|
| `/api/sessions` | Recent session IDs ordered by file activity |
| `/api/snapshot?session=latest` | Freshness, signals, provenance, derived values, report, energy budget and recent charts |
| `/api/snapshot?session=drive-YYYYMMDDTHHMMSSZ.csv` | The same snapshot explicitly labelled historical |

There are no diagnostic-command endpoints, vehicle writes, uploads or arbitrary
file downloads. Only named recorder columns are exposed; GPS coordinates and
satellite timestamps are omitted, while fix mode and satellite count may be
shown. A raw identifier stays labelled raw even if another part of the same
identifier has a validated scaling. Responses are not cached by the browser.
Symlink sessions, path traversal and unexpected Host headers are rejected.

The new energy budget measures pack energy, using `pack_v * pack_a` within
each row. It needs valid speed at both endpoints (standard speed or all four
wheel speeds), positive time progression and an acceptable interval length.
Intervals with either endpoint moving are attributed to movement; stationary
energy is counted only when both endpoints are stopped. The sampling cadence
limits how precisely a stop/start or short braking event can be attributed.
Missing intervals are counted and omitted, never filled with zero power.

Driving returned energy is separate from stationary energy flowing into the
pack. The latter is not called regenerative braking or wall-charger energy.
Stationary draw includes accessories and all other HV loads; identifying one
component would require a separately validated signal.

The dashboard retains at most one parsed session, lists at most 200 recent
sessions, and returns at most 600 actual chart points. Files larger than 16 MiB
or 20,000 rows require the offline analyzer. These limits keep browser work
bounded on the Pi. A UTC timestamp more than five seconds ahead of the viewing
host is treated as an untrusted clock, not a fresh reading. The default stale
threshold is 45 seconds and is configurable.
For an explicitly selected historical session, signal ages describe the time
before that session's final row; its session timestamp remains visibly historical.

## Evidence boundaries

No new CAN request or DID is introduced by this expansion. Existing command
allowlists, polling cadence, recorder service and sleep behavior are unchanged.
The existing module/identifier confidence registry remains the source of truth.
Some old overview text predates GPS and enhanced drive recording; consult the
GPS section, recorder code and access matrix when a historical overview
conflicts with them.

Neither the inferred energy/SOC capacity ratio nor current-step resistance is
a manufacturer-certified state-of-health measurement. Resistance depends on
sample timing, temperature, state of charge and sufficient current variation.
The browser withholds a nonpositive or weakly correlated resistance fit.
Chassis signals and raw arrays are useful for research but are not a validated
fault detector. Short transients between samples cannot be reconstructed.

The next decoding work should use repeatable observations against recorded raw
fields, as described by `hummer-obd-experiment` and `hummer-obd-respond`, with
independent checks before assigning units. The existing unknown thermal/body
fields offer a better grounded next investigation than guessing adjacent DIDs.

## Verification

The entry baseline passed 971 tests plus 2,683 subtests. Regression coverage for
the browser includes offline operation, zero-vs-missing values, changing files,
wall-clock staleness, invalid clocks, private-field exclusion, traversal and
symlink refusal, HTTP method restrictions, signal confidence, chart bounds,
and sampled energy accounting. Numerical regressions also cover paired values,
capture gaps and drive/regen transitions in the existing live analyzer.

Use `python3 -m pytest -q` for the full suite. Browser acceptance must include
desktop and mobile rendering, session selection, signal filtering, chart
selection, empty/stale states and network failure. Live verification reads
private recorder files; those files and screenshots of their contents remain
ignored and must not be committed to this public repository.
