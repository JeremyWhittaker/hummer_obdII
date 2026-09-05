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

- **The wake watch no longer costs the first five minutes of every drive.**
  After a session ended, `run_auto` waited a flat 300 s before looking again,
  so a drive beginning right after the vehicle fell asleep lost up to five
  minutes — including, on 2026-09-04, the motion-onset transition that would
  have settled whether `0x4127` steps at first wheel movement. It happened
  twice, and both times the workaround was restarting the service by hand,
  which needs network access to a node that is off WiFi *precisely because* it
  is moving. That makes it a defect in unattended capture, not an annoyance.

  The 300 s was not arbitrary and is not simply lowered: it bounds how often
  `WAKE_PROBE` reaches a sleeping vehicle, and that matters because the measured
  asleep band is 12.7–12.9 V against a 12.8 V threshold, so a parked truck reads
  "awake" on the rail about half the time and only the probe settles it.
  Instead the watch is now adaptive — `WAKE_WATCH_FAST_S` (20 s) for
  `WAKE_WATCH_WINDOW_S` (10 min) after the vehicle is seen to fall asleep, then
  the old 300 s. A vehicle that just parked is far likelier to move again soon
  than one that has been asleep overnight.

  The whole fast window costs about 30 single-PID probes, two orders of
  magnitude below the 2026-09-04 restart loop's ~100 requests per minute, and a
  test asserts that bound so the window cannot quietly be widened into one. A
  cold start deliberately does **not** watch fast: with no observed sleep there
  is no reason to expect a wake, and the node reboots beside an overnight-parked
  truck more often than a just-switched-off one.

- **A second drive independently confirms two findings and corrects a third.**
  9.10 km, 93 samples, −325.8 to +577.5 A, and nothing in it was fitted to.

  - **Pack internal resistance reproduces**: 18.86 mΩ on this drive alone
    (n = 62, r = −0.9926) against 18.59 mΩ pooled over the earlier corpus —
    1.5 % apart, separate drive, separate roads. `0x4127` and `0x2429` both
    move to **level 3**.
  - **`0x2429`'s zero point holds, and its one exception confirms the reading.**
    All 22 powertrain-down samples read exactly 22534. The three samples at
    speed 0 with the powertrain *up* read +435, −441 and +462 — and they line
    up with the brake: +435 at 100 kPa and +462 at 700 kPa are creep torque
    held at a stop, while −441 sits at 1000 kPa with −0.134 g still
    decelerating. A stopped EV in Drive is not a zero-torque EV. The claim is
    now "zero when the powertrain is **down**", not "whenever stationary" — the
    original was measured entirely on parked and charging samples. Sign
    convention reproduces: 31/32 above +20 A sit above zero, 28/29 below −20 A
    sit below.

### Changed

- **The `soc_pct` charging quantum is confirmed rate-independent.** The 0.400 pp
  figure came entirely from 240 V charges at roughly 8 kW. The 120 V charge ran
  at **0.581 kW, about a fourteenth of the power**, and both of its steps —
  04:45:04 and 06:04:38 — were **+0.400 pp exactly**. A single charge rate could not distinguish
  "the field steps in 0.400 pp" from "the field steps once per fixed energy at
  this rate"; two rates seventeen-fold apart can, and do.

- `0x4149` read `00A0` = 160 for a further **42 samples** across a second session
  file on the same 120 V cordset, with charger state `0x0D` throughout. The
  falsification stands on more data than it was published with.

### Fixed

- **The `soc_pct` step is not "exactly" 0.400 pp, and the exactness was an
  artefact of the decode.** The field is `u16 / 655.35`, so 0.400 pp is
  **262.14 counts** and 0.500 pp is **327.68** — neither an integer, and the
  module reports integers. Charging steps run **+262 counts (64 of 79) and +263
  (14)**; discharging **−328 (24 of 42), −327 (7), −329 (8)**. Decoded, that is
  0.399/0.400/0.401/0.402, and earlier readings called it "exactly 0.400" by
  taking the commonest value for the only one — including a claim published
  hours earlier the same day that two steps were "+0.400 pp exactly".

  The quantum itself is real and the rate-independence result survives: the
  120 V charge's steps are +262 and +263 counts, the same as the 8 kW charges.
  What was wrong was reading a rounded decode instead of the raw counts under
  it.

### Fixed

- **`0x4149` is not EVSE current, and the proof needs no charger at all.** The
  entry already called the `160 ÷ 4 = 40.0 A` match a coincidence, on the
  grounds that 160 predated the charger by 124 minutes. That is now a direct
  falsification instead of an absence of support: **the field reads 388 in 384
  samples taken while the vehicle is moving at 2–143 kph, across 5 distinct
  session files, with the charger state `00` in every one.** Nothing is plugged
  into a vehicle doing 143 kph. Measured on closed, committed sessions only.

- **A second EVSE, recorded provisionally.** A 12 A / 120 V cordset (Yura
  91686-G5020, nameplate read at the vehicle) was connected on 2026-09-05. The
  charger state took **`0x0C`, a value absent from all 7,117 prior rows**, and
  the charge settled at ~0.51 kW into the pack by two agreeing routes. `0x4149`
  read 96 for 29 minutes, then **settled on 160: seventeen answers spanning
  02:59:37 to 03:59:42, every one 160**, including isolated answers at 03:44:47
  and 03:59:23 after a 35-minute gap in which module 40 answered almost nothing.
  A value surviving that discontinuity is not a window artefact. So the field
  reads 160 for a 40.2 A supply and 160 for a 12 A one — and an advertised
  current cannot be equal for sources differing 3.35×. That is a **second
  independent falsification**, reached from entirely different data than the
  moving-vehicle one and agreeing with it.

  Re-derived against the closed 351-row session and unchanged. The charge also
  used charger state `0x0D` (125 samples) as well as `0x0C` (210), and neither
  appears in the `0x93`–`0x99` family that every 240 V charge uses exclusively —
  so the two charge types are distinguishable from the state word alone.
  **Two conclusions were drafted from this charge and withdrawn before
  publication**, both from reading a window that
  ended before the value settled: first that 96 was the 120 V charging value,
  then that the chargers needed divisors of 3.98 and 8.00 and that
  `960 × amps ÷ volts` fitted both. The settled reading of 160 kills all of it.

- **`0x4127`'s powertrain-down family is `{429, 1048}`, not 1048 alone — and
  the error came from reading a session that was still being written.** The
  previous entry said "all 22 samples with no road speed reported read 1048".
  That was measured on 93 rows of a file that became 335, in which 429 had not
  yet appeared. On the complete file, 67 of 69 no-speed samples read 1048 and
  2 read 429.

  Corpus-wide the partition is clean: 429 (9 samples) and 1048 (601) occur
  **only** with no speed reported — 610 of 610 — while 234, 238, 242, 246, 261
  and 601 occur with a speed in 2,373 of 2,374, one sample of 234 excepted.

  Every other figure taken from that partial read survived the complete file
  unchanged — 18.86 mΩ at n=62, 242 across all 68 moving samples, 9.10 km, and
  the `0x2429` sign convention at 31/32 and 28/29, with the zero-point evidence
  strengthening from 22 to 59 powertrain-down samples all at exactly 22534.
  That was luck, not method. A session is read after it closes.

- **`0x4127 = 246` is not "the moving value".** The previous entry said 246
  "appears in no other session and is first seen in the exact poll of first
  wheel motion". The second drive holds **242** across all 68 moving samples.
  The powertrain-up value is a per-session constant from the 234/238/242/246
  family, not a fixed motion marker; the state distinction is between that
  family and 1048. The no-speed rule itself is now cross-validated — 22 of 22
  no-speed samples at 1048, matching 477 of 477 corpus samples.

- **`0x416C` is an HVAC field, not a battery group voltage.** The source's
  label is falsified: across 2,737 paired samples it correlates with measured
  pack voltage at **+0.088**, and their ratio spans 0.00 to 6.93. A per-group
  voltage has to track the pack it belongs to. Its siblings `0x416D` and
  `0x416E` take only **six** distinct values each corpus-wide and are
  effectively constant, which no live voltage is.

  What it does track is HVAC mode, reversibly: 999 through all 99 cold-soak
  samples, 2048–2544 across max A/C, 664–794 across max heat, and 1789–2379 on
  the return to A/C. Cooling drives it up, heating drives it down, with the
  quiescent value between them — the shape of a bidirectional actuator command
  rather than a temperature. The return to the A/C band is what separates it
  from elapsed time. Level 1 to 2; no scaling claimed.

  **Recorded explicitly: 999 is not an HVAC-off signature.** It held for every
  sample of that one cold soak, but corpus-wide it is 119 of 2,829 samples
  (4.2 %), 878 is commoner at 286, and no session takes it as its only value.
  That is the same shape as the `0x4127 = 1048` error published and withdrawn
  earlier the same day — a value that fits one context perfectly because the
  corpus for it was all one context. The mode *response* is established; a
  lookup from value to state is not.

  It has 324 distinct values over 0–2644, so it has the granularity to carry a
  temperature setpoint. Whether it does is **untested**: every HVAC phase so far
  was at maximum, where demand and setpoint are indistinguishable.

- **`hummer-obd-live` now opens with a derived block** -- what the raw columns
  mean, not just whether they answered. Computed live from the session in
  progress: pack V/A/kW, cells in series, SoC with the implied pack capacity,
  **internal resistance fitted from that session's own current steps**,
  distance, efficiency, regen fraction, the `0x2429` torque signal as signed
  counts from its measured zero of 22534, the three 12 V readings, powertrain
  and charging state, and the since-last-charge counters. On the 2026-09-04
  commute it reports 388.75 V / 95.9 cells / 190.6 kWh implied / 18.00 mOhm /
  42.3 kWh/100km, all matching the offline analysis.

  Two defects were found and fixed while building it, and both have tests.
  Pack state read the last row of the session, which is usually the vehicle
  dropping its contactors -- it displayed a **1.06 V pack and 0.3 cells in
  series**, which reads as a broken decoder rather than a sleeping truck; it
  now uses only rows passing the sanity filter. And efficiency charged the
  parked HVAC draw against the drive's distance, turning a real 42 kWh/100km
  into **63**; it is now measured over the moving window, with the
  whole-session figure shown separately rather than silently merged.

  Nothing here invents a unit. The torque signal and the three accumulators
  are shown as raw counts, because their behaviour is established and their
  scaling is not.

- **The commute decoded `0x2429` into a physical quantity, and measured the
  pack's internal resistance for the first time.** A 20.1 km drive swung pack
  current **−275 A to +630 A** with 27 sign reversals — the one input shape
  elapsed time cannot imitate, and the thing every previous decode attempt
  lacked. Analysed across seven dimensions with an adversarial verifier on each.

  - **`0x2429` is a bipolar drive/regen torque signal zero-referenced at 22534
    (`0x5806`).** Not a voltage: in one session the measured pack voltage swings
    0.83 → 377.89 V across 526 samples while this field holds `0x5806` in every
    one. The zero point is constant across 1,083 stationary samples corpus-wide
    and is the only value present in 7 of the 8 sessions carrying it. It
    reverses about that zero with torque direction (25 of 27 zero crossings) and
    tracks electrical power ÷ speed at R² = 0.926. The earlier "~16.4 counts per
    amp" is superseded — that gain falls from 18.2 at 30 kph to 3.9 at 132 kph,
    a 1/speed law, so the pooled figure was an artefact of mixing speeds.
    Supported by a module-28-only check using no pack current at all:
    `longitudinal_g` and speed² predict the field at R² = 0.766 with an implied
    vehicle mass of 3,415 kg. **No equation is shipped** — 2.30–2.42 N per count
    is not a divisor a designer picks, and it only becomes round by assuming an
    unmeasured rolling radius or final drive.
  - **Pack internal resistance: 18.59 ± 0.11 mΩ** (0.194 mΩ/cell at 96S) from
    550 consecutive-step ΔV/ΔI measurements, r = −0.991. The ~2 mΩ spread against
    the level regression decomposes rather than being noise: 19.95 mΩ
    instantaneous plus 2.11 mΩ depending on the previous sample's current, so
    ~18.6 mΩ on a 7 s step and ~22.1 mΩ sustained. Use the step estimator — the
    level regression returns the wrong sign on one session and swings 27 % with
    window placement.
  - **`soc_pct` quantises differently by direction**: exactly 0.500 pp
    discharging, 0.400 pp charging, disjoint corpus-wide over 7,057 rows. It also
    freezes while parked (2.500 kWh drained over 45.2 min for 0.000 pp) and
    catches up under way, so **this drive cannot size the pack** — moving the
    window edges alone gives 152 to 253 kWh. Recorded as a negative result.
  - **Pack capacity from a discharge: 190.5 ± 0.8 kWh**, from the corpus's one
    uninterrupted 19.710 pp discharge, with the corpus-wide pointwise median at
    190.5 (sd 0.89, n=6,961). Cross-validates the established 191.9 kWh and sits
    below the charge-derived 194.0.
  - **The accumulator family is scoped**: `0x27BB`, `0x27B5` and
    `dist_since_chg_mi` all reset in the same poll at charge end — one decrease
    in 3,362 samples — so they are since-last-charge counters. `0x27BB` is not a
    clock (froze 5.18 h across 1,307 charging samples; steps *slower* while
    polling *faster*). `0x27B5` is not distance (32 counts across 311 samples
    with a static odometer) and carries nothing `0x27BB` does not. `0x27BF` is
    regen-specific — it read exactly 77 through 4 h 39 min of charging — with a
    tick threshold but no establishable tick size at 7–9 s polling.

