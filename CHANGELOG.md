# Changelog

All notable changes to this project are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project intends to follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Nothing has been released yet. There is no tag and no published artifact, so
every entry below sits under Unreleased; the `0.1.0` in `pyproject.toml` is a
build placeholder, not a shipped version.

These entries were reconstructed from the commit history, and they keep that
history's standard: where a capability is implemented but has never been
demonstrated on the vehicle, the entry says so. A changelog that overstates is
worse than no changelog, because the reader has no way to tell which lines to
discount.

## [Unreleased]

### Added

- **Fifteen sourced candidate identifiers, and two supervised profiles to test
  them.** Nothing claims they work here. Every one is a service 22 read, so the
  worst outcome of asking is a negative response, and they are allowlisted to be
  *tested* rather than trusted. Group one targets the battery manager `CB` --
  already answering eight identifiers, so the addressing is proven and only the
  identifiers are in question -- from meatpiHQ/wican-fw issue #884. Group two
  targets the body control module `40`, which this vehicle named in its own
  service 09 inventory and has never been asked anything, from
  OBDb/Cadillac-LYRIQ PR #14. The reason to treat that PR's register families as
  candidates rather than guesses is that it is the same source that supplied
  `2414`, which *is* proven here against the energy field's slope during a real
  AC charge. Both profiles run under the existing `--confirm` path with exact
  enumerated identifiers; neither is in the drive recorder, and the unattended
  collector still refuses service 22 entirely.

- **A link that died while parked could never be recovered.** `record()` revives
  a link that dies mid-session — that was fixed this morning — but nothing
  revived one that died while the vehicle was asleep, so the watch loop sat on
  a dead file descriptor indefinitely. Observed live: the vehicle slept, the OBD
  port lost power, the adapter dropped Bluetooth, and the recorder reported
  "adapter still silent" every five minutes against an rfcomm channel showing
  `closed`. It would never have recorded again without a restart. Past the
  prompt retries the watch now reopens the link, which works because
  `hummer-rfcomm` binds the device connect-on-open. It still sends nothing that
  reaches the CAN bus, and a test asserts that rather than asserting the
  narrower "only ATRV", since reopening legitimately re-sends the adapter's own
  session header.

- **Each address group carries its own CAN priority, and module 40 joins the
  recorder.** There is no universal priority, established by asking every module
  at both: 17, 1D, 1E and CB answer at `0x14` **and** `0x18`; module 28 answers
  at `0x14` and returns `7F 22 11` (`serviceNotSupported`) at `0x18`; module 40
  answers only at `0x18` and returns nothing at all at `0x14`. So 28 and 40 are
  mutually exclusive under one global priority — which is exactly why module 40
  could not be added until `AddressGroup` grew a `priority` field.

  All nine of its identifiers now record every cycle as **raw** columns, and a
  test asserts no column name claims a unit. Verified live: 31 of 31 rows
  carrying `evse_current_raw`, three group voltages, three battery temperatures
  and two coolant temperatures, alongside everything that already worked.

- **Pack voltage answers at all three drive motor controllers.** `0x2885` was
  proven only at module 17; asked at `0x18` it answers at 1D (382.39 V) and 1E
  (382.37 V) too, against a live `pack_v` of 382.65. Module 1E had exactly one
  identifier ever put to it before this, and that one a 12 V reading.

- **Module 45, the gateway, is reachable and holds none of the ISO set.** Its
  first-ever service 22 requests returned `7F 22 31` to all four standard
  identification identifiers — reachable, conversing, and empty of those.

- **Module 40 was never unreachable, and the fault was ours.** Thirteen
  identifiers had drawn `NO DATA` and the published conclusion was "the request
  is not arriving" and "testing more identifiers at this address is wasted
  effort". Both false. `_module_profile` hardcodes `ATCP14` — the CAN priority
  the battery manager answers at, established when `CB` was opened and inherited
  silently by every module probed through it. Module 40 answers at `0x18`, the
  priority the legislated services have been using all along.

  **All nine LYRIQ candidates answer at `18DA40F1`**: EVSE advertised current,
  three battery group voltages, HV battery temperature, two more battery
  temperatures and two coolant temperatures. No scaling is claimed for any of
  them — `416C` read 2589 then 2593 a minute apart, `416D` and `416E` returned
  identical values, and the vehicle was parked and unplugged, which is the state
  that says least about an EVSE current.

- **A per-module support census, using only the standard's own discovery
  calls.** `hummer_obd.discover` (`hummer-obd-discover`) asks each of the eight
  modules this vehicle named for itself which service 01 PIDs, service 09 items
  and service 06 monitors it supports. Those bitmaps are what J1979 defines for
  asking, so nothing here is a guess or a sweep, and a test asserts the tool can
  only ever send support bitmaps and addressing commands — never a vendor
  identifier. The bank walk advances only when a bitmap points onward, so a
  module supporting one bank costs one request rather than seven.

  It immediately paid for itself: module 40 answering `01 42` / `04 06 0A` is
  what exposed the priority error above. It also shows module 17 alone
  advertising nine service 01 PIDs against two everywhere else, which both
  proves the receive filter isolates and explains why the functional broadcast
  could never have revealed this.

- **Module `CD`'s closure was retested and holds.** The same doubt was applied
  to `CD`, which had refused seventeen identifiers at `0x14`. At `0x18` it
  refuses too, answering from its own address both times. That conclusion was
  right for the reason originally given rather than by luck.

- **Both open module questions answered, both in the negative, both useful.**
  Recorded in [Probe, 2026-09-03](docs/PROBE_2026-09-03.md), run parked and
  awake with the session pulled to a workstation first.

  **Module `40` does not answer at `14DA40F1`.** Four ISO 14229-1 standard
  identification identifiers returned `NO DATA`, joining nine vendor ones. A
  module that answers nothing — including the identifiers the standard defines
  for exactly this purpose — is not a module missing those identifiers; the
  request is not arriving. Thirteen for thirteen closes the question the first
  probe could not: testing more identifiers at this address is wasted effort,
  and what remains open is the *route*.

  **Module `CD` refuses everything, including the standard.** Seventeen
  identifiers now — four ISO standard, four proven at `CB` before the first CD
  probe, five discovered at `CB` since — every one answered
  `142AF1CD 03 7F 22 31`. `CD` is genuinely reachable and speaks service 22, but
  an ECU declining `F187` through `F191` is not hiding a namespace behind
  identifiers nobody has guessed; it exposes nothing in the session it answers
  in. The way to ask for a different session is service `0x10`, which this
  project permanently forbids. So `CD` is closed **from this access path** — not
  because the identifiers are wrong.

  Neither module gave up data and both experiments succeeded. Before them the
  plan pointed at `CD` as the most promising target on the vehicle and at `40`
  as a problem of finding better identifiers. Both directions would have
  absorbed a session each; they are now marked, with the evidence for the
  marking.