### Fixed

- **`0x4127 = 1048` does not mean charging, and the claim that it did was
  published from a mean.** This project stated that "every one" of those samples
  has negative pack current. That came from a −8.6 A mean in a summary table; the
  script written to verify it crashed with a `TypeError`, and the claim shipped
  without the check being re-run. Re-derived: of 443 samples at 1048 carrying a
  current, 235 are negative, **184 are strictly positive** and 24 are zero, with
  all 184 positives showing `charger_5401_raw = '00'`.

  The rule that holds without exception is better than the one it replaces: of
  477 samples at 1048, **zero carry any `speed_kph` value**. Charging is a subset
  of the no-wheel-speed state, which is why the wrong rule fit the parked corpus.
  The drive gave the disambiguating case — 1048 appears six seconds after the
  last wheel-speed report while parked, unplugged, SoC flat and current positive.

- **`power_kw` was never in conflict with `hv_power_kw`.** It is not a module
  reading: the recorder computes it as the slope of `energy_kwh` over a 60 s
  trailing window, with an inverted sign. Reconstructed 508/508 rows exactly. The
  apparent 6× gap was one hard-throttle instantaneous sample against a
  60-second average — the trailing mean at that row was 42.01 kW against 37.82.
  Corpus-wide r = +0.9725 against the 60 s mean vs +0.3988 against the
  instantaneous value.

- **Driving is the state where everything answers.** Measured corpus-wide as the
  fraction of populated cells by service: while moving, standard service 01 and
  enhanced service 22 are both at **100.0 %** (2,727 and 6,382 cells). While
  charging, service 01 falls to 37.3 % while service 22 holds 99.9 %. So the
  earlier "modules go quiet" was too broad — what happens is that the legislated
  PIDs are refused outside a run state while the enhanced reads are not. A
  sparse charging session is normal, not a fault.

### Changed

- **The 12 V "disagreement" is withdrawn: it is one rail with stable offsets.**
  `volts` (adapter `ATRV`), `module_voltage` (PID `0142`) and `dmc2_v`
  (`0x33E5`) differ by +0.293, +0.759 and +0.454 V with standard deviations of
  0.059, 0.062 and 0.033 V over 1,691–4,909 paired samples, ordered
  `volts` > `module_voltage` > `dmc2_v` in every phase across 12.1–13.8 V. The
  ~6 % figure was `volts` against `dmc2_v` while driving — a fixed 0.76 V offset
  plus two 0.1 V quantisation steps.

  The prior grading of `0x33E5` called the difference **multiplicative**, citing
  "ratios hold to 0.53 % while offsets wander by 23 %" over 358 samples. That
  reasoning is withdrawn on two grounds. It compares the relative spread of a
  quantity whose mean is 0.29 V against one whose mean is 1.02, which favours
  the ratio regardless of the truth; and the ratio's tightness is a
  *consequence* of a stable offset, since `y = x + c` gives `y/x = 1 + c/x` and
  so forces the ratio's sd to about `sd(c)/mean(x)` — which predicts 0.00447
  against an observed 0.00460, agreeing to 2.8 %. Refitting each model by least
  squares and comparing residuals in volts, additive wins every pair: 0.0591 vs
  0.0599, 0.0617 vs 0.0665, 0.0330 vs 0.0412 V. Full linear fits give slopes
  0.9985/0.9893/0.9739 with intercepts of +0.31/+0.90/+0.79 V, where a pure
  scaling needs an intercept near zero.

  Two caveats kept rather than smoothed over: the `0142`-vs-`33E5` ratio is 27 %
  tighter than a pure offset predicts and that pair's slope is furthest from 1,
  so a small real gain difference between those two modules is not excluded; and
  both pairs involving `volts` sit near the 0.0289 V floor its 0.1 V
  quantisation imposes. `0x33E5` stays at level 3 — which reading is correct
  still needs a reference meter — but the difference is an offset between sense
  points, not the ADC gain error previously claimed.

- **PID `0142` is the 12 V instrument; the other two are indicators.** The
  resolution gap is structural, not incidental: `0x33E5` decodes as one byte
  ÷ 10, giving 0.1 V steps and 19 distinct values corpus-wide, while `0142` is
  two bytes ÷ 1000, giving 1 mV steps and 143 distinct values.

  This mattered at once. A first attempt to test whether the offset grows with
  load — the signature of harness IR drop — compared phases differing by ~80 mV
  using `volts`, whose quantum is 100 mV, so the test could not have worked and
  is recorded as under-powered rather than as a falsification. Redone on `0142`
  alone: HVAC mode does **not** move the rail (13.539 / 13.537 / 13.527 V across
  A/C, heat, A/C again, n = 80/44/48, every step inside one pooled sd); driving
  drops it 0.92 V at 6.8× pooled sd; but it is **decoupled from traction load**,
  correlating with pack current at only +0.10 across a 905 A swing, with regen
  and hard-draw samples differing by 0.008 V — a tenth of a pooled sd, measured
  across a sign reversal that elapsed time cannot fake.

- **The HVAC A-B-A ran, and it is the first experiment here that separates
  cause from elapsed time.** Every earlier thermal reading came from a single
  transition, which cannot tell a field responding to the change from a field
  that was going to move anyway. This one returns to the starting condition:
  cold soak, max A/C, max heat, max A/C again, parked, owner operating the
  controls and reporting each switch. It changes four gradings.

  - `0x4127` — retired the name `batt_temp_a_raw` for `field_4127_raw`. It is
    **not a temperature**: it holds one constant value per phase (234 across
    179 consecutive cold-and-A/C samples, exactly 601 across all 44 heat
    samples, then 234 again), steps within one poll of each switch the owner
    threw, and returns to the value it left. Corpus-wide its value 1048 occurs
    in 410 samples of which every one has negative pack current — it is reached
    only while charging. Graded a heat-request state, level 1 to 2.
  - `0x27BB` — proven an **accumulator**, and this is what justifies the
    design. It rose during A/C and would have been published as an A/C
    response; it then climbed straight through the reversal, 0 → 10-60 →
    70-110 → 120-150, monotonically in steps of 10. It integrates. It must
    never be read as a mode indicator. Level 1 to 2.
  - `0x40E5` — the only continuous field that genuinely tracks mode, and it
    does so reversibly: flat at 860 cold, 890→980 under A/C, 1125-1170 under
    heat, back to 980-985 under A/C. Level 1 to 2.
  - `0x40E6` — responds to HVAC on/off but its heat and A/C bands overlap, so
    it carries no mode information. Level 1 to 2.
  - `0x4124` — retired the name `batt_temp_b_raw` for `field_4124_raw`. Reads
    exactly 1000 in all 271 samples across all four phases, leaving it only in
    transients at the switches themselves. Deliberately **kept at level 1**:
    the negative finding is solid, two transients are too thin for a positive
    one.

- **A prediction was recorded before the result and it failed.** *If `0x2709`
  is genuinely A/C compressor temperature, it should rise with A/C and not with
  heat.* It does not discriminate. But GM's Ultium vehicles are marketed with
  heat-pump and waste-heat-recovery systems, and if the compressor runs in heat
  mode too then warming in both modes is what a compressor temperature should
  do. That alternative is **not sourced for this VIN** and is recorded as
  unresolved rather than resolved either way. The narrow thing established is
  that the field cannot say which mode is running.

### Fixed

- **A column rename silently corrupted every session already on disk.**
  Renaming a hex column and updating only the recorder left 25 committed CSVs
  carrying the old header, which was no longer in `analyze._TEXT_COLUMNS` and
  so went to `_number()`. That neither raises nor returns `None` — it strips
  trailing letters and calls `float()`, yielding a plausible wrong number that
  nothing downstream can flag: `"00EA"` → `0.0` where the value is 234,
  `"0259"` → `259.0` where it is 601, and `"03E8"` → `300000000.0` because
  `3E8` is valid scientific notation. `_TEXT_COLUMNS` and
  `_UNPROVEN_ON_CHARGE` now carry both eras of header.

  This is the third time this shape has bitten the project, after
  `cell_extra_raw`, so it is now checked against the **real corpus** rather
  than a fixture: a new test scans every committed session for a column that
  holds hex letters while being read as a number. Verified to fail when the
  aliases are removed. The `0x2429` rename escaped this only because no session
  had ever been written under its old name.

- Corrected two figures published earlier in this run from truncated windows.
  `0x40E6`'s cold soak is 696-808 across 99 samples, not 806-808 from the last
  four minutes; its second A/C phase is 353-524 across 48 samples, not 484-524
  from the first 23. `0x27BB`'s last phase reaches 150, not 140. The separation
  each was cited for still holds; the numbers did not.

- **The cold soak ran, and the thermal fields still do not decode — but the
  negatives are much sharper.** First comparison between two genuinely different
  thermal *states* rather than a monotonic ramp: 72 samples at 95.0 °F against
  248 averaging 102.6 °F during the charge.

  **`batt_temp_a_raw` and `batt_temp_b_raw` are not continuous temperatures.**
  Across the whole corpus they take **five** and **four** distinct values — 234,
  238, 242, 429, 1048 and 0, 418, 910, 1000 — switching between two of them here
  and anti-correlated with each other. A quantity occupying four values across
  two days of driving and charging is a state or an index, whatever its source
  calls it. That is a much stronger statement than the earlier "it did not move
  during a charge", which the same fields then disproved.

  **`coolant_2_raw` moves the wrong way**, falling as the pack warms across 252
  distinct values.

  **`coolant_1_raw` is the only survivor**, and weakly: 0.0591 °C per count
  against a round 1/16 = 0.0625, about 5 % out on 7.6 °F of separation.
  Suggestive, not establishing — this project has been fooled twice already by a
  plausible divisor from too little thermal range.

  What would settle it is a genuinely cold morning. Arizona in September gave
  7.6 °F; 40 °F would make a 5 % discrepancy either vanish or become decisive.
  All six stay at level 1.

- **`0x0046` settled: it is a pack-side temperature, not ambient.** The morning
  after the charge it reads **95.0 F** while the truck's own display shows
  **94 F** — one degree apart, which on its own would argue for an ambient
  sensor. It is not: the same field rose **93.2 → 111.2 F during an overnight
  charge**, and garage ambient cannot move 18 F at 2 a.m. The two agree this
  morning because the pack has equilibrated to the garage overnight. That is a
  **convergence, not an identity**, and the distinction is the finding.

  The catalog had this as "a temperature the vehicle holds, semantics not yet
  confirmed" since it was first read. Confirmed now, and by two readings that
  disagree about what it is until you look at both.

- **The cold-soak reading, finally.** Pack at 95.0 F against the 111.2 F it
  reached charging — **16.2 F of gradient**, the largest this project has had on
  the module-40 fields and the reason they are undecoded. Display readings
  matched throughout: 91 % against `soc_pct` 91.362, and 303 mi against
  `range_mi` 302.59.