- **The identifier registry is generated from the gate that enforces it.**
  `docs/GM_ENHANCED_CANDIDATES.md` is the provenance record for everything this
  project may transmit, and it had fallen **thirty-six commits behind the
  code** — missing the traction-pack identifiers that closed the project's
  largest gap, and every candidate added since. That is the third hand-kept
  inventory here to drift, after the README's column count and the drive unit's
  identifier list, so the fix is structural rather than another correction:
  `hummer_obd.registry` renders the table from
  `safety.ENHANCED_READ_DIDS`, and `tests/test_registry.py` fails when the
  document and the gate disagree. Only the table is generated; the reasoning
  around it stays hand-written, because that is the part worth writing by hand.

- **Reachability probes that separate "unreachable" from "no such identifier".**
  Nine sourced identifiers at module `40` returned `NO DATA` — nothing replied,
  so the identifiers were never really what was under test; the route was. Four
  **ISO 14229-1 standardised** identification DataIdentifiers (`F187`, `F188`,
  `F189`, `F191`) are now allowlisted for exactly that question: an answer
  proves a module is reachable and speaks service 22, `7F 22 31` proves the same
  while denying that identifier, and only continued silence means the request is
  not arriving. `F190` (VIN) is deliberately excluded — it would answer the same
  question while pulling vehicle identity into an evidence file.

- **The second battery manager gets the eight identifiers it was never asked.**
  The earlier `CD` probe put only four of `CB`'s working identifiers to it and
  drew `7F 22 31` on all four. `27C7`, `27C0`, `0046` and `5401` were proven at
  `CB` before that probe and simply left out, and five more were discovered
  after it. `bsm-cd-next` now asks all nine.

- **`0x2AF1`'s twenty-four values are now recorded every cycle and broken out
  individually.** Having proven it answers, capturing it once was not the point
  — the scaling can only be settled by a temperature *range*, and that needs it
  in every session. It is stored **raw**, and a test asserts no column or label
  anywhere claims a unit: the source calls these module temperatures, and one
  sample at one temperature is not enough to name a column after.

  The live view breaks all twenty-four out with each value's drift from the
  array's median, so a single module running hot shows up against its
  twenty-three siblings. Unlike `0x2B43` it is compared against itself as a
  whole rather than in blocks, because `0x2B43`'s two blocks were *measured*
  before they were used and nothing has measured any structure in this one.

  `array_2af1` was added to the reader's text-column set in the same change —
  the mistake that made `cell_extra_raw` read as "never answered" was made once
  and is now covered by a test.

- **Fifteen sourced candidates tested on the vehicle: five hits, nine
  silences.** Recorded in [Probe, 2026-09-03](docs/PROBE_2026-09-03.md), run
  parked and awake straight after a highway drive, with the drive session pulled
  to a workstation first so nothing recorded was at risk.

  All five candidates at the battery manager `CB` answered on the first
  attempt: `27BF` (33), `27BB` (100), `27B5` (21), `2709` (110), and `2AF1` —
  which returns **twenty-four values**, the same count as the twenty-four
  module-like values in `0x2B43`, on a pack whose series count measures 96 and
  whose array splits into two blocks of twelve. Under `(x − 40) / 2` those
  twenty-four land at 37.0–37.5 °C against a pack temperature of 39.0 °C
  measured independently in the same minute. Close, and explicitly **not**
  claimed as proven: it is one sample at one temperature, and a scaling that
  lands near the right answer at 39 °C says nothing about whether it holds at
  5 °C or 55 °C.

  All nine candidates at the body control module `40` returned `NO DATA` — and
  that is not a negative response. `7F 22 31` would have meant "this module is
  here and does not have that identifier"; `NO DATA` means nothing answered at
  all. So this is not evidence the identifiers are wrong, but that nothing
  responds at `14DA40F1` on this vehicle, even though it names `40` as
  `BCM-BodyControl` in its own service 09 inventory. Recorded because a silence
  costs the next person a whole session to rediscover.

- **A hex column left out of the reader's text set read as never answered.**
  `cell_extra_raw` was added to the recorder and not to the reader, so every
  value went to the number parser, failed, and became `None`. The live view
  reported "NEVER ANSWERED 0/33" for a column the CSV plainly contained. The
  failure is silent in the worst way: these values begin with a digit, so the
  unit-suffix stripper that rescues `"13.8V"` leaves them alone and `float()`
  rejects the whole string — a field that is recorded but unreadable is
  indistinguishable from one never recorded. Found by the live view during a
  real drive, which is what its answering/not-answering distinction exists for.

- **A 97.8 kW drive froze two of `0x2AF5`'s unknown bytes.** Bytes 7 and 9 held
  at 15 and 23 through a parked pack, a 117 km/h cruise and a full-power pull
  with the pack sagging 384.88 → 381.73 V under 256 A, while bytes 6 and 8
  moved. A value that does not move across that range is not measuring it. This
  partly revives the refuted index idea in a different form: byte 7 does not
  index the `0x2B43` array, but 15 and 23 indexing *cells or modules* would
  explain constancy exactly, since the same cells stay weakest and strongest.
  Recorded as a live hypothesis in
  [Pack architecture](docs/PACK_ARCHITECTURE.md), not a result.

- **Range, energy and charge cross-validate, and match the EPA figure.**
  `range_mi / soc_pct` gives 333.1 mi at 100 % (sd 0.0124) and
  `range_mi / energy_kwh` gives 1.7364 mi/kWh (sd 0.0043), which against
  191.9 kWh usable is 333 mi — the same answer by an independent route, and
  1.2 % above the published 329 mi rating. Four fields now agree with each
  other, which is far stronger than any one being individually plausible.

- **The 12 V rail is measured two independent ways, and they disagree by 6 %.**
  `ATRV` (adapter, no CAN traffic) and `0x33E5` (drive motor controller `1D`)
  correlate at +0.955 over 774 paired samples, which validates the `byte / 10`
  scaling. But the adapter reads consistently higher, and the *ratio* between
  them is ten times tighter than the *difference* (0.36 % vs 5.8 % relative
  spread), pointing at a scale difference rather than an offset. Over the
  observed 12.9–13.9 V span the two models cannot be told apart, and the
  document says so rather than picking one. Nothing is wrong today —
  `WAKE_VOLTS` is compared against the same source it was measured from — but a
  threshold derived from a module reading must not reuse that number.

- **Usable pack capacity measured, and both battery decoders cross-validated.**
  `energy_kwh` (`0x27AF`, `/100`) over `soc_pct` (`0x27C6`, `/655.35`) is the
  capacity the vehicle works from. Two identifiers, two different scalings — if
  either were wrong the ratio would drift with state of charge. Across the
  sessions where charge actually moved, 78.85 % to 89.65 %, it holds between
  **191.84 and 191.94 kWh: a spread of 0.05 %** over an eleven-point swing. That
  puts usable capacity at about **191.9 kWh** and is strong evidence both
  scalings are correct.

- **The charge curve, and a third confirmation of 96 cells in series.** Mean
  cell voltage against state of charge over 1247 samples is monotonic at
  **7.58 mV per percent** (correlation +0.974). Extrapolated to 100 % that is
  4.1746 V per cell — and at 96 series cells, **400.8 V** at the top of a
  400 V-class pack, with a full-charge cell voltage sitting just under the
  ~4.2 V ceiling exactly where a manufacturer holding margin for longevity
  would stop. Recorded in [Pack architecture](docs/PACK_ARCHITECTURE.md).

- **`0x5401` is answered: it is not charger power.** The decoder kept this byte
  raw with a note saying it would stay so "until a charging session gives it a
  reference." The corpus contains one. The byte correlates with pack current at
  −0.81 over 297 paired samples, so it is certainly tied to charging — and just
  as certainly not power, because while charging it holds 147–152 across a
  measured **1.85 to 16.51 kW**, a ninefold power range moving it by at most
  five counts. It reads zero in 227 of 254 samples taken while not charging.

  The decisive evidence is the end of a charge: with state of charge flat at
  89.653 %, energy flat at 172.03 kWh and current at zero — charging already
  over — it decayed monotonically to zero across three and a half minutes
  (36, 33, 30, 26, 23, 20, 16, 13, 6, 0). A plateau while working and a slow
  ramp down afterwards is what a demand or duty signal looks like, not a power
  reading, and not battery temperature either (correlation −0.25). It stays raw
  because "shaped like a duty cycle" is not a unit, but the question the code
  posed is now closed.

- **The `0x2AF5` index hypothesis was tested and refuted.** The suggestion that
  byte 7 names the weakest cell was checked against 1314 cycles where `0x2AF5`
  and `0x2B43` were read back to back: byte 7 matched the array's minimum index
  **0 times**, and byte 9 matched its maximum **0 times**. Not weakly supported
  -- never true. Recorded in [Pack architecture](docs/PACK_ARCHITECTURE.md)
  alongside the correlation table showing those four bytes reach at most 0.55
  against anything already measured, which is what a field carrying information
  this node does not otherwise have looks like.

- **Columns holding several values are broken out individually.** `0x2B43`'s
  26 per-module readings were captured every sample and stored as one hex
  string; the live view now shows each one on its own line with **its drift
  from its own block's median**. Drift is the useful figure: absolute values
  move together as the pack charges, so a single value pulling away from its
  neighbours is the earliest visible sign of one module going bad, and it shows
  long before it moves the pack-wide min/max envelope. The two blocks are
  measured, not assumed, and drift is computed within a block because the two
  halves sit at different levels -- a whole-array comparison would flag every
  member of the lower block and hide a real outlier inside it. `--compact`
  restores the collapsed view.

- **`0x2AF5`'s four discarded bytes are kept.** Measured over 1315 reassembled
  replies, that identifier always answers with ten bytes and the decoder read
  six. Four per sample were dropped before reaching the CSV, so nothing later
  could recover them and nothing revealed they existed. They are preserved raw
  in a new `cell_extra_raw` column, deliberately undecoded: byte 9 is constant
  at 23 across all 1315 samples and byte 7 takes only seven values between 13
  and 24, which is what indices look like rather than measurements. Naming a
  column after a guess is worse than keeping bytes under a plain name.

- **[Pack architecture](docs/PACK_ARCHITECTURE.md), derived from the vehicle's
  own recordings.** 96 cells in series, measured as `pack_v / cell_avg_v` over
  297 samples -- mean 95.991, standard deviation 0.041 -- from two identifiers
  on two different modules, so the ratio is not one decoder's artefact. All 26
  values of `0x2B43` track state of charge at +0.995 or better, and split into
  two statistically distinct blocks of twelve: spread across the 24 is 2.1x the
  spread within either block. Twelve modules of eight cells is 96, which is
  what the voltage ratio independently measures. The document separates what is
  measured from what is inferred, and says plainly which parts are arithmetic
  on figures this node never measured.

- **A live text view of every sensor, and whether it is still answering.**
  `hummer_obd.live` (`hummer-obd-live`, `--watch` to refresh) prints one line
  per column: the value, the identifier carrying it, how long since it last
  answered, and how many of the recent rows it appeared in -- grouped by the
  module it comes from, so a whole module going quiet reads as one silent block
  instead of scattered blanks. A sensor that has stopped answering therefore
  looks nothing like one reporting zero, which is the distinction that matters
  when something is wrong and the one the CSV alone cannot show.

  It never opens the serial device. It reads the session the recorder is
  already writing, because two processes on one RFCOMM device would corrupt
  both streams, and because the recorder's own output is what the recorder is
  *actually getting* rather than what a second connection would get. Safe to
  run at any time, including while driving; a test asserts the module contains
  no `serial`, `Transport`, `rfcomm` or `.send(`.

  The map of which module supplies which column is derived from `drive.GROUPS`
  and `drive.DECODERS` by asking each decoder what it returns, rather than
  written out by hand. Every hand-kept inventory in this project has drifted
  from the code at least once, and this one would be read while something was
  already going wrong.