- **Two of the same day's fixes proved themselves in production, hours apart.**
  At 10:08Z, after the charge finished and the vehicle settled, the recorder
  logged:

  ```text
  transport failed: 3 consecutive cycles decoded nothing; exiting so the link
                    is re-established
  adapter still silent; reopening the link
  vehicle awake (12.9 V); starting a session
  ```

  The first two lines are the dead-cycle check doing exactly what it was added
  for — the recorder once sat `active (running)` for two hours writing blank
  rows, and this is the mechanism that ends that. It detected nothing decoding,
  exited cleanly, and systemd restarted it into a working link.

  The third line is the wake threshold. **12.9 V is the reading that has been
  wrong twice**, and under the 12.95 V value it carried this morning the vehicle
  would have been classified asleep and this session would not exist. At 12.8 it
  is correctly read as awake. The overlap is real — 12.9 V has been observed
  both asleep and awake — and the resolution stands: the threshold decides when
  to *try*, and the dead-cycle check decides whether the vehicle is really
  there. Both halves fired here, in the right order, unattended.

- **The charge finished, and the pack-sizing guidance published two hours
  earlier was wrong.** Complete dataset: 911 samples over **5.32 hours**,
  69.943 % → 89.955 %, **+38.83 kWh**, mean 7.30 kW at the pack, cell spread
  narrowing 3.7 → **1.5 mV**.

  Implied capacity converges monotonically with the *span* measured: 233.9 kWh
  over 2.8 pp, 208.7 over 5.2, 203.4 over 10.0, 196.3 over 16.4, and
  **194.0 kWh over the full 20.01 pp — 1.1 % from the established 191.9**.

  The bias is an **edge effect**, not the initial stall alone. `soc_pct` steps
  0.400 pp every ~5.4 minutes while `energy_kwh` rises continuously, so at any
  window edge the two are out of step by up to one quantum. On a 2.8 pp span
  that is a seventh of the measurement; on 20 pp it is a fiftieth.

  **So the earlier entry had it backwards.** It said the whole-charge figure was
  "the least trustworthy of the four despite resting on the most data" and
  advised measuring "across a window where state of charge is already moving".
  Sub-windows are *more* contaminated, because each adds two fresh edges. That
  entry has been corrected in place with the reversal marked.

  A per-band table computed the same way gave **~234 kWh** across every 2 pp
  band from 70 to 88 % — contradicting both the whole-charge figure and the
  established one, while being internally consistent across ten bands. Recorded
  as a trap rather than a finding: it looked exactly like a real measurement.

- **The app's charge forecast was good.** It predicted 90 % by 2:10 am local
  (09:10Z) from a 04:16Z reading. The vehicle reached 89.955 % at **08:44Z** —
  **26 minutes early on a 5.2-hour forecast, 8.2 % error**, and conservative.

- ~~**Do not size a pack from a short charge window.**~~ *(superseded above —
  the reasoning was right, the conclusion inverted.)* The
  energy-over-state-of-charge ratio should give capacity. Across three and a
  half hours it drifts badly and only settles late: **222.8 kWh** in the first
  third, 197.9 in the middle, **187.8** in the last, and 200.8 for the whole
  charge.

  The cause is the documented lag. For the first twenty minutes `soc_pct` did
  not move while `energy_kwh` climbed 1.42 kWh, so any window containing that
  stall under-reports the SoC change and **over-reports** capacity — by 16 % in
  the first third. An earlier hour-long window that happened to exclude the
  stall gave 188.3 kWh; the last third gives 187.8. Same answer, about 2 % under
  the 191.9 kWh established by three other routes.

  **The whole-charge figure is the least trustworthy of the four despite resting
  on the most data**, because it is the only one containing the lag. More data
  does not fix a systematic bias, it averages it in.

- **The 0.400 pp state-of-charge quantum confirmed on the full charge.** First
  claimed on thirteen values; it now holds across **33 gaps — mean 0.4001,
  standard deviation 0.00074**, every one between 0.399 and 0.402.

- **Module answer rates collapse during a settled charge, and it is not damage.**
  A post-test audit — after five recorder stop/starts and four adapter protocol
  changes — found the newest rows carrying only 21 of 53 columns, with `pack_v`,
  `pack_a`, the wheel speeds and the EVSE field all empty. That looks exactly
  like a rig broken by a night of interference.

  It is not. Answer rates by period:

  | Period | `CB` | `17` | `28` | `40` | std PIDs |
  |---|---|---|---|---|---|
  | parked and awake | 99 % | 76 % | 76 % | 77 % | 65 % |
  | charging, first 20 min | 100 % | 100 % | 100 % | 100 % | **0 %** |
  | charging, settled | 96 % | **7–27 %** | 7–28 % | 7–26 % | 0 % |

  **The degradation begins at 04:16 — eleven minutes before the first passive
  capture.** Two distinct vehicle behaviours: standard OBD stops entirely when
  charging begins (module `17` serves enhanced identifiers but refuses
  legislated service 01), and the non-battery modules drop to under a third once
  the charge settles, while the battery manager stays at 96 %.

  Worth recording because a sparse session is easy to read as a fault, and a
  night of interfering with the rig is exactly when a coincidence gets mistaken
  for a consequence.

- **Onboard charger efficiency, measured twice: 87.6 % and 91.0 %.** Pack DC
  taken within 90 seconds of each JuiceBox reading — 8.17 kW against 9.319 kW at
  04:16Z, and 8.51 kW against 9.351 kW at 05:24Z. The second is higher for a
  sound reason: the pack voltage rose from 378.46 to 382.37 V, so the same wall
  power delivers more DC power. **No amount of vehicle telemetry produces this
  number**; it exists only because the display was read.

- **`0x4149` held `0x00A0` across all 147 charging samples** while the JuiceBox
  read **40.2 A twice, 65 minutes apart**. 160 / 4 = 40.0, and the divisor fits
  the other corpus values too — `0x0060` → 24.0 A, `0x0064` → 25.0 A, all
  plausible EVSE currents. Strong circumstantial support and **still not a
  decode**: it read the same `0x00A0` while parked and *unplugged*, so it is a
  capability — an advertised pilot current or configured limit — not a
  measurement of current flowing. Stays at level 1. A different EVSE with a
  different rating settles it in one session.

- **State of charge steps by exactly 0.400 pp while charging.** An hour of the
  2026-09-04 charge took `soc_pct` through thirteen values, 69.943 to 74.743,
  and every gap is 0.400: **mean 0.4000, standard deviation 0.0008**, one step
  about every 5.4 minutes. At 8.27 kW that is 0.75 kWh per step, which matches
  the interval.

  Corpus-wide the gaps are *not* all 0.400 — they are 0.2, 0.3 and 0.4 — and all
  are near-multiples of **0.1 pp** (mean residual 1.1 % of a step). So the field
  resolves to a tenth of a percentage point and the vehicle advances it four
  tenths at a time while charging. The earlier "frozen" and then "steps coarsely"
  readings were both windows too short to see the pattern; this is the pattern.

- **An outside measurement is not automatically better evidence.** Across that
  hour the app read 70 % then 75 %, and the vehicle read 69.943 then 74.743.
  Sizing the pack from the same 9.04 kWh:

  | Source | Change | Implied pack |
  |---|---|---|
  | myGMC app, whole percent | 5 pp | 180.8 kWh |
  | `soc_pct`, three decimals | 4.800 pp | **188.3 kWh** |
  | Established by three other routes | | 191.9 kWh |

  The *outside* reading is the worse one, by 4 %, purely because it rounds. This
  project treats outside measurements as what breaks the circle of correlating a
  vehicle's numbers against its own — and they do — but **"outside" and
  "precise" are different properties and the first does not imply the second.**
  Where the vehicle reports more digits than the display, use the vehicle's;
  reserve the display for what the vehicle never states at all — wall current,
  ambient temperature, the charger's own kilowatts.

  The app's other readings remain excellent: 249 mi against a recorded 249.16,
  and 233 against 233.0 an hour earlier.

- **Single-wire CAN on pin 1: researched, and declined.** With every framing on
  pins 6 and 14 silent, the obvious thought was that we had been listening to
  the wrong wire — GM historically carried locks, lights and remote fob on GMLAN
  SW-CAN at 33.3 kbit/s on J1962 pin 1, and the MX+ supports it.

  The vendor documentation is encouraging. `STP 61`–`64` select it; `STP`
  explicitly *"does not actually open the communication channel"*; opening
  SW-CAN sets the transceiver to **Normal**, not the High Voltage Wakeup mode,
  which needs a separate `STCSWM 2` this project would never send; and the
  STN21XX datasheet describes omitting the high-speed load circuit for
  *"'flight recorder' type (monitoring only) applications"*.

  **Declined anyway, for three reasons.** The best source found says Global B —
  which covers this truck — does not carry LS-GMLAN on pin 1 at all, but a
  secondary CAN-FD network. That makes pointing a 33.3 kbit/s single-wire
  receiver at it precisely the unidentified-bus case
  `docs/CAN_FD_EXPANSION.md` exists to forbid, and a positive reason to expect a
  mismatch is worse than no information. And the safety conclusion is a *sound
  derivation* from documented defaults rather than a vendor statement — no
  sentence anywhere says "monitoring SW-CAN transmits nothing".

  That last one decides it. `hummer-obd-passive` promises nothing reaches the
  vehicle, and that promise is currently backed by a manifest of sixty-five
  bytes of adapter configuration reconstructed from the transcript. **A promise
  backed by an inference is a weaker thing wearing the same words.** Recorded in
  `CAN_FD_EXPANSION.md` and as an `UNREACHABLE` entry, with what would change
  it: GM service information for this VIN saying what pin 1 actually carries.

- **The passive negative covered one protocol, not all of them.** Every capture
  before 2026-09-04 used `ATSP7` — 29-bit, 500 kbit/s — which is what this
  vehicle answers *diagnostics* on. Body traffic had no reason to share that
  framing, and a frame at 11-bit or 250 kbit/s would have been invisible to all
  of it. The conclusion had been generalised from a single protocol.

  Four 45-second windows with the owner pressing unlock continuously:
  `ATSP7` **0 bytes**, `ATSP6` (11-bit/500k) **0 bytes**, `ATSP8` (11-bit/250k)
  **0 bytes**. `T:00 R:00` after every one. **Three framings silent instead of
  one** — a wider negative, obtained in three minutes.

  `ATSP9` (29-bit/250k) **did not run** on the first pass and was recorded as
  untested rather than silent. It failed on `ATZ`, before any protocol was
  selected, with a Bluetooth RFCOMM error between one capture closing the port
  and the next opening it. The tool logged the empty partial, recorded the
  failure and exited rather than reporting a capture that never started. **A run
  that did not happen is not evidence** — the same rule that keeps `NO DATA`
  distinct from a formed refusal, applied to this project's own tooling.

  **It was re-run rather than assumed**, with the owner still pressing, and came
  back **0 bytes** in 45.1 s with a retry built in so a second glitch could not
  be mistaken for a result. **All four framings on pins 6 and 14 are now
  measured silent** — 11- and 29-bit, at 250k and 500k. Nothing unsolicited
  crosses the gateway to this connector on any framing it speaks. That closes
  the question as far as this pair of wires can take it.

  What none of it touches: **single-wire CAN on J1962 pin 1**. GM historically
  carried locks, lights and remote fob on GMLAN SW-CAN at 33.3 kbit/s, a
  physically different conductor from the high-speed pair every capture here has
  listened to. Whether the adapter can monitor it *without transmitting* decides
  whether it is tried — SW-CAN has a high-voltage wakeup mode, and this tool's
  only promise is that it puts nothing on the wire.

- **The connector is silent while charging.** Four receive-only captures — about
  four minutes across parked, awake and **charging at 8 kW** — returned nothing
  from the vehicle. `T:00 R:00` before and after every one. Charging is a busier
  state than the parked-and-awake baseline and the earlier capture had not
  covered it, so this genuinely extends the negative.

  **That entry originally overclaimed** — see the correction under *Fixed*.

  **The experiment was then run properly, and the answer is the same.** A
  60-second receive-only capture at `04:46:19Z` while the owner stood at the
  vehicle and performed **unlock x5, lock x5**: **0 bytes from the vehicle**,
  `T:00 R:00` either side. The confirmation was recorded *after* the fact as an
  appended mark, and the capture carried the label "REQUESTED, owner
  confirmation pending" until it arrived — which is the whole correction from
  the failed attempt.

  **The passive question is closed.** The fob messages exist; the doors locked.
  They do not cross the gateway to pins 6 and 14. Everything this project will
  ever obtain from this vehicle must be asked for.

  Stated limit: nothing timestamps the presses independently. The window ran
  `04:46:19`–`04:47:19` and the owner reported finishing around `04:47:20`, so
  they overlap by his account rather than by measurement.