- **The node can restart its own recorder without a password.**
  `scripts/enable_service_control.sh` installs a narrow sudoers rule granting
  start/stop/restart on four named hummer units and nothing else. This exists
  because the recorder's code lives in the operator's home directory and can be
  updated with no privilege at all, while the running process only picks up new
  code on a restart -- so a fix could be deployed and then sit inert on disk
  while the vehicle drove away. That is not hypothetical: on 2026-09-03 the
  wake-detection fix reached the node before the drive home and was not
  running, because nobody was there to type a password.

  Each rule is an exact command string, so no arguments can be appended, and
  there is deliberately no wildcard: `systemctl` can run arbitrary code as root
  through transient units, so `systemctl *` would be a root escalation wearing
  a service-manager costume. The units already run as the operator's own user,
  so restarting them grants nothing that user could not already do. The file is
  checked with `visudo -c` and installed only if it parses, because an invalid
  file in `/etc/sudoers.d` breaks sudo for every user and recovering it needs
  physical access to a machine that lives in a vehicle. The script finishes by
  restarting once more *as the operator* rather than as root, since a rule that
  installs cleanly but does not work would be discovered at the worst moment.

- **A recorded session can now be read back.** `hummer_obd.analyze`
  (`hummer-obd-analyze`) turns a `drive-*.csv` into a report, entirely offline
  and without opening the serial port -- the same constraint the capabilities
  report is held to, asserted by a test that greps the module for `serial`,
  `Transport` and `rfcomm`. Until now the project could record a drive and had
  nothing that could say what the drive showed.

  The report leads with **capture quality**, because that decides what the rest
  is worth: it measures the sample period the session actually achieved rather
  than the one that was configured, and counts gaps longer than three times the
  median as lost samples. That section is what makes a half-rate capture or a
  dropped-Bluetooth minute visible at all; both were invisible before.

  It then reports distance (from the odometer *and* from integrated speed, and
  warns when the two disagree), energy used, efficiency in mi/kWh and
  kWh/100mi, the pack size the vehicle's own `energy_kwh` and `soc_pct` imply,
  energy drawn and energy regenerated separated by the sign of pack current,
  pack voltage sag, cell spread, thermal range, and chassis extremes.

  It normalizes the two power columns, which carry **opposite** sign
  conventions: `hv_power_kw` is `pack_v * pack_a` and is positive while
  discharging, while `power_kw` is the slope of energy *remaining* and is
  negative while discharging. Both are kept and their disagreement is reported
  rather than averaged away. A `volts` column that arrives as the string
  `"13.8V"` is parsed rather than discarded, and a torn final row -- what
  losing power mid-write leaves behind -- is dropped with a warning instead of
  poisoning the totals.

- **High-voltage battery state of charge, over OBD.** The project previously
  stated that EV battery data could not be obtained through this port. That was
  wrong. A single supervised UDS `ReadDataByIdentifier` request for identifier
  `0x27C6`, addressed to `0x14DACBF1` with CAN priority `0x14`, returns a
  reproducible value from this vehicle: `62 27 C6 D1 8A`, which the published
  decoder maps to roughly 82 %. The identifier came from a community profile
  that names this vehicle, not from a guess. **The value has not yet been
  cross-checked against the dashboard**, so the honest claim is a stable
  reading in a plausible range rather than a confirmed percentage.
  See `docs/GM_ENHANCED_CANDIDATES.md`.
- **A second safety gate, deliberately narrower than the first.**
  `safety.validate_enhanced_command()` accepts service `22` only for an
  identifier enumerated in `ENHANCED_READ_DIDS`, and refuses every other
  service — including the ordinary read services the collector is allowed to
  send. Service `22` was **not** added to `ALLOWED_OBD_MODES`; an import-time
  assertion now fails the build if it ever is, so unattended collection cannot
  transmit an enhanced read regardless of configuration. The gate refuses
  `0x27C5` and `0x27C7`, one step either side of the identifier that works,
  which is the anti-sweep property and is asserted by test.
- **`hummer_obd.enhanced`**, a supervised experiment runner. Dry run by
  default: without `--confirm` it prints and validates the exact byte sequence
  it would send and never opens the serial device. One request per identifier
  per run, no loop. Records the connector voltage alongside every read, because
  a `NO DATA` at 12.8 V and one at 13.8 V are different results.
- Adapter commands `ATCP`, `ATFCSH`, `ATFCSD` and `ATFCSM` on the read-only
  allowlist. `ATCP` is required because `ATSH` carries only three of the four
  bytes of a 29-bit identifier; the `ATFCS*` group configures ISO 15765-2 flow
  control for multi-frame replies, which service 09 already depends on.

### Fixed

- **The session report keyed distance off the least reliable column it had.**
  `odometer_km` and `speed_kph` are standard OBD PIDs, and on 2026-09-03 they
  answered in 8 of 79 rows while every enhanced read answered in all 79. The
  report therefore described a 12.6-mile drive as 0.06 miles. It now computes
  distance four ways -- the odometer, `dist_since_chg_mi`, the four wheel
  speeds, and integrated `speed_kph` -- shows all of them, uses the densest
  trustworthy one, and names which it used in `distance_basis`, so no reader
  has to guess what fed the efficiency figure. A negative `dist_since_chg_mi`
  delta is the counter resetting on a charge rather than a reversing truck, and
  is discarded. The derived wheel-speed mean is built into throwaway rows
  rather than written back, so a column invented by the report is never listed
  in completeness as though the vehicle had sent it.