- **The thermal-limiting hypothesis was falsified within the hour, by the same
  charge that suggested it.** Over the charge's first four minutes, power
  correlated with `temp_f` at **+0.72** while both moved monotonically — the
  textbook look of a pack warming and the vehicle backing off. It was explicitly
  *not* claimed at the time, on the grounds that a monotonic ramp cannot
  separate "tracks temperature" from "tracks time since plug-in".

  It could not. The charge then **recovered**: power went 8.02 → 2.25 → 8.39 kW
  while temperature rose to 111.2 °F and fell back to 105.8. Over the full 101
  samples the correlation is **−0.028**, and the decisive detail is that the
  **hardest charge (−8.39 kW) and the slowest (−2.25 kW) occurred at exactly the
  same 107.6 °F**. Temperature does not explain the power. What does is not
  established — the EVSE, the grid, or something the vehicle is doing.

  This is the second time in one day that a convincing short-window correlation
  evaporated on more data, after `0x2429`. The confidence entries for the three
  moving thermal candidates now carry the demonstration rather than just the
  warning.

- **How a charge moves state of charge, range and energy — all three
  differently.** Across 101 samples the pack gained **1.42 kWh** and:

  | Field | Behaviour |
  |---|---|
  | `energy_kwh` | rises continuously, 132.66 → 134.08 |
  | `range_mi` | steps — **three** distinct values |
  | `soc_pct` | held one value for 101 samples, then **stepped** to 70.343 |

  State of charge did not move by a single count at 1/655.35 % resolution while
  the pack took on 0.74 % of its capacity — and then moved all at once. The step
  also **under-reports**: 0.400 pp is 0.77 kWh against the 1.58 kWh actually
  taken on in that interval, so it lags as well as steps.

  Not a decode fault: the myGMC app showed **70 %** while the recorded value read
  69.943, so the vehicle is holding it. **Operationally: during a charge use
  energy, not state of charge** — it is a lagging step function.

  *Corrected within the hour.* The first look called it "frozen", which twenty
  more minutes disproved. The observation window was 101 samples and every one
  of them was inside a single step; that is exactly long enough to mistake a
  coarse step function for a dead field, and it is the same mistake shape as the
  thermal correlation above — a window too short to contain the behaviour that
  matters.

- **A second outside measurement, and the decode chain checks out against it.**
  The app read **233 mi** against a recorded `range_mi` of **233.0** — exact —
  and 70 % against 69.943 %. Its predicted finish (90 % by 2:10 am, five hours
  out) implies a mean **7.67 kW**; measured power at that moment was
  **8.27 kW**, and the implied full pack of **191.7 kWh** matches the 191.9 kWh
  this project established by three other routes. This validates the *decoding*,
  not the sensors — the app is served from the same vehicle data.

- **The first charge separated the six thermal candidates into two groups, and
  the interesting group is the one that did nothing.** Across 52 charging
  samples the pack warmed **16.2 °F**. Three fields the source calls
  temperatures **did not move at all**:

  | Identifier | Source's label | What it did |
  |---|---|---|
  | `0x434F` | HV battery temperature | flat at `0x46` |
  | `0x4127` | battery temperature A | flat at `0x0418` |
  | `0x4124` | battery temperature B | flat at `0x0000` |
  | `0x40E5` | battery coolant temperature 1 | moved, 35 distinct values |
  | `0x40E6` | battery coolant temperature 2 | moved, 36 distinct, **disjoint from all 566 parked samples** |
  | `0x2709` | A/C compressor temperature | moved, 14 distinct |

  A field that holds still while the thing it allegedly measures moves 16 °F is
  not measuring it — this project's own rule, turned on its own candidates.
  `0x4124` reading `0x0000` throughout is not a temperature under any scaling.
  That is evidence against three source labels, and it is not proof: a slow
  update cadence or a different thermal zone would look identical over ten
  minutes.

  **No scaling is claimed for the three that did move**, and the reason is worth
  recording because the fits looked tempting. Least-squares against `temp_f`
  gives **1/17.2, 1/5.7 and 1/1.3 °C per count** — no designer picks those.
  Over a monotonic ramp any two rising quantities fit a line, so a believable
  slope means a round divisor that holds across a *second* charge warming at a
  different rate. All three stay at level 1.

- **`0x5401` positively identified: it is a charging *state*, not a quantity.**
  The 2026-09-04 charge gave a clean boundary: `0x00` across **566 consecutive
  parked-and-unplugged samples**, `0x93`/`0x96` across **all 22 taken while
  charging** — completely disjoint, cross-checked against pack current being
  negative. Level 1 → 2.

  The discredited part stays discredited. The published two-byte `/4350`
  charger-power scaling is still wrong here — single byte, plateaus across a
  ninefold power range — and is still not applied; the recorder stores the byte
  raw. Why it alternates between `0x93` and `0x96` while charging is unknown,
  which is most of why this is 2 and not 3.

  **A test written that morning blocked this change**, having pinned the
  identifier at level 1 on the reasoning that "a source disagreeing with the
  vehicle is exactly when a level must not creep upward". The guard did its job
  — it stopped the change and forced the question — but it pinned the wrong
  thing. New evidence about a *different* claim is not a rehabilitation of the
  discredited scaling. The test now pins the durable invariant instead: the
  `/4350` equation is never applied, the decoder returns hex rather than a
  number, and no `charger_kw` column exists. A level may follow evidence; a
  disproven scaling may not come back.

- **What the first charge measured, beyond that.** `energy_kwh` tracks it
  properly: **+0.310 kWh over five minutes**, against a ~4 kW mean — the slope
  route works. But **`soc_pct` and `range_mi` are frozen**: one distinct value
  each across the entire charge, `69.943 %` and `231.76 mi`, at a resolution
  (1/655.35 %) that would easily show the 0.16 % gained. For charge monitoring,
  use energy; state of charge does not update on this timescale.

  The charge also **tapered steeply while the pack warmed** — 8.02 kW down to
  2.93 kW as `temp_f` rose 93.2 → 104.0 °F and both coolant fields climbed
  monotonically. That is the signature of thermal limiting and it is *not*
  claimed as a finding: over four minutes of monotonic ramp everything
  correlates with everything, and this cannot separate "tracks temperature" from
  "tracks time since plug-in".

- **The first outside measurement in this project's history.** Every number here
  has come from the vehicle describing itself. On 2026-09-04 at 03:56Z the truck
  was plugged in and the charger's own display was read: **40.2 A, 9319 W AC**,
  implying 231.8 V. That is the first `label_source: observed-at-vehicle`
  sidecar; the other 23 are all `inferred-from-telemetry`.

  It buys two things immediately.

  **The onboard charger's efficiency, measured for the first time.** Pack DC was
  377.51 V x -21.25 A = 8.022 kW against 9.319 kW at the wall — **86.1 %**. No
  combination of vehicle-reported values could have produced that number.

  **A decode candidate for `0x4149`.** It read `0x00A0` = 160, and 160 / 4 =
  **40.0 A** against the charger's 40.2. The divisor holds across the other
  values already in the corpus: `0x0060` -> 24.0 A, `0x0064` -> 25.0 A. Those are
  plausible EVSE currents rather than arbitrary numbers.

  **The disconfirming evidence, recorded with it rather than after it.**
  `evse_current_raw` also read `0x00A0` while parked and *unplugged*, so this is
  not a measurement of current flowing — more likely the advertised pilot current
  or a configured limit that the vehicle retains. One matching sample is not a
  decode, and `0x2429` was decoded on one convincing sample earlier the same day
  and was wrong. It stays at level 1. What would settle it is a different EVSE
  with a different rating, or the current changing mid-session with the field
  following.

  Separately, `charger_5401_raw` went from `00` to `93`/`96` on the charge
  starting, which supports this project's own earlier finding that `0x5401` is a
  charging-state signal rather than the "charger DC power" its source claimed.

- **`hummer-obd-respond --since`.** Every mark is a segment boundary, so the
  handful written while testing the tooling would slice a real experiment into
  noise. The fix is not to edit an append-only file — it is to say where the
  experiment started.

- **`docs/GM_SERVICE_INFORMATION.md` and `docs/OEM_DIAGNOSTIC_WORKFLOW.md`** —
  the two hardware-prerequisite documents. The first is what to retrieve
  privately against the VIN *before* an internal-bus tap can even be evaluated,
  and states plainly that it is not authorisation to tap anything. The second
  treats GM's own GDS2 as a truth oracle for the seventeen identifiers that
  answer here but are stored raw — potentially the fastest route left, since
  sourcing has run dry.

  The second answers the awkward part with a measurement instead of a shrug. The
  J1962 connector has one socket and both tools want it, so alternating sessions
  only works for quantities that hold still across a swap — and which ones those
  are is a measured question: `0x4149` holds **eight distinct values across 1570
  samples** and `0x2709` **thirteen across 1155**, so both survive a swap taking
  minutes, while `pack_a` is hopeless under alternation and needs nothing from
  the exercise anyway.

  Both cite real NHTSA-hosted GM bulletins, including **24-NA-015**, which
  covers GDS2 charging-data displays for the HUMMER EV Pickup 2022–2025
  specifically. Subscription pricing is recorded as **not established**:
  `acdelcotds.com` returns HTTP 403 to automated fetch and the third-party
  figures found were mutually inconsistent and visibly stale.

- **`hummer-obd-experiment mark` and `hummer-obd-respond`: do something to the
  vehicle and find out which field noticed.** Seventeen recorded columns are raw
  payload bytes whose meaning is unclaimed, and correlating them against the
  truck's *other* numbers can only show that two of its outputs move together.
  What identifies a field is an **outside intervention**: switch the climate
  system on, plug in, open a door, and see what moves.

  `mark` is time-keyed rather than session-keyed, deliberately — an operator at
  the vehicle does not know which CSV is being written, may cross a session
  boundary mid-experiment, and should not have to care. Append-only, for the
  same reason the raw log is.

  **Both of the tool's metrics were wrong on its first run**, and both were
  caught because the validation case had a known answer — a drive, where
  `speed_kph` and the wheel speeds obviously must respond:

  * The numeric metric divided by the *larger* of the two spreads, which
    suppresses exactly the response that matters most: a field that sat at a
    constant zero and then started swinging has a huge spread afterwards.
    `speed_kph` did not appear in a report about a drive. Now pooled, with a
    separate variance term so a field that goes flat-to-variable with no mean
    shift still registers.
  * The text metric asked "are there new values", which is useless for a payload
    like `array_2b43` carrying 779 distinct values across the corpus: any two
    windows are nearly disjoint, so **every** raw column claimed a response and
    buried the real ones. Now it asks whether a field is *stable within* each
    segment and *different between* them — a column holding one value throughout
    and then another is a strong response; one churning through fifty in each is
    telling you nothing about your intervention.

  It refuses to report a difference when either side has fewer than eight
  samples, because at an eight-second cycle that is under two minutes and a
  difference from five samples is a coincidence with a decimal point. And it
  states in its own output that what it found is **association in time and
  nothing more**: a field that moves when the climate system starts may be
  measuring compressor current, cabin temperature, the pack heater reacting or
  the 12 V load. Separating it from everything that did *not* move is progress;
  it is not identification.

- **`hummer-obd-experiment`: what a person observed, recorded beside the
  session.** Every number this project holds comes from the vehicle, and that is
  a problem for the fields it cannot decode: correlating the truck's numbers
  against the truck's other numbers can only ever show that two of its outputs
  move together. `0x4149` is an EVSE-current candidate read while parked and
  **unplugged**; the module-40 thermal fields cover 9.0 °F of a 23.4 °F corpus.
  What breaks that circle is an outside measurement — a thermometer, a charger's
  own display, the dashboard — and those are exactly the things a person reads
  and then forgets.

  It is a sidecar, `evidence/experiments/<session>.json`, never a change to the
  CSV: the CSV is what the vehicle said and must stay exactly that, while the
  sidecar is what a person claims, and a reader is entitled to weigh them
  differently.

  **The load-bearing field is `label_source`.** A label derived from the session
  data does not break the circle — "charge_state was charging because pack
  current was negative" adds nothing the analysis did not have — and recording
  it as though it did would launder an inference into evidence. So the schema
  requires the distinction and **refuses to let an inferred label carry an
  outside measurement at all**. It has no default, because a default would be
  answered by whichever value someone picked and the distinction would quietly
  stop being made.

  23 sidecars were generated for the existing corpus and **every one is marked
  `inferred-from-telemetry`**, because nobody was standing at the vehicle with a
  thermometer. That is the honest state and it is what makes the gap visible.

  Other deliberate choices: unknown field names are an error rather than being
  dropped, because silently discarding a hand-written observation is the worst
  possible outcome — it was made, written down, and thrown away. `parked-awake`
  and `asleep` are separate states because free text lets them blur.
  `plugged-idle` is its own state because an EVSE reading taken plugged-in-but-
  idle is a different observation from one taken unplugged. And a charge rate
  recorded against `unplugged` is refused as the transcription error it is.

  The required-field list is derived from the dataclass rather than hand-kept —
  it had already drifted once during this change, so a sidecar missing
  `label_source` raised a `TypeError` three frames down instead of naming the
  field.