- **The wake threshold sat above the voltage this vehicle drives at.**
  `WAKE_VOLTS` was 13.2, chosen against bands measured on a *parked* vehicle:
  12.7-12.9 V asleep, 13.7-13.9 V running. Driving turns out to sit between
  them. The ATRV probes taken across the drive lost on 2026-09-03 read 13.1,
  13.1 and 13.0 V -- every one below the threshold. A vehicle awake for a while
  charges its 12 V battery full, and the DC-DC then holds a float near 13.0 V.

  This corrects an earlier reading of the same evidence. The 12.9 V sample that
  suggested the asleep and driving bands overlapped was a single point on the
  way down during shutdown, not a steady state; the steady values are 12.7 V
  asleep and 13.0-13.1 V driving. The bands do not overlap, and 12.95 is the
  only value that separates them at the 0.1 V resolution `ATRV` reports.

  Without this the earlier fix was not enough on its own: keeping a session
  alive once started does nothing if the session can never start, and this
  vehicle drove its whole commute without the rail once reaching 13.2 V.

  The margin is acceptable now only because a false wake became cheap. A
  sleeping vehicle answers nothing, so the dead-cycle check ends the session
  within about three cycles having sent a handful of requests no module
  replies to. Before that check existed, a threshold this close to the sleeping
  band would have been reckless.

- **A real 12.6-mile commute was recorded as "asleep", and voltage was the
  reason.** On 2026-09-03 the vehicle idled awake for 23 minutes at 13.9 V,
  which topped up its 12 V battery. The DC-DC converter then dropped to float,
  and the entire drive was made at 12.9-13.1 V -- below `WAKE_VOLTS` (13.2).
  The recorder read the last sample of the session at 12.9 V, declared the
  vehicle asleep, ended the session, and slept 300 s at a time through the
  whole drive. The raw transcript shows it exactly: full-rate traffic until
  15:48 UTC, then precisely two events at 15:53, 15:58 and 16:03 -- one `ATRV`
  each, five minutes apart -- then full rate again at 16:08. The odometer moved
  2197.7 to 2218.0 km (20.3 km) and `dist_since_chg_mi` went 0.0 to 12.68 with
  nothing recording in between.

  Lowering the threshold cannot fix this. The measured asleep band is
  12.7-12.9 V and the vehicle drove at 12.9-13.1 V, so the two bands overlap:
  no single voltage separates "asleep" from "driving with a full 12 V
  battery". Voltage cannot answer the question being asked of it.

  A session now ends when the vehicle stops **answering**, not when the rail
  reads low. `stop_when` no longer consults voltage at all; the dead-cycle
  check added above already detects a vehicle that has gone quiet, and voltage
  is kept only as corroboration *after* the answers have stopped -- which is
  what separates a sleeping vehicle (clean end) from a broken link (reconnect,
  then exit for a restart). A vehicle answering enhanced reads is awake
  whatever its 12 V rail says, and that is now what the code believes.

  The unchanged half is deliberate: `WAKE_VOLTS` still decides when to *start*
  a session, so a genuinely parked vehicle is still never polled on the CAN
  bus. Only the decision to stop moved.

- **A hung-up adapter was recorded as data for the rest of the session.** Every
  transport failure inside a cycle was caught per group and the loop continued,
  which is right for one quiet module and wrong for a link that has gone away.
  Nothing in `drive.py` ever called `transport.reconnect()`, although
  `SerialTransport` implements it with capped backoff and `collector.py` calls
  it on exactly this error. pyserial does not close the port on an I/O error,
  so the transport never noticed by itself: after an RFCOMM hang-up the
  recorder wrote rows carrying nothing but a timestamp, once per cycle, until
  `DRIVE_MAX_SESSION_S` -- two hours by default -- while the service stayed
  `active (running)` and the journal stayed silent. `run_auto` then polled the
  same dead file descriptor forever. Reproduced offline: 40 cycles, 280
  swallowed errors, every one of the 27 data columns empty.

  A cycle that decodes nothing is now recognised as a dead link rather than as
  a sample, and no row is written for it, because a row of nothing but a
  timestamp makes a dead link look like data. The first such cycles reopen the
  link *and re-send the session header* -- reopening the RFCOMM device
  re-establishes the Bluetooth link, which returns the ELM to power-on
  defaults, so reconnecting without re-initialising would leave echo on and no
  protocol selected, which reads as corrupt data rather than as a dead link.
  After `DEAD_CYCLES_BEFORE_EXIT` consecutive dead cycles the recorder raises,
  `main()` returns 3, and `Restart=always` hands the next process a freshly
  bound device. Exiting is the recovery path here, not a failure of it; the
  unit comment already said as much, and this handler was what prevented it.

  Found by an adversarial audit of the capture path, which raised 58 findings
  and refuted 53 of its own. One quiet module still costs only its own columns,
  which is the distinction the whole check rests on and is tested directly.

- **One dropped Bluetooth read ended a session as if the vehicle had gone to
  sleep.** The recorder decided a session was over with
  `(_volts(transport, timeout) or 0) < WAKE_VOLTS`. `_volts` returns `None` when
  the adapter does not answer, and `or 0` turned that silence into 0 V -- below
  every threshold, including the 13.2 V wake band. A moving vehicle glitches the
  RFCOMM link, so a single transient timeout was enough to stop recording a
  drive that was still happening. Silence is now explicitly not evidence of
  sleep: an `_asleep()` helper ends a session only on a *measured* voltage below
  the band, and a link that is genuinely gone is left to the read errors inside
  `record()`. Found while arming the node for a real commute, before that
  commute rather than after it.
- **An unanswered `ATRV` cost five minutes of a live drive.** The watch loop
  fell straight to `asleep_interval_s` (300 s) whenever the adapter did not
  answer, so one dropped read stopped sampling for the length of a short
  commute. The first `UNANSWERED_RETRIES` (3) silences now retry after
  `UNANSWERED_INTERVAL_S` (5 s) before the slow watch resumes. This path still
  sends nothing but `ATRV`, so the property that makes the unit safe to enable
  at boot against a parked vehicle is unchanged, and the test asserting that a
  dead adapter transmits only `ATRV` still passes.
- **Two documented inventories had drifted from the code.** The README
  advertised a "26-column CSV" against a `drive.COLUMNS` of 29, and
  `config/hummer-drive.default` described "14 enhanced identifiers across three
  modules" against a `GROUPS` of 16 across four (CB, 28, 17 and 1D). Both now
  match, and both point at the code as the place to read the number rather than
  restating one that has already drifted once.

- **The drive recorder sampled at half the configured rate, and the config
  explained why in terms that were wrong.** `record()` calls
  `sleeper(interval_s)` *after* a completed cycle, so `DRIVE_INTERVAL_S` is a
  trailing gap and the sample period is `cycle_time + DRIVE_INTERVAL_S`. With a
  ~4.5 s cycle and the shipped `DRIVE_INTERVAL_S=5`, the measured period was
  9.5 s: more than half of every drive went unsampled. The comment in
  `config/hummer-drive.default` asserted that a value below the cycle time "is a
  request for a backlog rather than a faster sample rate", which cannot happen
  when the sleep is trailing -- nothing queues behind it. The default is now
  `1`, which keeps a courtesy pause for the adapter and nearly doubles
  resolution, and the comment states the actual relationship.

- **A GM enhanced reply was parsed as an incomplete multi-frame message and
  discarded.** `split_can_header` recognised only the legislated `18 DA`/`18 DB`
  29-bit form. This vehicle answers an enhanced read as `14 2A F1 CB ...`, so
  byte 0 was `0x14`, whose high nibble reads as "first frame of a multi-frame
  message"; the parser waited for continuation frames that never arrived and
  dropped the payload. The first real enhanced read from this truck was very
  nearly lost to this, and survived only because the raw transcript is written
  before parsing. The pattern is now widened, guarded by an ISO-TP PCI
  plausibility check so it cannot swallow ordinary payload bytes.
- Seven negative-response codes added to the decoder, including `0x34`
  `authenticationRequired`, which is the one a gatewayed GM module is most
  likely to return for a protected identifier.
- `hummer_obd.enhanced` stored the adapter voltage with the adapter's carriage
  returns and `>` prompt embedded, because `str.strip()` does not remove a
  trailing `>`. The evidence field now carries the value alone.

- **The read-only telemetry node itself.** A Raspberry Pi Zero 2 W talks to an
  OBDLink MX+ over Bluetooth Classic SPP and a persistent RFCOMM binding,
  preserves byte-exact diagnostic responses, decodes a small set of standard
  OBD-II data, buffers it in WAL-mode SQLite, and reports node health on a
  2.13-inch e-paper panel. Ships with Pi provisioning and deployment tooling,
  a hardware-free PTY/ELM simulator, and a test suite that runs under both
  pytest and `python -m unittest`.

- **The safety gate** (`safety.py`). Every byte bound for the serial port
  passes `validate_command()` first. It is an allowlist, so an unrecognised
  command is rejected rather than forwarded, and `FORBIDDEN_SERVICES` is
  checked as a second independent barrier: service 04 (DTC clear), service 08,
  and the UDS write/control/security/reset/routine set can never be
  transmitted. There is no runtime flag that turns it off. Mode 22 is
  *deferred* rather than permitted, because GM/Ultium identifiers are unproven
  on this VIN and this project does not guess them.

- **Exact read-only capability probes**, replacing hopeful command lists with
  requests whose shape is known before they are sent.

- **Services 02 and 06, admitted through change control.** `ALLOWED_OBD_MODES`
  became `{01, 02, 03, 06, 07, 09, 0A}`. Both are standard SAE J1979 *read*
  services from the same specification as the modes already allowed and,
  unlike mode 22, neither requires a vendor identifier to be guessed — which
  is the entire reason mode 22 stays rejected. The five-part record is in
  `docs/SAFETY.md`. Service 02 was given its own request shape rather than
  relaxing the one-parameter rule for every mode that shares it, and a test
  asserts the allowlist and the denylist stay disjoint.

- **Per-ECU capture.** On this vehicle a single `0142` request is answered by
  eight modules, each reporting its own supply voltage. `decode_pid_per_ecu()`
  returns one value per module and `samples.ecu` records which module said
  what. Replayed against the transcript already on the node, the eight
  answers spanned 13.500 V to 13.910 V — a 0.41 V harness drop, and a real
  measurement that the previous first-match-wins path had been discarding.

- **The freeze-frame support bitmap, always requested.** The probe now always
  sends `020000`, which asks what a freeze frame *would* contain rather than
  what one does contain. It exercises request shaping, the frame byte and
  parsing without a stored fault, which matters because this vehicle has none
  and inducing one to exercise a decoder was never an option. On 2026-09-01 it
  returned a positive response advertising `02 0D 1F 20`.

- **The capabilities report** (`hummer-obd-capabilities`). A sanitized offline
  account of what the node can do and what it has proven, split into proven /
  available-but-unproven / not available. It never opens the serial device and
  opens SQLite read-only, so it is safe to run in the middle of a sleep
  observation, and a test asserts that property. Later gained a
  collection-coverage section reporting sessions, observed time, coverage
  ratio and every inter-session gap above a threshold — which immediately
  corrected the record, showing the drive-time gap as 66.9 seconds rather than
  the "about three minutes" that had been quoted from memory.

- **Local export for external ingestion** (`hummer-obd-export`). Writes the
  SQLite buffer out as self-describing JSONL, CSV or JSON that a notebook, a
  spreadsheet or a language model can read without this repository in front of
  it. Read-only on the database, never uploads, masks VIN-shaped tokens and
  network identifiers, and is deterministic: two exports of the same database
  differ only in the timestamp, which `--export-time` pins.

- **Bounded collector trials and their supervision.** `--max-cycles`,
  `--duration-s` and a `--poll-interval-s` override, with sliced waits so a
  deadline and a SIGTERM are both honoured inside a long idle backoff. Then
  `systemd/hummer-collector-trial.service`, which survives an SSH session
  closing, caps a restart loop at five starts per hour, caps each start at
  `RuntimeMaxSec=7200` regardless of configuration, does not restart a clean
  exit, reads a separate trial config so `config/hummer.toml` is never
  touched, and has no `[Install]` section, so it cannot become a boot service
  by accident. `scripts/run_trial.sh` mirrors those guarantees without root,
  for the case where installing a unit over an unreliable link is itself the
  fragile step.