- **`hummer-obd-passive-diff`: compare two passive captures and say what
  differs, if anything.** One capture answers *is anything being said at this
  connector*. Two answer the question worth asking: *does it change when
  something happens to the vehicle?* Take a baseline with the truck awake and
  quiet, then one capture per physical event — a door, a fob lock, the climate
  system, a charge starting — and compare identifier counts, payload sets and
  which byte positions moved.

  **It expects to find nothing, and says so as a first-class result.** The
  2026-09-04 baseline returned zero bytes, so the honest default outcome is
  "there is nothing to compare" — and reporting that plainly beats a page of
  zeroes. The tool exists so that the day something does arrive, the comparison
  is already written rather than improvised.

  Three deliberate refusals: it never opens the serial device (a tool that both
  captures and compares is one that gets pointed at a vehicle to "just re-run the
  baseline"); it never suggests replaying anything, because a frame that changes
  when the doors lock is a **lead, not a command** — modern CAN security can
  include freshness counters, sequence numbers and message authentication, so a
  byte pattern is not an instruction; and it states in its own output that frame
  counts are not bus load, because ASCII over Bluetooth at 115200 caps at a few
  hundred frames per second and an absent identifier may simply have been
  dropped. **Absence is weak evidence; presence is strong.**

  Running it against the real 2026-09-04 transcript on its first execution found
  two bugs in itself. `monitor.py` logs the adapter's reply to the stop character
  *before* it writes `capture_end`, so "every received byte between
  `capture_start` and `capture_end`" swept in ten bytes of adapter prompt and
  made a genuinely empty capture read as non-empty. Selecting on the note the
  stream writer actually uses is precise where a window is not; bytes waiting
  before the capture and bytes drained after it are now counted separately, and
  a test pins each.

- **`ROADMAP.md` rewritten against measured state.** It was three days old and
  still listed Mode 22 enhanced PIDs and passive CAN monitoring as *blocked* —
  31 of 35 identifiers now answer and the passive experiment has been run. It
  now carries the six-goal plan as complete, an item-by-item status for the
  larger external expansion proposal (A–J), and a *Blocked on physical
  conditions* table where each row names the specific event that would release
  it: a labelled charge, a cold morning, a DC fast charge, a fault occurring,
  someone at the vehicle to cause an event, a GM Service Information
  subscription.

- **`docs/ACCESS_MATRIX.md` and `hummer-obd-access`: one page that says what
  this node can and cannot reach, and how to check every line of it.** Twenty
  documents describe this project and not one answered the question a new reader
  or a new agent actually arrives with. Each is organised by *how something was
  discovered* -- a probe on a date, a research note, a sourcing sweep -- which is
  the right shape for preserving an argument and the wrong shape for looking
  something up. The answer to "can we read pack current" was spread across four
  files, and two of them said no.

  Most of the page is **generated from the code that enforces it**, following
  the pattern `registry.py` and `confidence.py` already established:

  * **What may be transmitted** is a matrix of 45 representative commands
    against **all five gates** -- there are five, not four, and which gate a
    caller was built with *is* the safety model. Every cell is produced by
    putting that command to that gate and recording the answer. Nothing is
    asserted. A test requires every one of the 22 forbidden services to have a
    row, so the matrix cannot silently omit one.
  * **What is collected** is all 53 columns with module, identifier, CAN
    priority and confidence level, composed from `live.column_sources()` and
    `confidence.CONFIDENCE` rather than restated.
  * **What cannot be reached** is 27 entries, hand-written because it needs
    judgement -- but written as *data*, with a reason drawn from five distinct
    categories that are routinely conflated: **forbidden** / **unsourced** /
    **hardware** / **measured** / **scope**. Each says concretely what would
    change it.

  The point of that last table being data is a test: no entry may name a column
  the recorder actually writes. `docs/CAPABILITIES.md` claimed pack voltage was
  unavailable while `pack_v` was in `drive.COLUMNS`, two hundred lines above its
  own correction. A sentence cannot notice that; a table checked against the
  recorder can.

  Rather than ban an unreachable entry from citing an identifier the vehicle
  *does* answer, the test requires it to fill in a `despite` field. "We record
  `0x2AF1`'s twenty-four values and cannot say what they mean" is the honest and
  useful sentence, and a rule that forbade it would push authors toward vaguer
  prose rather than clearer.

- **Three defects the new tests found on their first run.** A gate probe that
  *is* a carriage-return-separated command batch -- a real injection the gate
  must refuse -- split its own markdown table row in half when written raw, and
  made the document fail its own idempotency check. Same class as the unescaped
  pipe this project shipped once before; control characters are now rendered as
  their source escapes. The "never touches the vehicle" check, copied from
  `test_decode_fields.py`, false-positived on the word `SerialTransport`
  appearing in a *docstring*; it now reads the import graph with `ast`, which is
  the thing actually being asserted, and a companion test points it at
  `collector.py` to prove it still fires. And asserting that every `validate_*`
  callable appears as a matrix column immediately surfaced a sixth one,
  `validate_all`, which is a batch wrapper rather than a policy -- excluded
  explicitly and then given its own test, because a permissive `validate_all`
  would be invisible in the matrix and reachable from the probe.

- **The 12 V disagreement's own discriminator was run, and it came back against
  the standing explanation.** `PACK_ARCHITECTURE.md` proposed that `ATRV`,
  `0142` and `0x33E5` differ because they are one measurement at each of three
  points on a harness rather than three measurements of one point -- and named
  its own test: an IR drop must **widen under load**. Adding `0142` to the
  recorder made 358 three-way paired samples available spanning **-68.9 to
  +317.8 kW**, so the test could be run.

  It does not widen. The gap is 0.02 V larger above 30 kW than below 5 -- a
  fifth of its own standard deviation -- and its correlation with traction power
  is `-0.065`, not merely weak but the wrong sign. Meanwhile the *ratio* between
  any two readings is about forty times more stable than the *difference*,
  relative to each one's size: 0.53 % against 23 %. A least-squares fit agrees:
  slopes 0.9763 and 0.9485 with intercepts of +0.015 and -0.093 V, where a
  resistive drop would give slope 1 and intercepts near -0.29 and -0.76.

  So the evidence moved back toward the scaling explanation that section had set
  out to undermine. Two things stop it closing: traction power is a poor proxy
  for 12 V current -- the DC-DC follows lights and blowers, not the inverter,
  and nothing here measures 12 V current at all -- and the fit rests on 0.90 V
  of span, so separating slope from intercept extrapolates twelve volts past the
  data.

  What is established is narrower and still worth having: **three uncalibrated
  ADCs differing multiplicatively by 2.4 % and 5.9 %, with ratios stable to half
  a percent across every state recorded.** Which is closest to the truth is not
  answerable from inside the vehicle; it needs a reference meter on the
  connector. That is a smaller question than the one that section opened with,
  and it has a definite answer waiting behind one measurement.

- **The motor telemetry the plan called "the real gap" does not exist
  publicly.** Two independent sweeps across every source where such a thing
  would live -- eight OBDb vehicle repositories, meatpiHQ/wican-fw's profiles,
  issues and pull requests, commaai/opendbc -- found **no identifier, on any
  platform, for motor RPM, motor torque, inverter temperature, stator
  temperature, motor or inverter coolant temperature, propulsion or regen power
  limit, or motor phase current.** Nothing was added. Written up in
  `docs/SOURCING_2026-09-04.md`, because a negative that took two sweeps is
  worth exactly as much as a positive and is easier to lose.

  Three near-misses, each rejected for a stated reason. The Bolt EV signalset
  carries **96 sequential per-cell voltage identifiers**, merged, byte-exact --
  the single thing this project most wants -- and they are 11-bit `7E7`/`7EF`
  headers on the 2017-2023 Bolt: a different addressing scheme and a different
  battery architecture. The Bolt charger block was **tried on a real 2025 LYRIQ,
  one platform generation closer than the Bolt, and returned `NRC 0x31` in every
  vehicle mode**, independently confirmed by that repository's own CI probe --
  sourced *and measured negative* on the nearest platform, which is a stronger
  reason to decline than this project usually has. The Honda Prologue is BEV3
  and GM-built and names 168 per-cell voltages and six module temperatures at
  `0x2028`/`0x202C` -- with no formula anywhere, on Honda's own address map.

- **A public source disagreement about `0x33E5` that this vehicle already
  settles.** Two unmerged LYRIQ pull requests by the same author, two days
  apart, read that identifier mutually exclusively: PR #13 calls it HV pack
  voltage at `x 2.7` (the byte `0x84` becomes 356.4 V), PR #14 is an explicit
  correction calling it the 12 V rail at `/10` (the same byte becomes 13.2 V).
  Both are still open, so a reader arriving there today finds two claims
  differing by a factor of 27 and no merged answer.

  This project uses `/10`, and modules `17`, `1D` and `1E` each answer 13.2 /
  13.1 / 13.1 V in the same minute the pack reads 382.65 V from `0x2885`. Being
  on the right side of an unresolved public dispute is worth recording. It does
  not tighten the divisor beyond the 6 % this project already has open against
  `ATRV`, and `0x33E5` stays at confidence level 3.

- **`0x2429` answered, and it is 96 nominal cells.** Sent for the first time
  since being allowlisted -- see the profile note above -- it returned `0x5806`
  = 22534. The source's `/64` gives **352.09 V**, which across the 96 cells this
  pack was independently shown to have in series is **3.6676 V per cell**: the
  textbook nominal for an NMC cell, a figure nothing here fitted. It is captured
  every cycle as `nominal_pack_v` and graded **level 2**, not 3: that is a
  structural corroboration of the divisor, and what would settle it is the thing
  a nominal must do, which is hold still while the pack does not. One reading in
  one state cannot show that.

- **The ninth thing module 17 advertises is now collected too.** The per-module
  census had module `17` advertise nine service 01 PIDs; seven were wired in
  when that was found, and `01` was left out because it is the awkward one --
  four bytes of packed flags rather than a scalar, so `decode_pid` correctly
  reports it as undecoded and there is no single value for the standard-PID
  loop to put in a column.

  Being the awkward shape is not a reason to leave a legislated, advertised
  signal on the table. `decode.decode_monitor_status` already unpacked it
  properly and nothing called it from the recorder. Two of its fields now have
  columns: `mil_on` and `dtc_count`. **A malfunction lamp coming on *during* a
  drive, with the distance and speed either side of it, is exactly what a
  recorder catches and a later scan cannot reconstruct** — a scan tells you a
  fault exists, not what the vehicle was doing when it appeared.

  The eleven readiness bits deliberately do not become columns: booleans that
  change across months, written every eight seconds, are the wrong shape for
  this file, and `hummer-obd-probe` reports them when asked. A short frame
  leaves both columns empty rather than writing `0`, because two of the four
  bytes would decode into confident-looking flags about bytes that never
  arrived — the one failure here that cannot be spotted afterwards from the row.

  Only module `17` can answer: `STANDARD_ADDRESS` points at it physically, so
  this is that module's view of the lamp and not a vehicle-wide one. That is
  the only view a physically addressed request can give, and it is worth saying
  rather than implying otherwise.

- **`hummer_obd.confidence`: how much each identifier has actually been
  proven, as a table something can check.** The safety gate answers *may this be
  transmitted?* It cannot answer the question a reader of the telemetry actually
  has, which is *how much should I believe this number?* That answer lived in
  prose, in three documents, maintained by hand -- which is precisely how the
  identifier registry fell thirty-six commits behind the code. `registry.py` was
  written to stop that; this is the same fix one layer up.

  Five levels: **0** sourced only, **1** answers here with no meaning claimed,
  **2** decoded with a stated scaling and nothing independent behind it, **3**
  cross-validated against a second route, **4** cross-validated *and* re-derived
  in more than one vehicle state. **Production telemetry starts at 3.** Level 2
  is the dangerous one -- a plausible number with a confident-looking unit and
  nothing behind it -- and the project has already published one: `0x5401`,
  "charger DC power", which this vehicle contradicted. It is level 1 and a test
  pins it there.

  `ENHANCED_READ_DIDS` keeps its `dict[str, str]` type, so `enhanced.py`,
  `registry.py` and their tests are untouched. Key parity is asserted at import
  *and* by a test, so an identifier cannot exist in one table and not the other.
  The generated registry now carries level, answering modules and observed
  states, and the tier prose in `GM_ENHANCED_CANDIDATES.md` is demoted to
  historical narrative that says so.

  Of 35 identifiers: five at level 4, four at 3, five at 2, sixteen at 1, five at
  0.

- **Two level-3 claims that did not exist before, both measured from the
  committed corpus.** `0x4A7A` wheel speed is now cross-validated against
  legislated PID `010D` -- recorded in the same row, from a different module:
  **r=+0.997 on each of the four corners over 670 moving samples spanning
  1-130 km/h**, mean difference within 0.1 km/h of zero. A vendor scaling from an
  unmerged BEV3 source, confirmed by the standard's own measurement.

  `0x4C30` longitudinal acceleration is cross-validated against the derivative
  of that same PID: r=+0.837 over 1683 samples, and -- the part that carries it
  -- the magnitudes match, -2.71..+2.60 m/s2 from the speed derivative against
  -3.00..+3.19 m/s2 from the field. The correlation is not higher because the
  two are read seconds apart and a derivative over an eight-second cycle is a
  smoothed accelerometer; that is a sampling limit, not a disagreement.

  Both are asserted by `tests/test_confidence.py`, which **recomputes them from
  the committed sessions** rather than trusting the docstring. A cross-validation
  written in prose is a story; one a test recomputes is a measurement that will
  say when it stops being true. `0x4C2F` lateral acceleration shares `0x4C30`'s
  scaling and stays at level 2: a sibling being confirmed is suggestive and is
  not evidence, because nothing here measures cornering independently.

- **`0x2429` was allowlisted on 2026-09-03 and then reachable from no profile at
  all**, so nothing could ever transmit it -- an identifier approved for use and
  never used. Building the confidence table is what found it. It now has a
  profile, `dmc-17-nominal`, which asks it at module `17` alongside the two
  identifiers proven there as a positive control: if `0x2885` and `0x2414` answer
  and `0x2429` does not, the negative is about the identifier rather than the
  addressing. A test now asserts every allowlisted identifier is reachable from
  some profile or some recorder group.

- **The passive experiment was run, and it came back empty.** Thirty seconds at
  the diagnostic connector on 2026-09-04 (UTC), parked and awake at 380.6 V and
  75.4 % state of charge: **zero bytes received.** Sixty-five bytes transmitted,
  every one adapter configuration, reconstructed from the transcript rather than
  from the program's intentions. `ATCS` read `T:00 R:00` before and after; the
  DTC inventory was empty before and after; the recorder restarted and resumed
  writing rows immediately. Conditions, full command manifest and transcript
  hash in `docs/VALIDATION.md`.

  This is the outcome `PASSIVE_CAN_VALIDATION.md` predicted, now measured on
  this truck instead of inferred from other people's. **There is no passive
  fallback**: every byte this project has ever obtained from this vehicle
  arrived because something asked for it. It does not say the internal networks
  are quiet — they are certainly not, behind the gateway — and it does not
  cover driving or charging, which were not tested. What it retires is "try
  sniffing the DLC" as a recurring idea. The line above that said this was "not
  yet run on the vehicle" is now superseded within the same release.

- **`hummer-obd-passive`: the first tool here that asks the vehicle nothing.**
  Every other command sends a request and reads the answer. This one puts the
  adapter into receive-only CAN monitoring -- `STCMM0`, where it does not even
  assert the acknowledgement bit that a normal CAN node puts on every frame it
  hears -- and records whatever arrives. It answers a question the rest of the
  project cannot: not *what will a module tell me if I ask*, but *is anything
  being said at all*. `docs/PASSIVE_CAN_VALIDATION.md` set the prerequisites for
  this a day before it existed and they were treated as binding.

  **The gate was not widened.** The obvious implementation is an exact entry in
  `_ALLOWED_AT_EXACT`, and it is wrong: that set feeds `validate_command`, which
  is the unattended collector's gate and the default `SerialTransport`
  validator. An entry there, however narrow, makes monitoring reachable from a
  service that runs for hours with nobody watching. There are two more gates
  instead -- `validate_monitor_setup_command` (the production gate plus
  `STCMM0`) and `validate_monitor_stream_command` (`STMA`, and not even
  `STCMM0`). `capabilities.py` now puts all six monitor commands to the *live*
  production gate, so a future widening flips a published report entry from
  refused to accepted in plain sight.

  The split buys a structural guarantee rather than a rule to remember.
  `MonitorTransport` is built with the *setup* validator, so `send("STMA")`
  raises instead of blocking for the full timeout and returning truncated bytes
  flagged as a timeout -- the failure the validation doc warned about at length
  is now one the object cannot make. It is a subclass in its own module for the
  same reason: `collector.py` constructs a `SerialTransport`, so a `capture()`
  method on *that* class would turn "unreachable from the collector" from a
  property into a convention. `hasattr(SerialTransport, "capture")` is `False`
  and a subprocess importing the collector ends without the monitor in
  `sys.modules`; both are tests. `transport.py` has a zero-line diff.

  `ATSP0` is refused at import time: auto protocol detection discovers a
  protocol *by transmitting*, and a tool whose whole claim is that nothing
  reaches the vehicle cannot auto-detect its way onto the bus. The protocol is
  pinned to `ATSP7`.

  **A zero-byte capture exits 0.** That is the documented negative result, not a
  failure to retry, and the tool says so in its own output. It also says, when
  bytes do arrive, that the count is not a measurement of bus load: ASCII over
  Bluetooth at 115200 caps at a few hundred frames per second against a bus
  carrying thousands, the capture is lossy by construction, and the loss is not
  recorded anywhere. **Not yet run on the vehicle** -- everything above is
  offline evidence.

- **A test this repository could not previously write.** Every existing
  transport test asserts against a fake serial port's list of *command names*.
  That is the right check for request/response and it cannot, by construction,
  catch a write that bypassed `log_tx`: the fake would record the bytes, the raw
  log would not, and both lists would still look plausible. For a tool whose
  entire claim is silence, the assertion has to be the bytes -- the
  concatenation of the raw log's `tx` records equals the concatenation of
  everything the port was handed, byte for byte, and the same in the `rx`
  direction.

  Holding that property found a real defect immediately. Reading one byte and
  then draining `in_waiting` into the same buffer loses the first byte when the
  second read raises: it had reached the program and never reached the
  transcript, which is the one thing the raw log exists to prevent. Each read is
  now banked before the next is attempted. `transport.py:165-168` has the same
  shape and the same small gap on a mid-read link failure; it is left alone
  deliberately, because the passive path was specified to give that file a
  zero-line diff, and it is recorded here rather than silently carried.

- **`docs/CAN_FD_EXPANSION.md`.** Buying nothing and choosing nothing: a
  comparison of red panda, PiCAN FD Duo, the isolated Waveshare HAT and
  MDI2/GDS2, written now so the decision is not made at 2 a.m. with a truck
  plugged in and a charge session ending. It states the hard rule once -- never
  connect anything to an internal pair until GM service information or measured
  physical-layer evidence identifies that bus and its bitrate -- and it is
  honest that we have no evidence CAN FD at the connector would show us
  anything: the gateway is a boundary, not a bottleneck, and `hummer-obd-passive`
  measures that for free before anything is spent.

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

- **Four identifiers were proven and then never captured.** `27BF`, `27BB`,
  `27B5` and `2709` answered at module `CB` on 2026-09-03 and were left out of
  the recorder, so each had been seen in exactly one state — warm, parked, just
  driven — and could never be decoded from that. Proving an identifier answers
  and then not recording it wastes the discovery: the only way to learn what a
  field means is to watch it across states it has not been seen in. All four now
  record every cycle as raw columns, and a test asserts that **nothing proven at
  `CB` is left uncaptured**, so this cannot recur silently.

- **Six PIDs the vehicle said it supports were going uncollected.** The
  per-module census run on 2026-09-03 had module 17 advertise nine service 01
  PIDs — `01 0D 1C 1F 21 30 31 42 A6` — and the recorder was taking two. The
  other seven were legislated, already decodable, and free. Six now record every
  cycle: OBD standard, run time, distance with MIL on, warm-ups since codes
  cleared, distance since codes cleared, and control module supply voltage.
  `01` stays out deliberately — it is a monitor-status bitfield, not a scalar,
  so it has no single column to land in.

  These are not sourced candidates and carry no provenance debate: the
  vehicle's own support bitmap is the evidence. They also reuse
  `decode.decode_pid`, which already holds each scaling and unit
  (`decode.py:446-453`) — six more hand-rolled branches would have been
  re-deriving what that module already knows.

  **Verified on the vehicle**: all six populate 5/5 rows, and `run_time_s`
  increments at the sample rate — 77, 87, 115 s — which self-validates the
  decode.

  `0142` gives a **third independent reading of the 12 V rail**, and it changed
  the explanation rather than confirming it. The three are consistently ordered
  in 15 of 15 rows — adapter 13.100 V, responding module 12.694 V, drive motor
  controller 12.247 V, about 0.4 V apart each. The earlier note called the
  adapter/module gap a 6 % disagreement and guessed at calibration; if one
  device read high, two would agree and one would differ. Three differing
  *monotonically* is what a distribution with resistance in it looks like —
  one measurement at each of three points, not three of the same point.
  Recorded as a better explanation, not a proven one, with the test that would
  settle it: IR drop widens under load, so a high-power drive should show it.

- **A charge session is reported as a charge, not as a strange drive.**
  `analyze` detects charging from sustained negative pack current — a vehicle
  can sit still without charging, and the sign of the current is what says which
  way energy is moving — and reports energy added, SoC gained, duration, peak
  current, cell-spread drift and charge power by **both** independent routes.
  It also reports how far each unproven raw field moved, because that is the
  whole reason to record a charge: a field seen in one state cannot be decoded
  from that state.

- **`--trend`: what changes between sessions, which is where degradation
  lives.** Every other tool here reads a single session, and cell spread
  widening over months — the earliest sign of a pack going bad — is invisible
  inside any one of them. The output states plainly that rows are not
  like-for-like, since spread widens with load and temperature and these
  sessions were recorded at different states of charge.

  It found something on its first run: one session measured **91.4 cells in
  series** against every other session's 96.0. That was not degradation, it was
  a wake edge — twenty-four transitional rows dragging the ratio down. The fix
  moved the sanity filter from `decode_fields` into `analyze` (the import
  direction only allows one of those) and applied it before comparing, after
  which the session reads **96.0196**.

- **`hummer-obd-decode`: the project can now re-derive its own findings — and
  the first thing it did was disprove one.** Every published correlation lived
  only as prose in a code comment, computed ad hoc in a shell; there was no
  correlation function anywhere in `src/`. The new tool extracts every plausible
  field from each raw hex column — single bytes, big-endian `u16`/`s16` pairs,
  `u24` windows — and correlates each against every quantity the vehicle reports
  directly. Stdlib only: `statistics.correlation`, no pandas, because the
  analysis stack is hand-rolled for a Pi Zero on purpose.

  It reproduced `0x2B43` against state of charge at **+0.994** (published:
  +0.995) and found something the hand analysis missed — the same positions
  track `energy_kwh` at **+0.999**, better than they track SoC, which is what a
  finer-grained field should do.

  **It did not reproduce `0x5401` against pack current at −0.81.** The real
  figure over 1907 paired rows is **−0.09**. The original came from a corpus
  that was almost entirely parked and charging, where pack current spanned −22
  to +105 A; once real driving was recorded, current reached +836 A while the
  byte stayed at zero, and the relationship collapsed. It was measuring corpus
  composition. The conclusion it appeared to support — that `0x5401` tracks
  charging state and is not power — is unchanged, because it rests on the
  plateau across a ninefold power range and the decay to zero after a charge,
  neither of which involved that number.

  Three behaviours are deliberate and tested: transitional rows are dropped and
  the count reported (correlating through wake/sleep edges invented a +0.55);
  every correlation carries the **span** of what it was measured against (all
  the temperature figures rest on 5.4 °F); and a constant field is reported as a
  finding rather than skipped.

- **Six documents were asserting things the code had already disproved.** An
  audit of every file against the current source found claims that had outlived
  the facts, in some cases by a day, in one case inside the same file:

  * `README.md` said pack voltage "was not obtained" thirteen lines below a row
    saying it was **proven**. Both were written on 2026-09-02, four hours apart,
    and the older one was never revisited.
  * `docs/GM_MODULE_MAP.md` listed module `40` as "none tried", module `45` the
    same, pack voltage as "the highest-value gap in the project" — the gap a
    commit message says was closed — and stated "pack voltage remains
    unobtained" outright.
  * `docs/TELEMETRY_CATALOG.md`, which calls itself the authoritative list, said
    the gate held "currently 14" identifiers against 35, described a
    "26-column" CSV against 40, and listed module `40` as never asked in a
    summary table sitting above its own section documenting nine identifiers
    answering there.
  * `docs/CAPABILITIES.md` and `docs/PASSIVE_CAN_VALIDATION.md` both still said
    pack voltage was unavailable.
  * `docs/ARCHITECTURE.md`'s service table omitted `hummer-drive` entirely.

  All corrected, and the counts that keep drifting are gone rather than
  updated: column count and identifier count are now named by their source in
  code. Where a claim outlived the fact, the correction says so instead of
  quietly replacing it.

- **[CAN priority](docs/CAN_PRIORITY.md), the day's architectural finding,
  written down.** The full matrix of which module answers service 22 at which
  priority, measured at both, with the three failure shapes distinguished:
  `NO DATA` (nothing replied — says nothing about the identifier), `7F 22 31`
  (present, no such identifier), and `7F 22 11` (present, not this service at
  this priority — new to this project). It states what it does *not* establish
  as carefully as what it does: no other priorities were tried, and why the
  split falls on the chassis and body controllers is unmeasured.

- **The telemetry catalog covers today's haul.** It is the single authoritative
  list and was missing all of it: module `40`'s nine raw identifiers in a new
  section, `CB`'s five new ones and `0x2AF5`'s recovered trailing bytes, pack
  voltage answering at all three drive motor controllers, and four new entries
  in the refusals table — including the one recording that module `40`'s
  silence was **misread** rather than quietly correcting it.

- **The runbook covers the drive recorder.** It produces almost all of this
  project's data and the operator documentation did not mention it: how to
  restart it without a password, the three read-back commands that never open
  the serial device, the rule that anything which transmits needs the recorder
  stopped first (and the sessions pulled before that), and a table separating
  its normal noisy log lines from a genuine fault.

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

- **"Three fields the source calls temperatures did not move" said more than it
  should have.** During the charge `0x434F`, `0x4127` and `0x4124` held single
  values while the pack warmed 16.2 F, and that was written up as evidence
  against the source's labels. The morning after weakens it: two of them **do**
  take other values in other states — `0x4127` went 1048 → 234 and `0x4124` went
  0 → 1000 across the overnight cool-down. They are not dead fields; they held
  still through a charge.

  The rule still applies to that window and it says less than it appeared to.
  Corrected in all three entries.

- **A guard I wrote this morning fired on a false positive.** The check that no
  claimed temperature span exceeds the corpus span matched any `N.N F` in the
  basis text, so it broke the moment that entry gained an *absolute* reading —
  "reads 95.0 F while the display shows 94 F" is not a claim about how much
  evidence exists. It now matches only figures explicitly described as a span.
  A guard that cannot tell the two apart gets silenced rather than heeded.

- **The `0x4149` "match" was coincidence, and one query last night would have
  shown it.** The entry above reports the field holding `0x00A0` = 160 across
  all 147 charging samples while the JuiceBox read 40.2 A, with 160/4 = 40.0,
  and calls it strong circumstantial support.

  **It had already been 160 for 124 minutes before the charger was plugged in.**
  The charge did not produce the value; it was simply the value at the time.
  Across the corpus the field takes 36, 96, 100, 160, 384, 385, 388 and 389,
  changing repeatedly with nothing connected, and it read **384** the morning
  after unplugging — a value it also took several times overnight while idle.

  Two loose clusters and no relationship to a connected EVSE that this data
  supports. Level 1 was already correct and the divisor was never applied, so
  nothing downstream was wrong — but the *reasoning* published was, and the
  check that would have caught it was one query: **when did this value first
  appear, relative to the event it supposedly responds to?**

  That question is now the first one to ask of any field that appears to react
  to something.

- **A sleeping vehicle saw a hundred requests every five minutes instead of
  one.** The restart-loop fix above stopped the process churning, but left the
  watch opening a full session on each 300-second pass — the rail read 12.8 V,
  no threshold can classify that, so it tried, spent a session init and three
  cycles of enhanced reads finding nothing, and slept again. Better than once a
  minute, and still the opposite of the documented promise that **a sleeping
  vehicle sees only `ATRV`**.

  It cannot be *only* `ATRV` forever: something has to notice the vehicle
  waking, and the rail demonstrably cannot. So the cost is now **one legislated
  request** — `010D`, through the ordinary unattended gate, no enhanced
  identifier is used to poke a sleeping truck. Once the modules have been seen
  to stop answering while the rail still read awake, the watch asks that one
  question and only opens a session if something replies.

  A hundredth of the traffic, answering the same question. Four tests, including
  one that runs `run_auto` against a permanently silent vehicle and asserts at
  most two session files, and one that confirms the probe does not latch the
  watch shut when the vehicle does wake.

- **A sleeping vehicle became a restart loop, and it was caused by that
  morning's threshold change.** Found by checking the node after the charge
  rather than by anything reporting a fault.

  The truck was asleep at **12.8–12.9 V** — above the new `WAKE_VOLTS`, and a
  reading that has now been measured in *both* states. So `run_auto` called it
  awake and opened a session; `record()` decoded nothing, exited to force a
  reconnect; systemd restarted the process; repeat, about once a minute. **Each
  pass sent a full session init and a round of enhanced reads to a sleeping
  truck** — the exact opposite of the guarantee that a sleeping vehicle sees
  only `ATRV`. Two hundred-odd rows of nothing and a fresh session file every
  ninety seconds.

  The old reasoning had been "a false wake costs about three dead cycles". That
  is true *once*. It ignored systemd restarting the process, which turns a cheap
  one-off into an unbounded loop.

  **Two fixes, and neither is a threshold.** Raising it back would restore the
  risk of sleeping through a drive, and 12.9 V genuinely cannot be classified.

  *`record()` now asks whether the adapter **answers**, not what it says.* A
  reply proves the link is healthy, so silence on the bus is the vehicle's doing
  — end the session. Only if `ATRV` is silent *too* is the link suspect and
  worth exiting for. It also no longer reconnects a link that is answering,
  which it previously did twice on the way to giving up.

  *`run_auto` honours that verdict* via a new `Session.ended_asleep`. Without it
  the watch re-read the same ambiguous voltage, called it awake, and opened
  another session immediately — the loop simply moved from between processes to
  inside one, which the first fix alone did on the live vehicle before this was
  added.

  Four new tests, including one that drives `run_auto` end to end and asserts it
  creates at most two session files against a permanently silent vehicle.

- **An efficiency figure of 60 %, from comparing a mean against a point.** The
  first pass at onboard charger efficiency divided the *mean* pack power across
  the whole charge — 5.59 kW, dragged down by a dip to 2.25 kW — by an AC
  reading taken at a single moment. A mean over one window against a point in
  another is not a comparison, and 60 % is not a plausible onboard charger.
  Corrected to instantaneous, matched within 90 seconds: **87.6 % and 91.0 %**.

  This is the same error shape as the `energy_kwh` "decrease" recorded and
  corrected earlier the same evening, which compared 566 parked samples against
  22 charging ones. Worth naming twice because it looked entirely reasonable
  both times, and it was caught both times only by the answer being physically
  implausible rather than by anything structural.

- **A protocol was recorded as though writing it down made it happen.** The
  three passive captures at 04:28–04:31 were published as "the owner worked the
  lock, unlock and other fob buttons through a three-minute window", and the
  conclusion drawn was that event-triggered traffic had been ruled out and the
  passive path closed for good.

  **Nobody was at the vehicle.** The captures were started immediately after
  asking for the button presses, and the owner — who was away — confirmed
  afterwards that none happened. The captures are real and their zero-byte
  results are real; what was invented was the vehicle state they were taken in.

  This is the exact failure `hummer_obd.experiment`'s `label_source` field was
  built to prevent — a claim about what a human observed, unbacked by a human
  having observed it. The field was bypassed by asserting the state in prose
  instead of recording it as data, which is worth more than the specific error:
  **a discipline that only applies where the schema reaches is not a
  discipline.**

  Corrected in `docs/VALIDATION.md` with the original text quoted rather than
  removed. The surviving result is narrower and still useful: the connector is
  silent *while charging*. The fob experiment has not been run.

- **A capture that received nothing reported five bytes.** They were `STMA\r` —
  the adapter echoing the tool's own stream command back despite `ATE0`,
  observed once in four live captures. Bytes this tool transmitted are not bytes
  the vehicle sent, and reading "captured 5 bytes" as traffic is exactly the
  wrong conclusion to hand someone. The echo is now recognised, logged under its
  own note so it is preserved rather than dropped, and excluded from the count.
  A companion test proves real traffic arriving *after* an echo is still kept.

- **A scripted edit asserted on a string whose line-wrapping had changed.** The
  same class of failure as the `states`-tuple corruption an hour earlier, caught
  the same way — by asserting before substituting. Redone as an exact
  whole-line match, which reported how many occurrences it replaced (three)
  rather than silently doing one or none.

- **A scripted edit wrote six paragraphs into the wrong dataclass field.**
  Inserting before the first `"),` after each identifier put the text inside the
  `states` tuple, producing `("parked", "driving. On 2026-09-04 ...")` — six
  corrupted entries. The suite caught it immediately and the change was reverted
  rather than patched. Redone with explicit full-block replacements, each
  asserted present before substitution.

- **A comparison of means across unequal windows read as a decrease.** An early
  look at the charge reported `energy_kwh` down 0.076 kWh — while the pack was
  gaining. The figure was a mean over 566 parked samples against a mean over 22
  charging ones, which is not a before-and-after at all. Within the charge the
  field rises monotonically, `132.72 → 133.03`. Corrected before it reached any
  document.

- **A link check that could not tell a committed file from a stray one.** Two
  documents were written into `docs/` by a background process and left
  untracked. Every link to them resolved — on this machine, against the working
  tree — and would have 404'd for anyone who cloned the repository. The check
  added specifically to catch broken links passed, because `os.path.exists`
  answers a different question from "is this in the repository".

  Every link target is now checked against `git ls-files`, and the new test was
  verified to fire by pointing a real document at a real untracked file before
  being trusted. Skipped rather than failed outside a git checkout, since a
  tarball install has no index and that is not a defect.

- **1,201 lines of documentation were committed without being read.** Commit
  `b8edd1a` used `git add -A -- docs`, which swept in both files above while its
  message described something else entirely. They have now been reviewed —
  the part numbers are NHTSA bulletin IDs with working URLs rather than invented
  GM parts, the prices are explicitly hedged as unestablished, and no connector,
  pin, bus name or bitrate is asserted for this vehicle — but the review should
  have preceded the commit, not followed it. `git add -A` over a directory a
  background process can write to is the mistake; the path-scoped form is not
  scoped enough when something else is also writing there.

- **`ROADMAP.md` claimed two documents were written that did not exist**, and
  linked to both. Written and shipped within the same hour as the roadmap
  itself, in a table whose entire purpose is saying honestly what is and is not
  done — which is the failure mode the table was built to prevent.

  The link checker added earlier the same day did not catch it, because it
  covered `docs/` and `README.md` and the roadmap is neither. **A link checker
  that covers `docs/` but not the roadmap pointing at `docs/` is checking the
  wrong half**; it now covers `ROADMAP.md` too. Both rows corrected to
  "not written yet", with what each would be for stated in place of the link.

- **Five false claims found by a fourteen-agent adversarial verification pass**,
  each independently re-confirmed before being fixed. The pass inventoried every
  dimension of the project's access surface and then attacked its own inventory;
  most of its 58 refutations were agents correcting each other, which is the
  design working. Five named defects in the repository survived that filter:

  * **`README.md` and `docs/VALIDATION.md` published "509 tests / 391 subtests"
    against a measured 836 / 2424** — a written count drifted by 327 tests. Both
    now point at the command that produces the number, the same fix `HANDOFF.md`
    got earlier today. A count in prose is a claim like any other.
  * **`src/hummer_obd/enhanced.py` and `docs/GM_MODULE_MAP.md` both said that
    without `ATCP14` "the module does not answer".** That is false for the very
    module the sentence is about: `CB` answers at **both** priorities, which is
    what the `bsm-cb-p18` profile was written to establish. It is also the exact
    overgeneralisation that hid module `40` for a day — priority is per-module,
    `28` answers only at `0x14` and `40` only at `0x18`. Set the priority
    because it decides which addressing you are using, not because omitting it
    guarantees silence.
  * **`capabilities.py` published "GM/Ultium identifiers are unproven on this
    VIN" in every generated capability report.** 31 of 35 answer and nine are
    cross-validated. A fixed string inside a *generated* report is the worst
    place for one, because the report looks like a measurement whether or not
    the sentence inside it is. It now counts from `confidence.CONFIDENCE`, and a
    test asserts the counts match and that the phrase is gone from the emitted
    string.
  * **`docs/PASSIVE_CAN_VALIDATION.md` quoted a cell spread of 4.9–5.3 mV.**
    That was one early session and covers 17.7% of what has since been recorded.
    Across 4843 committed samples: median 4.6 mV, 86.2% between 2 and 5 mV, tail
    to 15.4 mV. Recomputed independently before changing it.

  Worth recording what the pass did **not** find, because it is the more useful
  signal: no defect in the safety gate, no gate that accepts something it should
  refuse, no column claimed that is not collected, and no broken link. The
  refutations were all in prose describing the code, never in the code.

- **`0x2429` was decoded as volts, published, and it is not a voltage.** This is
  the most convincing wrong answer this project has produced, and the way it
  looked right is the part worth keeping.

  The first and only reading was 22534. Divided by the source's 64 that is
  **352.09 V**, which across the 96 cells this pack was independently shown to
  have in series is **3.6676 V per cell** — the textbook nominal for an NMC
  cell, to four significant figures, from a number nobody had fitted. The source
  called it nominal pack voltage. Every step of that reasoning was sound except
  the sample size.

  Three hours later there were **405 samples**, and the field moves: 18556–26588
  raw, 108 distinct values. Worse, it moves **with load** — `r = +0.83` against
  pack current and against HV power, `r = −0.67` against pack voltage, and
  essentially nothing against state of charge (−0.09) or energy (−0.08). It
  rests near 22350 and rises about **16.4 counts per amp** of discharge. A rated
  figure does not move at all, and a voltage does not rise with the current
  drawn from it.

  Now stored raw with no equation, exactly like `0x5401` — the other identifier
  whose published label this vehicle contradicted. The column is
  `field_2429_raw`, not `nominal_pack_v`, **because a name is a claim too**, and
  `analyze._TEXT_COLUMNS` was updated in the same change because forgetting that
  step has silently broken two columns before.

  It was found by adding a `--coverage` view to `hummer-obd-access`, which
  reports what fraction of the corpus carries each column and its value range.
  A column asserted to be constant, showing 108 distinct values, is not
  something a table of names can surface. Confidence level drops **2 → 1**, and
  the catalog-parity test written this morning caught the documentation the
  moment the level changed — which is what it is for.

- **Fault codes were never actually read until 2026-09-04.** Every DTC check in
  this project's history returned `NO DATA`, and every one was recorded as "no
  fault codes". That is precisely the error the three-failure-shapes rule exists
  to prevent — `NO DATA` means *nothing replied* and says nothing whatever about
  whether codes exist — and it was made by the person who wrote the rule, in a
  table on the same page as the rule, the same day.

  A run re-confirming modules `CD` and `45` first-hand left the adapter
  addressed to the gateway, and the DTC read that followed answered properly:

  ```
  probe default addressing   03 -> NO DATA        07 -> NO DATA        0A -> NO DATA
  addressed to module 45     03 -> 18DAF145024300 07 -> ...4700       0A -> ...4A00
  ```

  `43 00` is a positive response to service `03` carrying a DTC count of zero;
  likewise `47 00` and `4A 00`. **So the vehicle really does have no fault codes
  — and until that frame arrived, the claim rested on silence.** The conclusion
  survived; the evidence for it did not, and the difference is the whole point.

  Corrected in `VALIDATION.md` (the passive-capture table now says `NO DATA`
  with the correction beside it), `TELEMETRY_CATALOG.md` (the **measured** grade
  is now correct for a different reason than it was first given), and
  `hummer_obd.access` (the freeze-frame entry cited the same non-evidence).
  `ACCESS_MATRIX.md` keeps the whole episode as a worked example under the
  failure-shape rule, because the generalisable lesson is narrower than "read
  the shapes": **a `NO DATA` you have already explained to yourself is the
  dangerous kind.** The rule is easy to apply to someone else's result and hard
  to apply to one that agrees with what you expected.

- **Two broken relative links, written an hour after adding a link checker that
  would have caught them.** The correction above used `../docs/X.md` from inside
  `docs/`, twice. The checker existed and was scoped to a single file. A link
  checker scoped to the document you happen to be editing is scoped to the wrong
  document; it now covers every markdown file in `docs/` plus `README.md`, and
  a vacuity guard requires it to have actually checked more than forty links.

- **"5.4 degrees Fahrenheit" was repeated in four places and had quadrupled.**
  It was the corpus-wide temperature span when written, and it was the stated
  reason no thermal field could be decoded. By 2026-09-04 the corpus spanned
  **23.4 F** (91.4-114.8) -- the constraint had loosened more than four-fold and
  nothing noticed, because a figure in prose has no way to notice.

  The correction is narrower than "it was wrong", which is why it is worth
  stating precisely: **5.4 F is still exactly right about `0x2709` and
  `0x27BB`**, whose 749 rows do span only that. The module-40 thermal
  identifiers cover **9.0 F** across their 1131 rows, and `0x2AF1` the same,
  because all of them were added part-way through the corpus. Only the
  *corpus-wide* claim was stale, and the per-field spans are the ones that
  matter -- exactly what `decode_fields.py` reports per correlation and what a
  corpus-wide figure hides.

  Re-running the decode against the wider corpus: the best any byte window of
  `batt_temp_a_raw` reaches against `temp_f` is **+0.69 over 1131 samples and
  9.0 F**. A direction, not a scaling. Nothing was promoted. What these fields
  need is a cold morning, not another source.

  The guard added is deliberately asymmetric rather than pinning the number,
  which would fail every time a session is committed: a test now asserts that
  **no span claimed in code exceeds the span the corpus actually has.**
  Under-claiming evidence is safe; over-claiming is the failure that matters.

- **Six false documentation claims, found by an audit run against the same
  day's work.** Three were made hours earlier by the change that introduced
  confidence levels: `0x4A7A`, `0x4C30` and `0x33E5` were promoted to level 3 in
  code while `TELEMETRY_CATALOG.md` still graded them `read`. Three were older
  and had simply never been revisited:

  * `CAPABILITIES.md` said "pack voltage and cell balance are still unavailable,
    because no sourced identifier for them has been found" — while the *same
    file*, 200 lines below, said pack voltage "is now proven at `0x2885`".
  * `PASSIVE_CAN_VALIDATION.md`'s findings table said pack current was "still
    not obtained — no sourced identifier found". The pack-voltage row directly
    above it had been corrected; this one was not, and the paragraph beneath
    repeated the error.
  * `ENHANCED_PID_VALIDATION.md` said passive monitoring "is not approved, and
    it is not on the allowlist" — after it was approved, built and run.

  Each is corrected with the old text struck through rather than deleted,
  because a claim that outlived its truth by a known number of days is worth
  more visible than hidden.

  **The structural fix matters more than the six.** Nothing checked
  `TELEMETRY_CATALOG.md`'s grade column against `confidence.py`, so it drifted
  within hours of the levels existing — the fourth hand-kept inventory in this
  project to do so. `tests/test_confidence.py` now scrapes that column and
  asserts every identifier's word matches its numeric level, both directions:
  nothing graded that the gate does not hold, and nothing the vehicle answers
  left ungraded. Only the four ISO reachability probes are legitimately absent.

  The scraper had a bug of exactly the kind it exists to catch —
  `grades.get(did, "raw")` gives an unseen identifier rank 1, so `raw` never
  beat its own default and all sixteen raw-only rows were silently dropped. A
  vacuity guard asserting the pattern matches at least 25 rows is what caught
  it, which is the argument for writing that guard into every table-scraping
  test.

- **The wake threshold was wrong a second time, and it cost a live session.**
  On 2026-09-04 the recorder was restarted, read 12.9 V, classified the vehicle
  as asleep and went to its 300-second watch -- on a truck that was awake in
  front of it, answering service 22 from five modules, pack at 379 V, drawing
  about 4 kW.

  The threshold had been moved to 12.95 on the strength of an argument that read
  well and was wrong: *"12.9 V was never a steady state; it was one sample on the
  way down."* It is a steady state. The vehicle sat parked and awake at exactly
  12.9 V for over twenty minutes, and 146 recorded rows every one of which reads
  `12.9V` were sitting in the corpus while that sentence was written.

  The error was in the sampling, not the arithmetic. Three states had been
  observed -- asleep, driving, shutting down -- and the conclusion was drawn as
  though those were all of them. **Parked-and-awake is a fourth**, it floats
  lower than driving because nothing is moving, and no measurement covered it.
  The threshold was then set 0.05 V above a state nobody had watched.

  And the bands do not merely touch at 12.9 — **they overlap there.** The
  journal has both, hours apart: at 16:10:48 the vehicle read 12.9 V with
  *nothing answering*, genuinely asleep; from 00:41 to 01:03 it read 12.9 V with
  five modules answering and 146 rows recorded, genuinely awake. So no threshold
  classifies 12.9 V correctly, and hunting for one is the mistake — twice now.

  The recorder already had the right instrument and it is not the voltmeter.
  `record()` ends a session when **nothing answers** for three consecutive
  cycles, which measures the thing actually being asked about; the 16:10:48 line
  is that check firing, not the threshold. So the threshold's job is narrower
  than it looks: it decides when to *try*, and answers decide whether the vehicle
  is really there. It therefore belongs *below* the ambiguous region rather than
  inside it, and the asymmetry settles where — **a false wake costs about three
  dead cycles and a handful of unanswered requests; a false sleep costs an entire
  drive.** 12.8 sits above the only unambiguous sleeping reading and below the
  ambiguous one, so every ambiguous case is resolved by asking rather than
  guessing. Every measured state now has its own assertion, and three existing
  tests that used 12.8 or 12.9 as a stand-in for "asleep" use 12.7, the only
  unambiguous sleeping voltage observed.

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