- **A 12 V watch that provably cannot reach the vehicle** (`hummer-obd-voltage`).
  `ATRV` reads connector voltage inside the adapter: no protocol, no ECU, no
  bus arbitration, so the parasitic-drain question can be measured while the
  vehicle sleeps without putting a byte on CAN. The guarantee here is narrower
  than the rest of the project's — not "read-only" but "nothing reaches the
  vehicle at all". `WATCH_COMMANDS` is fixed, every entry is checked at import
  time to be an adapter command, and a test proves that `0100` — a perfectly
  legal read-only request everywhere else — is refused here.

- **A PiSugar2 cell watch** (`python -m hummer_obd.battery`, supervised as
  `hummer-battery.service`). It is deliberately not one of the seven
  `hummer-obd-*` console scripts: it is a supervisor, not an operator command.
  The chip was identified by measurement rather than from the label — the
  IP5209 register pair reads 4.05 V and the IP5312 pair reads 2.60 V, and
  2.60 V is below the voltage at which the Pi could have taken the reading at
  all. `identify_chip()` repeats that check at run time and refuses to answer
  if both profiles look plausible. The module is built around its refusals, because a shutdown that
  fires wrongly strands the node: the threshold is a measured voltage and
  never a modelled percentage, an implausible reading clears the low streak
  instead of extending it, an I2C failure never counts towards a shutdown,
  five consecutive low readings 30 s apart are required, a cell below
  threshold but rising is left alone, and a test asserts that no I2C write
  exists anywhere in the module. No new dependency: a register read is one
  write and one one-byte read over `/dev/i2c-1` through `fcntl` and `os`.

- **Documentation:** `docs/CAPABILITIES.md` (proven / unproven / out of scope)
  and `docs/ENHANCED_PID_VALIDATION.md` (the evidence bar a Mode 22 identifier
  must clear before it is allowed on the wire).

### Changed

- **Standard OBD speed and odometer are asked of one module instead of shouted
  at all of them.** `010D` and `01A6` were broadcast to `DB33F1`, and a
  functional broadcast is answered by whichever module speaks first. Measured
  across a full raw transcript: each was answered 545 times and *every single*
  answer came from module `0x17`, while module `0x28` refused service 01 with
  `7F 01 22` (conditionsNotCorrect) more than 760 times -- faster than module
  17 could answer. The adapter returned the refusal and the real answer never
  arrived. On 2026-09-03 that left speed and odometer in 8 of 79 rows while
  every enhanced read was in all 79, and it made a 12.6-mile drive look like
  0.06 miles until the report learned to prefer denser columns.

  They are now addressed to `DA17F1` with the receive filter pinned to that
  module's own reply address `18DAF117`, so a module that was not asked cannot
  be mistaken for one that was. Module `0x17` is not a new address or a guess:
  it is the `pack_power` module this node already reads pack voltage and
  current from.

- **The probe asks the vehicle what it supports, instead of a fixed generic
  PID list.** The old list overlapped this truck in three places, so eight
  PIDs it advertises had never been requested — including the odometer. The
  probe now reads the vehicle's own support bitmap, and does the same for
  service 09 via `0900`. All fourteen advertised service 01 PIDs answered, the
  odometer decoded at 2146.6 km, and service 09 named all eight modules.
  Decoders added alongside: A6 odometer (4 bytes at 0.1 km/bit), 30 warm-ups,
  1C OBD standard. PID 01 is left undecoded on purpose — it is a composite of
  MIL state and readiness monitors that the scalar sample shape cannot
  represent honestly.

- **Storage schema v3, migrated in place from v1 or v2.** The node holds readings
  nobody can take again, so `migrate()` only ever adds: `ALTER TABLE ADD
  COLUMN` and `CREATE TABLE IF NOT EXISTS`, never a drop, rename or rebuild,
  and it raises rather than opening a version it does not understand. Verified
  against a copy of the reference node's real database before the original was
  touched, then run on the original with a backup taken first: 7384 samples,
  18 DTC reads, 5 sessions, 1 vehicle_info and 5 events all byte-identical on
  their original columns, with `ecu` defaulting to `''` on old rows.

  v3 followed with the same shape and the same guarantee: a `cycles` table plus
  a **nullable** `cycle_id` on `samples`, `monitor_tests` and `dtc_reads` — a
  column alone cannot record a pass that produced zero rows, and a sleeping
  vehicle produces exactly that; an `ecu_modules` current-state index backfilled
  from the append-only `vehicle_info` log, where an empty name never overwrites
  a proven one; and `dtc_ecu_reads`, `monitor_status`, `monitor_readiness` and
  `ecu_info` as child tables rather than widened ones, so no existing row
  changes meaning. The new columns are nullable rather than `NOT NULL DEFAULT
  0`, because `cycle_id = 0` would be a fabricated group id that reads like a
  real one while `NULL` says "recorded before cycles existed", which is true.
  `add_ecu_info` refuses service 09 item 02 outright — that is the VIN, and this
  table is exported. Verified the same way against a copy first: 7440 samples,
  27 DTC reads, 9 sessions, 16 vehicle_info and 5 events identical, version
  2 to 3, `cycle_id` NULL not 0, and the backfill took exactly the eight real
  modules while excluding two malformed rows a transient bug had written.

- **The module map is measured, not inferred.** Each address was queried behind
  its own `ATCRA18DAF1<addr>` receive filter: 17 DMCM, 1D DMC2, 1E DMC3,
  28 BSCM, 40 BCM, 45 Gateway Module - GWM, CB and CD BSM.

- **UAS scaling row `0x24` removed from the service 06 table.** It could not be
  confirmed against SAE J1979, and "a multiplier of 1.0 cannot change a
  magnitude" is circular — it holds only if 1.0 is the right multiplier. An
  unrecognised unit-and-scaling identifier now yields a null scaled value with
  the raw counts kept, which is why the table is deliberately partial. A
  plausible wrong reading is worse than an admitted gap.

- **The battery watch stops the collector instead of halting the node.** A
  PiSugar2 cannot power the Pi back on — the vendor's own position, not an
  inference — a bare `systemctl poweroff` does not tell the chip to cut power,
  and GPIO3 wake needs bootloader EEPROM the Zero 2 W does not have. So every
  halting path strands an unattended node in a vehicle. The default action is
  now `stop-collector`: it ends the polling that is drawing the power and
  holding the serial port, and leaves the OS up and reachable. The watch keeps
  running afterwards, so a recovering cell needs no intervention.
  `--on-low poweroff` remains for a node whose return has been solved, and its
  help says it has not been solved here. This also removed the need for the
  `--dry-run` crutch the watch had briefly shipped with, so the watch is armed.

- **Documents reconciled against what has actually been measured.** The README
  had claimed "pack voltage under load" among the live driving telemetry; what
  is captured is PID 42, the 12 V control-module supply each module sees. HV
  pack voltage is not exposed over standard OBD on this vehicle, which is the
  whole reason the enhanced-PID document exists. Service status was brought
  current everywhere: service 06 is proven and advertises zero monitor IDs,
  which is an answer rather than a failure; service 02's request path is
  proven, while frame contents are not and cannot be until the truck develops
  a fault of its own.

- **Test counts recorded at the capability milestones above**, in order:
  235, 243, 252, 265, 337, 346, 366, 372, 393. The suite is the acceptance
  record
  for a project that cannot re-run its measurements, so it grew with every
  capability rather than after them.

### Fixed

- **The package had never been installed on the node.** All seven
  `hummer-obd-*` console scripts were declared and none existed;
  `import hummer_obd` raised `ModuleNotFoundError`. Every command in the
  runbook, and every command a reviewer would reasonably copy out of it,
  failed with "command not found". `bootstrap_pi.sh` now performs an editable
  install and is safe to re-run.

- **The probe printed per-module readings and then dropped them.** It stored a
  session row and a masked VIN and nothing else, so decoded values survived
  only in the JSON report. It now stores the decoded samples, monitor tests,
  DTC reads and the module map — verified on the node as 23 rows carrying a
  module address across eight distinct modules.

- **The module map was written as two rows holding the `repr` of a list and a
  dict**, rather than one queryable row per module address.

- **The export dropped the `ecu` column.** It predated schema v2, so it
  silently exported per-module readings with no way to say which module
  reported which — turning a distribution back into an unattributed list,
  which is the exact loss that column was added to prevent. Fixed in all three
  formats and described in the meta record.

- **`docs/ENHANCED_PID_VALIDATION.md` had named address 28 as the gateway.** It
  is the brake system controller; 45 is the gateway. The inference came from
  28 being the only module still answering during shutdown, which is true and
  still not the same thing. Addressing the wrong module is precisely the
  failure mode an enhanced-PID request has to avoid, so the correction is kept
  visible rather than quietly swapped.

- **An upload endpoint's embedded credentials could be printed.**
  `_safe_endpoint` existed and was tested, but the configuration section
  reported `cfg.upload.endpoint` verbatim, so `https://user:pass@host/` would
  have been written to the capabilities report and to stdout in full. The
  not-HTTPS refusal had the same hole. Both paths now share
  `config.redacted_endpoint()`. Alongside it: tailnet hostnames are matched
  case-insensitively, since DNS is, and userinfo is split off the authority by
  hand rather than rebuilt from `parts.hostname`/`parts.port`, which
  lower-cases the host and drops the brackets around an IPv6 literal.

- **The reconnect backoff ignored the collector's deadline.** It was a bare
  `time.sleep` of up to 120 s — the third and longest sleep in a cycle — so a
  `--duration-s` trial could overshoot by two minutes and a stop request could
  look ignored for the same window. The wait is now injectable, and the
  collector hands it the same deadline-aware waiter it uses everywhere else.

- **`--max-cycles 0` meant "unlimited"**, so it silently turned a config
  carrying `max_cycles = 20` into an unbounded run on a real vehicle. It was
  the one input where a typo removed a bound instead of being rejected. Now
  refused; omit the flag to use the configured value.

- **`hummer-obd-capabilities --config` without `--root`** reported the working
  directory as the project root and then announced that nothing had been
  recorded on this node — wrong in the most misleading direction, because it
  reads as a finding. The root is now taken from the config's own location.

- **The voltage log was written with CRLF line endings.** The `csv` module
  defaults to `\r\n`; that file is appended to over hours and read back with
  shell tooling while the watch is still running, so the last column parsed as
  `"ok\r"` and a comparison against `"ok"` failed while looking obviously
  correct. `export.py` already pinned `lineterminator="\n"` for the same
  reason.

- **`scripts/deploy.sh` copied one named config template**, so
  `config/hummer-collector-trial.default` never reached the node and
  installing the trial unit failed on a missing file. It now ships every
  template (`*.example.toml` and `*.default`) and still never touches the
  node's live `config/hummer.toml`.

- **`0906` was rendered as ASCII.** CVNs are binary, and the ASCII path
  produced noise that read like a decode failure on a good reply. Routed
  through `decode_cvns`, which returned 42 CVNs as hex.

- **The service 02 bitmap was one byte out.** A service 02 support bitmap
  carries a frame byte before the four bitmap bytes, so
  `_supported_pids_for_mode` gained a `skip` parameter. Reading them shifted
  would have advertised a set of PIDs the vehicle never claimed: plausible, and
  wrong.

  **This fix is currently unguarded, and that is a known gap rather than an
  accepted one.** `supported_freeze_frame_pids()` has no direct test:
  `tests/test_probe_integration.py` asserts only that a `supported_pids` key is
  present in the probe summary, never what it decodes to. Setting `skip=0` — the
  exact regression — still passes the whole suite. The `02 0D 1F 20` recorded in
  `docs/VALIDATION.md` is the decoder's own output rather than an independent
  reading, so it corroborates nothing about the offset. Closing this needs a test
  that decodes a fixed `42 00 <frame> 40 08 00 03` payload and asserts the same
  four bitmap bytes yield the same PID list through service 01 and service 02.

- **systemd argument splitting in the battery unit.** `${VAR}` expands to a
  single argument and `$VAR` to whitespace-separated words. The braced form
  passed `--on-low stop-collector` as one token, argparse rejected it, and the
  unit went into a restart loop. Now unbraced, with the reason recorded in the
  unit so it is not "tidied" back.
