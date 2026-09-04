# Telemetry catalog

**The single authoritative list of what this node can read from this vehicle.**

If you are an agent or a reviewer trying to work out this project's
capabilities, read this file. Everything else is supporting detail:
[module map](GM_MODULE_MAP.md) for topology, [enhanced
candidates](GM_ENHANCED_CANDIDATES.md) for provenance and rejected leads,
[safety](SAFETY.md) for the gates, [validation](VALIDATION.md) for the power
measurements.

Last measured 2026-09-03. Vehicle: 2024 GMC Hummer EV, GM BT1/Ultium.
Adapter: OBDLink MX+ (STN2255) over Bluetooth SPP to a Raspberry Pi Zero 2 W.
Link: ISO 15765-4, 29-bit CAN, 500 kbit/s.

## Evidence levels

Each signal carries one, and they mean different things:

| Level | `confidence.py` | Meaning |
|---|---|---|
| **measured** | 3 or 4 | Read from this VIN, decoded, and cross-checked against an independent quantity |
| **read** | 2 | Read from this VIN and decodes to a plausible value, but nothing independent confirms the scaling |
| **raw** | 1 | The module answers, the bytes are stored, the meaning is *not* claimed |

The middle column is the machine-checkable version of the same judgement.
`hummer_obd.confidence` holds one entry per allowlisted identifier, keyed
identically to the safety gate and asserted equal to it, and it splits
**measured** in two: level 3 is cross-validated, level 4 is cross-validated
*and* re-derived in more than one vehicle state. Anything below 3 is not a
telemetry reading whatever its column is called. The generated table in
[GM enhanced candidates](GM_ENHANCED_CANDIDATES.md) carries the levels, and
`tests/test_confidence.py` recomputes the level-3 claims from the committed
sessions rather than trusting this table.

A signal can be `raw` in two very different situations, and conflating them
cost this project four identifiers. One is *recorded every cycle*, accumulating
the states a field must be seen across before it can be decoded. The other is
*answered once in a supervised probe* — seen in exactly one state, and
undecodable from that no matter how much later analysis is applied.

`0x27BF`, `0x27BB`, `0x27B5` and `0x2709` were proven on 2026-09-03 and then
left out of the recorder, which put them in the second category while looking
like the first. All identifiers listed below are now in the first: everything
this vehicle has been shown to answer is captured every cycle, and a test
asserts it, so the distinction cannot quietly reappear.
| **refused** | The module answered `7F`, and the response code is recorded |
| **absent** | No sourced identifier exists — not "impossible", "not found" |

---

## 1. High-voltage battery — module `CB` `BSM-BatterySysMngr`

| Signal | Identifier | Scaling | Unit | Level |
|---|---|---|---|---|
| State of charge | `0x27C6` | `[B4:B5] / 655.35` | % | **measured** |
| Energy remaining | `0x27AF` | `[B4:B5] / 100` | kWh | **measured** |
| Estimated range | `0x27C7` | `[B4:B6] / 103` | mi | **measured** |
| Distance since full charge | `0x27C0` | `[B4:B6] / 16.09344` | mi | **read** |
| Temperature | `0x0046` | `(B4 - 40) * 1.8 + 32` | °F | **read** |
| Cell voltage average | `0x2AF5` | `[B0:B1] / 10000` | V | **measured** |
| Cell voltage minimum | `0x2AF5` | `[B2:B3] / 10000` | V | **measured** |
| Cell voltage maximum | `0x2AF5` | `[B4:B5] / 10000` | V | **measured** |
| Cell spread | derived | `(max - min)` | mV | **measured** |
| 26-element array | `0x2B43` | none applied | — | **raw** |
| Charging-state indicator | `0x5401` | none applied | — | **read** |
| 24-element array | `0x2AF1` | none applied | — | **raw** |
| `0x2AF5` trailing bytes | `0x2AF5` | `[B6:B9]`, none applied | — | **raw** |
| Regeneration field | `0x27BF` | none applied | — | **raw** |
| Thermal-management energy | `0x27BB` | none applied | — | **read** |
| Thermal-management distance | `0x27B5` | none applied | — | **raw** |
| A/C compressor temperature | `0x2709` | none applied | — | **raw** |

`0x2AF5` answers with **ten** bytes, not six. The decoder read the first six and
discarded the rest for as long as it existed; they are preserved now. Two of
them do not move: byte 9 held at 23 and byte 7 at 15 through a parked pack, a
117 km/h cruise and a 97.8 kW pull that sagged the pack from 384.88 to
381.73 V. A value that does not move across that range is not measuring it.

`0x2AF1` returns **twenty-four** values, which is the module count three
independent structural results agree on — see [pack
architecture](PACK_ARCHITECTURE.md). The source calls them module temperatures
and under `(x - 40) / 2` they land within 2 °C of the pack figure, which is one
sample at one temperature and therefore not a scaling.

### `0x5401` is a state, and the charge of 2026-09-04 proved it

The identifier switches cleanly and carries no quantity:

| | |
|---|---|
| parked and unplugged | `0x00` across **566 consecutive samples** |
| charging | `0x93` / `0x96` across **all 22 samples** |

Completely disjoint, cross-checked against pack current being negative. So it
reports *whether* the vehicle is charging. It does not report how fast: the
published two-byte `/4350` charger-power scaling remains wrong here — it answers
with a single byte and plateaus across a ninefold power range — and is still not
applied. Why it alternates between `0x93` and `0x96` while charging is unknown.

### What a charge does to state of charge, range and energy

Measured across 101 samples of the 2026-09-04 AC charge, and they behave
completely differently:

| Field | Over twenty minutes of charging |
|---|---|
| `energy_kwh` `0x27AF` | rises continuously, **132.66 → 134.08 kWh** |
| `range_mi` `0x27C7` | steps: **three distinct values**, 231.76 → 232.38 → 233.0 |
| `soc_pct` `0x27C6` | **steps coarsely.** Held 69.943 for the first 101 samples, then jumped to 70.343 in one move at 04:17:06 — a single 0.400 pp step in twenty minutes |

For twenty minutes the pack gained 1.42 kWh — about 0.74 % — and state of charge
did not move by a single count at a resolution of 1/655.35 %. It then moved all
at once, 69.943 → 70.343, and the step under-reports: 0.400 pp is 0.77 kWh
against the 1.58 kWh actually taken on in that interval, so it lags as well as
steps.

This is not a decode problem. The myGMC app showed **70 %** while the recorded
value read 69.943, so the vehicle itself is holding it. A continuously
coulomb-counted energy figure alongside a state of charge that settles in coarse
steps is ordinary BMS behaviour.

**Operationally: during a charge, use `energy_kwh`. State of charge is a lagging
step function, not a live value** — a first reading suggested it was frozen
outright, and twenty more minutes showed it merely steps.

An hour of charging measured the step exactly. `soc_pct` advanced through
thirteen values from 69.943 to 74.743, and every gap is **0.400 pp**:

```text
0.4  0.399  0.402  0.399  0.4  0.4  0.4  0.4  0.401  0.4  0.399  0.4
mean 0.4000, standard deviation 0.0008, one step about every 5.4 minutes
```

Confirmed on the full charge: **33 gaps, mean 0.4001, standard deviation
0.00074**, every one between 0.399 and 0.402. The claim was first made on
thirteen values and it holds.

At 8.27 kW that is 0.75 kWh per step, which matches the interval. Across the
**whole** corpus the gaps are not all 0.400 — they are 0.2, 0.3 and 0.4 — and
they are all near-multiples of **0.1 pp** (mean residual 1.1 % of a step). So
the field's resolution is a tenth of a percentage point, and while charging the
vehicle chooses to advance it four tenths at a time.

### The HVAC A-B-A: the first experiment that separates cause from elapsed time

Every thermal reading before this one was taken across a **single** transition —
a charge starting, a night cooling down, an A/C switch being thrown. A single
transition cannot distinguish a field that responds to the change from a field
that was going to move anyway, because in both cases the field moves and time
passes. The project got this wrong twice: a thermal-limiting hypothesis that
showed +0.72 over four minutes and −0.028 over a hundred samples, and a `0x4149`
"match" whose value predated the charger by 124 minutes.

So this run returns to the starting condition. On 2026-09-04 the owner operated
the controls and reported each switch, parked in a garage at roughly 84 °F:

| Phase | Window (UTC) | Samples |
|---|---|---|
| Cold soak, HVAC off | 15:19 – 15:34 | 99 |
| **A** — max A/C | 15:34 – 15:49 | 80 |
| **B** — max heat | 15:49 – 15:58 | 44 |
| **A** — max A/C again | 15:58 – 16:07:53 | 48 |

The second A/C phase ends at first wheel motion, when the owner drove to work.
The switch minutes themselves are excluded from every phase, so a transient *at*
a transition falls in a gap rather than contaminating a phase.

| Field | cold | A/C | heat | A/C again | Reads as |
|---|---|---|---|---|---|
| `field_4127_raw` | 234 | 234 | **601** | 234 | **heat state** |
| `coolant_1_raw` | 860 | 890→980 | **1125–1170** | 980–985 | **mode, reversible** |
| `coolant_2_raw` | 696–808 | 437–505 | 485–516 | 353–524 | HVAC on/off only |
| `compressor_temp_raw` | 101–104 | 106–110 | 107–112 | 110–112 | no mode information |
| `thermal_energy_raw` | 0 | 10→60 | 70→110 | 120→150 | **accumulator** |
| `field_4124_raw` | 1000 | 1000 | 1000 | 1000 | transients only |

**`0x4127` is a heat-request state, not a battery temperature.** It holds a
single constant value per phase: 234 across all 99 cold-soak samples and all 80
of the first A/C phase, exactly 601 across all 44 heat samples, then 234 again.
It steps at 15:50:02 and returns at 15:59:23 — each within one poll of a switch
the owner threw. No pack temperature is constant to the count for 179 samples,
then moves 367 in one poll, then comes back inside nine minutes. Corpus-wide,
its value 1048 appears in 410 samples of which **every one** has negative pack
current: it is reached only while charging.

**`0x27BB` is an accumulator, and this is the finding that justifies the whole
design.** It rose during A/C and would have been published as an A/C response.
It rose again during heat. Then it kept rising straight through the reversal —
0, then 10→60, 70→110, 120→150, monotonically non-decreasing in steps of 10
from a zero start. It integrates. It was never responding to anything, and only
the return to A/C makes that visible. **It must never be read as a mode
indicator.**

**`0x40E5` is the one continuous field that genuinely tracks mode.** Flat at 860
cold, ramping to 980 under A/C, jumping to 1125–1170 under heat, and returning
to 980–985 under A/C — landing back on the value the first A/C phase ended at
rather than continuing to climb. Up with heat, down when heat stops, back to its
own earlier value: that is a state response, not a clock.

**A prediction that failed, and a label that may not have.** Before looking at
the result this project recorded: *if `0x2709` is genuinely A/C compressor
temperature, it should rise with A/C and not with heat.* It does not
discriminate — the A/C and heat bands overlap almost entirely. But GM's Ultium
vehicles are marketed with heat-pump and waste-heat-recovery thermal systems,
and if the compressor runs in heat mode too then warming in both modes is
exactly what a compressor temperature should do. That alternative has **not**
been sourced for this VIN, so it is recorded as unresolved. What the experiment
does establish is narrower and still useful: the field carries no
A/C-versus-heat information and cannot be used to infer which mode is running.

**What none of this decodes.** No scaling is claimed for any field above. The
experiment constrains *behaviour* — what responds to what — and says nothing
about units. `0x4127` at 601 is not 601 of anything.

**What would raise these to level 3.** One heat cycle is one heat cycle. A
second, on a different day, reproducing 601 in `0x4127` and the return in
`0x40E5`, is the missing evidence. A genuinely cold morning would separately
settle whether `coolant_1_raw`'s 0.0591 °C/count is really 1/16.

### The cold soak: the best thermal experiment yet, and it decodes nothing

An overnight cool-down gave the first comparison between two genuinely different
thermal **states** rather than a monotonic ramp — 72 samples at 95.0 °F against
248 during the charge averaging 102.6 °F.

| Field | cold | warm | Δ | implied °C/count |
|---|---|---|---|---|
| `coolant_1_raw` | 860.0 | 931.7 | **+71.7** | 0.0591 |
| `coolant_2_raw` | 766.1 | 713.7 | **−52.4** | −0.0808 |
| `hv_temp_raw` | 70.0 | 51.7 | −18.3 | −0.2314 |
| `field_4127_raw` | 234.0 | 1048.0 | +814.0 | 0.0052 |
| `field_4124_raw` | 1000.0 | 0.0 | −1000.0 | −0.0042 |
| `compressor_temp_raw` | 103.1 | 110.3 | +7.2 | 0.5875 |

**What this rules out.**

`field_4127_raw` and `field_4124_raw` are **not continuous temperatures**.
Across the entire corpus — 6,949 rows — they take **eight** and **four**
distinct values: 234, 238, 242, 246, 261, 429, 601, 1048 and 0, 418, 910, 1000.
In this comparison each simply switches between two of them. They are also anti-correlated: when one is low the
other is high. A quantity that occupies four values in two days of driving and
charging is a state or an index, whatever its source calls it.

`coolant_2_raw` moves **the wrong way**: it falls as the pack warms, across 252
distinct values. Whatever it tracks, it is not pack temperature in the direction
a coolant sensor would.

**What survives, weakly.** `coolant_1_raw` is the only field whose implied
divisor is near a round number: 0.0591 °C per count against **1/16 = 0.0625**,
about 5 % out. On 7.6 °F of separation that is suggestive and not establishing,
and this project has now been fooled twice by a plausible divisor derived from
too little thermal range.

**What would settle it** is a genuinely cold morning. Arizona in September gave
7.6 °F between states; 40 °F would make a 5 % discrepancy either vanish or
become decisive. Until then all six stay at level 1.

### Modules go quiet during a settled charge

Checked after a night of stopping and restarting the recorder for passive tests,
to confirm nothing had been left broken. Nothing had — but the answer rates move
a great deal with vehicle state, and it is worth knowing before reading a sparse
session as a fault:

| Period | `CB` | `17` | `28` | `40` | standard PIDs |
|---|---|---|---|---|---|
| parked and awake | 99 % | 76 % | 76 % | 77 % | 65 % |
| charging, first 20 minutes | 100 % | **100 %** | **100 %** | **100 %** | **0 %** |
| charging, settled | 96 % | **7–27 %** | 7–28 % | 7–26 % | 0 % |

Two separate behaviours:

**Standard OBD stops entirely the moment charging begins.** Speed and odometer
have a 0 % answer rate for the whole charge — module `17` serves its enhanced
identifiers but refuses the legislated service 01 PIDs. Reasonable: the vehicle
is not being driven.

**The non-battery modules go quiet once the charge settles.** For the first
twenty minutes after plug-in everything answers; thereafter `17`, `28` and `40`
fall to under a third while the battery manager `CB` stays at 96 %. That reads
like the vehicle putting everything but the battery system into a low-power
state for the hours-long part of a charge.

The timing matters for a different reason. The degradation begins at **04:16**,
**before** the first passive capture at 04:27 — so it is vehicle behaviour and
not damage from stopping the recorder or switching adapter protocols. Worth
stating explicitly, because a night of interfering with the rig is exactly when
a coincidence would be mistaken for a consequence.

**Driving is the state where everything answers.** The table above is a
per-module reading of one night. Measured a different way — the fraction of
populated cells across the whole corpus, grouped by which service the column
comes from — the picture is consistent and adds the state that was missing:

| Vehicle state | standard service 01 | enhanced service 22 |
|---|---|---|
| moving | **100.0 %** (2,727 cells) | **100.0 %** (6,382 cells) |
| charging | 37.3 % (3,378 cells) | 99.9 % (5,762 cells) |
| parked and awake | 54.5 % (28,180 cells) | 85.1 % (57,398 cells) |

So "the modules go quiet" is too broad a statement of it. What actually happens
is that **service 01 is refused outside a run state while service 22 is not** —
during a charge the legislated PIDs fall away completely while the enhanced
reads hold at 99.9 %. Under way, both are perfect. A drive is the best
telemetry state this vehicle offers, and a sparse charging session is normal
rather than a fault.

### The three 12 V readings are one rail with offsets, not a disagreement

This project has had an open question about a ~6 % disagreement between its 12 V
readings. There are three: `volts` (the adapter's own `ATRV`, measured at the
OBD connector), `module_voltage` (legislated PID `0142` from module 17), and
`dmc2_v` (`0x33E5`, the enhanced read). They differ by **stable offsets**:

| Pair | n | Offset | sd |
|---|---|---|---|
| `volts` − `module_voltage` | 1,691 | +0.293 V | 0.059 |
| `volts` − `dmc2_v` | 4,909 | +0.759 V | 0.062 |
| `module_voltage` − `dmc2_v` | 1,691 | +0.454 V | 0.033 |

The ordering `volts` > `module_voltage` > `dmc2_v` holds in **every** phase
across 12.1–13.8 V and three vehicle states. The ~6 % figure was `volts` against
`dmc2_v` while driving (12.94 vs 12.14) — a stable 0.76 V offset plus two 0.1 V
quantisation steps, not a measurement conflict.

**Use PID `0142`.** The resolutions differ structurally, and it is not a close
call: `0x33E5` decodes as one byte ÷ 10, giving 0.1 V steps and 19 distinct
values corpus-wide, while `0142` is two bytes ÷ 1000, giving 1 mV steps and 143
distinct values. For any 12 V work the legislated PID is the instrument and the
other two are indicators.

That distinction mattered immediately. A first attempt here to test whether the
offset grows with electrical load — the signature of harness IR drop — compared
phases differing by about 80 mV using `volts`, whose quantum is 100 mV. The test
could not have worked. Redone on `0142` alone:

- **HVAC mode does not move the rail.** 13.539 / 13.537 / 13.527 V across max
  A/C, max heat and max A/C again (n = 80 / 44 / 48), every step inside one
  pooled standard deviation at 1 mV resolution. A clean null, properly powered.
- **Driving drops it 0.92 V**, 6.8× the pooled sd — large and unambiguous.
- **But it is decoupled from traction load.** Over the drive `0142` correlates
  with pack current at only **+0.10** across a 905 A swing, and regen samples
  differ from hard-draw samples by **0.008 V**, a tenth of a pooled sd —
  measured *across a sign reversal*, which is the test elapsed time cannot fake.

So the rail moves 1.2 V during a drive and traction is not what moves it.

### Onboard charger efficiency, measured twice

The one quantity here that no amount of vehicle telemetry could produce. Pack DC
taken from samples within 90 seconds of each charger reading:

| Time | Pack DC | Wall AC (JuiceBox) | Efficiency |
|---|---|---|---|
| 04:16Z | 378.46 V x −21.58 A = **8.17 kW** | 9.319 kW at 40.2 A | **87.6 %** |
| 05:24Z | 382.37 V x −22.25 A = **8.51 kW** | 9.351 kW at 40.2 A | **91.0 %** |

Both in the plausible band for an onboard charger, and the second is higher for a
sound reason: the pack voltage has risen, so the same wall power delivers more DC
power.

**A first attempt at this reported 60 %**, by comparing the *mean* pack power
across the whole charge — 5.59 kW, dragged down by a dip to 2.25 kW — against an
AC reading taken at one moment. A mean over one window against a point in
another is not a comparison. It is the same error shape as the `energy_kwh`
"decrease" recorded and corrected earlier the same evening, and it is worth
naming twice because it looked entirely reasonable both times.

### Sizing the pack: the estimate converges with span, and only with span

The energy-over-state-of-charge ratio should give the pack's capacity. Measured
across the complete 5.32-hour charge — 69.943 % to 89.955 %, +38.83 kWh — it
converges monotonically as the span grows:

| Span measured | Δenergy | Implied pack | vs 191.9 kWh |
|---|---|---|---|
| 2.80 pp | 6.55 kWh | 233.9 kWh | **+21.9 %** |
| 5.20 pp | 10.85 kWh | 208.7 kWh | +8.7 % |
| 10.00 pp | 20.34 kWh | 203.4 kWh | +6.0 % |
| 16.40 pp | 32.19 kWh | 196.3 kWh | +2.3 % |
| **20.01 pp (whole charge)** | 38.83 kWh | **194.0 kWh** | **+1.1 %** |

**Why short spans are biased, and it is not the initial stall alone.**
`soc_pct` advances in 0.400 pp steps roughly every 5.4 minutes while
`energy_kwh` rises continuously. At any window *edge* the two are out of step by
up to one quantum — 0.4 pp against however much energy accrued since the last
step. On a 2.8 pp span that edge error is a seventh of the measurement; on a
20 pp span it is a fiftieth. The bias is an edge effect, and edge effects shrink
with span.

> **An earlier version of this section said the opposite** — that the
> whole-charge figure was "the least trustworthy of the four despite resting on
> the most data" and that capacity should be measured "across a window where
> state of charge is already moving". That was written when the charge had run
> 13.2 pp, and it was wrong. Sub-windows are *more* contaminated, not less,
> because each one adds two fresh edges. The whole-charge span is the best
> estimate available and it lands 1.1 % from the established figure.

A per-band table computed the same way suggested **~234 kWh** across every 2 pp
band from 70 % to 88 % — flatly contradicting both the whole-charge figure and
the established one. That is the same artefact at its worst: a 1.6 pp band
cannot absorb an edge error of 0.4 pp. It is recorded here as a trap rather than
a finding, because it was internally consistent across ten bands and looked
exactly like a real measurement.

### An outside measurement is not automatically better evidence

The app read **70 %** and later **75 %** across that same hour, and the vehicle
read **69.943** and **74.743**. Using them to size the pack from the 9.04 kWh
taken on:

| Source | Change | Implied pack |
|---|---|---|
| myGMC app, whole percent | 5 pp | **180.8 kWh** |
| `soc_pct`, three decimals | 4.800 pp | **188.3 kWh** |
| Established elsewhere, three routes | | 191.9 kWh |

The app is an *outside* reading and the worse one, by 4 %, purely because it
rounds. This project treats outside measurements as the thing that breaks the
circle of correlating a vehicle's numbers against its own — and they do — but
**"outside" and "precise" are different properties, and the first does not
imply the second.** Where the vehicle reports more digits than the display, use
the vehicle's, and use the display for the quantities the vehicle never states
at all: the wall current, the ambient temperature, the charger's own kilowatts.

The app also read **233 mi** against a recorded `range_mi` of **233.0** — exact
— and 70 % against 69.943 %. That is a cross-check of the decoding against an
independent readout, though not of the underlying sensors, since the app is
served from the same vehicle data.

### Charge/discharge power is derived, not read

`0x5401` is published as "charger DC power" with a two-byte `/4350` scaling.
**That is wrong for this vehicle.** It answers with a *single* byte, reads
`0x96` at idle and `0x93` during an 7.8 kW AC charge — non-zero when idle, and
not scalable to the measured rate. It is stored raw and no equation is applied.

Power is instead taken from the slope of `0x27AF`:

```
power_kw = Δenergy_kwh / Δhours
```

That field moves smoothly and with high resolution — 80 distinct values across
ten minutes of charging — which makes its slope a sound measurement. Positive is
charging.

**The slope is taken over a 60-second window, not between consecutive samples**,
and the reason is worth recording. `energy_kwh` is quantised to 0.01 kWh. At a
~7 second cycle a single quantum is about 5 kW, so a consecutive-sample slope
alternated between 9.53 and 4.76 kW while the true rate was a steady 7.8 — right
on average, useless instant to instant. Measured live on a charging vehicle
before and after:

```
consecutive samples   9.53  4.76                                  (+-60%)
60-second window      6.99  7.46  7.77  7.34  7.51  8.06  7.96    (~7.6)
```

Validated against a real AC charging session at 7.81 kW derived offline from the
CSV. The column is empty rather than zero until there is enough history: a
placeholder zero would read as "not charging".

### Why these decodes are trusted

They were never fitted to one another, yet they agree:

| Cross-check | Computation | Result | Independent expectation |
|---|---|---|---|
| range ÷ SoC | 269.04 / 0.8085 | **333 mi** at full | 329 mi EPA |
| energy ÷ SoC, three separate occasions | — | **191.0 / 191.7 / 191.7 kWh** | consistent pack capacity |
| cell voltage vs SoC | 4.02 V at 80.85 % | correct for an NMC cell | — |
| cell ordering | min < avg < max | holds exactly | a wrong offset breaks it |
| charging | energy, SoC and all cell voltages rose together while power read +7.8 kW | internally consistent | — |

---

## 1b. Traction pack voltage and current — module `17` `DMCM-DriveMotorCtrl`

| Signal | Identifier | Scaling | Unit | Level |
|---|---|---|---|---|
| Traction pack voltage | `0x2885` | `[B0:B1] / 100` | V | **measured** |
| Traction pack current | `0x2414` | `signed16 / 20`, negative = charging | A | **measured** |
| Unknown, load-tracking | `0x2429` | none applied | — | **raw** |
| Instantaneous HV power | derived | `pack_v * pack_a / 1000` | kW | **measured** |

This was the project's single largest gap and it is now closed. Measured during
an AC charging session: **388.60 V**, **−20.95 A**, **−8.14 kW**.

Both identifiers come from **unmerged, single-author, BEV3 sources** — weaker
provenance than anything else in this catalog. They are trusted anyway, because
the vehicle and the existing measurements corroborated them:

| Check | Result |
|---|---|
| Magnitude | 388.6 V is a 400 V-class Ultium pack. The 12 V rail read 13.6 V at the same moment, so this is not the low-voltage domain mislabelled |
| Sign convention | current was **negative** while plugged in and charging, exactly as the source states |
| `0x2414` against its own test vectors | `0xFE39 → −22.75 A` and `0x0012 → 0.9 A`, both reproduced exactly |
| **Against a wholly independent measurement** | volts × amps gave **8.14 kW**; the charge power derived from the *energy field's slope* — a different identifier, a different module, a different method — gave **~7.7 kW**. Agreement within 6 %, and in the correct direction: pack DC power exceeds usable energy gain by the conversion and thermal losses |

That last row is the one that matters. Two unmerged sources for a different
platform were confirmed by a measurement taken a completely different way.

Both power figures are kept side by side in the recorder rather than averaged.
Two independent routes to one quantity is what exposed the `0x5401` mislabel;
disagreement between them is a signal worth seeing.

### `0x2429`, and the most convincing wrong answer this project has produced

`0x2429` was allowlisted on 2026-09-03 and reachable from **no profile at all**,
so nothing could ever transmit it — an identifier approved for use and then
never used. Building `hummer_obd.confidence` is what found that.

Sent for the first time on 2026-09-04, it answered `0x5806` = 22534. The
source's `/64` gives **352.09 V**, which across the 96 cells this pack was
[independently shown](PACK_ARCHITECTURE.md) to have in series is **3.6676 V per
cell** — the textbook nominal for an NMC cell, to four significant figures, from
a number nobody had fitted.

**It was decoded as volts, published, and it is wrong.** Three hours later the
recorder had 405 samples instead of one:

| | |
|---|---|
| raw range | 18556 – 26588, **108 distinct values** |
| vs pack current | **r = +0.83** |
| vs HV power | **r = +0.83** |
| vs pack voltage | **r = −0.67** |
| vs state of charge | −0.09 |
| vs energy remaining | −0.08 |
| at rest | ≈ 22350, rising **~16.4 counts per amp** of discharge |

A *rated* figure does not move at all, and a voltage does not rise with the
current drawn from it. Whatever `0x2429` is, it tracks **load** — and it is now
stored raw with no equation applied, exactly like `0x5401`, the other identifier
whose published label this vehicle contradicted. The column is
`field_2429_raw`, not `nominal_pack_v`, because a name is a claim too.

**What made this one dangerous is that it was not a guess.** The 3.6676 V figure
was a genuine structural coincidence: 96 series cells is independently
established, and NMC nominal really is 3.67 V. Every part of the reasoning was
sound except the part that mattered, which was the sample size. This is what
[the confidence levels](../src/hummer_obd/confidence.py) mean by level 2 being
the dangerous one — a plausible number with a confident unit beside it, and one
observation behind it.

---

## 2. Chassis dynamics — module `28` `BSCM-BrakeSystem`

| Signal | Identifier | Scaling | Unit | Level |
|---|---|---|---|---|
| Wheel speed, four corners | `0x4A7A` | one byte each, FL/FR/RL/RR | km/h | **measured** |
| Brake pressure | `0x4A7C` | `(B0 - 10) * 100` | kPa | **read** |
| Steering wheel angle | `0x4C2D` | `signed16 * 0.022` | ° | **read** |
| Lateral acceleration | `0x4C2F` | `signed16 * 0.0015928` | g | **read** |
| Longitudinal acceleration | `0x4C30` | `signed16 * 0.0015928` | g | **measured** |

These carry the **best-evidenced scalings in the project**. The source is not a
stated formula but test fixtures pairing a captured frame with its expected
value, so each equation was *derived from the vectors* and re-checked against
every one — including both signs. A transcription error survives copying but not
arithmetic.

Two of them stopped resting on the vectors alone on 2026-09-04, when this
vehicle was asked to confirm them:

* **`0x4A7A` against legislated PID `010D`** — recorded in the same row, from a
  different module. `r=+0.997` on each of the four corners over 670 moving
  samples spanning 1–130 km/h, mean difference within 0.1 km/h of zero. A vendor
  scaling from an unmerged BEV3 source, confirmed by the standard's own
  measurement.
* **`0x4C30` against the derivative of that same PID** — `r=+0.837` over 1683
  samples, and the magnitudes match: −2.71..+2.60 m/s² derived against
  −3.00..+3.19 m/s² read. The correlation is not higher because the two are
  sampled seconds apart and an eight-second derivative is a smoothed
  accelerometer; that is a sampling limit, not a disagreement.

`0x4C2F` shares `0x4C30`'s scaling and stays **read**. A sibling being confirmed
is suggestive and is not evidence: nothing here measures cornering
independently.

---

## 3. Drive motor controllers — modules `17`, `1D`, `1E`

| Signal | Identifier | Scaling | Unit | Level |
|---|---|---|---|---|
| Module supply voltage ×3 | `0x33E5` | `B0 / 10` | V | **measured** |
| Pack voltage, at all three | `0x2885` | `[B0:B1] / 100` | V | **measured** |

All three answer `0x33E5` independently: 13.2 / 13.1 / 13.1 V. This is the
**12 V domain**, not traction-pack voltage, and must never be presented as one.
Legislated PID `0142` was added on 2026-09-04 as a third independent route to
that rail; the three differ multiplicatively by about 2.4 % and 5.9 % with
ratios stable to half a percent, which is a calibration question between
uncalibrated ADCs rather than a decode error — see [pack
architecture](PACK_ARCHITECTURE.md). That is what promotes this row to
**measured** and what stops it going further.

`0x2885` was proven only at module `17` until 2026-09-03, when asking at
priority `0x18` had `1D` and `1E` answer it too — 382.39 V and 382.37 V against
a `pack_v` of 382.65 V read from `17` in the same minute. Three independent
sources for pack voltage is a cross-check the project did not previously have.
Module `1E` had exactly one identifier ever put to it before that, and that one
a 12 V reading.

---

## 3b. Body control — module `40` `BCM-BodyControl`

**Reachable only at CAN priority `0x18`.** At `0x14` it returns nothing at all,
which is why it was documented as unreachable for a day. See [CAN
priority](CAN_PRIORITY.md).

| Signal | Identifier | Scaling | Unit | Level |
|---|---|---|---|---|
| EVSE advertised current | `0x4149` | none applied | — | **raw** |
| Battery group voltage 1 | `0x416C` | none applied | — | **raw** |
| Battery group voltage 2 | `0x416D` | none applied | — | **raw** |
| Battery group voltage 3 | `0x416E` | none applied | — | **raw** |
| HV battery temperature | `0x434F` | none applied | — | **raw** |
| Battery temperature A | `0x4127` | none applied | — | **read** |
| Battery temperature B | `0x4124` | none applied | — | **raw** |
| Battery coolant temperature 1 | `0x40E5` | none applied | — | **read** |
| Battery coolant temperature 2 | `0x40E6` | none applied | — | **read** |

Every one is **raw**, and the reasons are specific rather than cautious:
`0x416D` and `0x416E` returned identical values, `0x416C` read 2589 and then
2593 a minute apart, and the vehicle was parked and unplugged — the one state
that says least about an EVSE current. Nine payloads and nine open questions.

Names in the first column are the *source's* labels, from OBDb/Cadillac-LYRIQ
PR #14. This vehicle has confirmed only that it answers them.

---

## 4. Standard OBD-II

Priority `0x18`, addressed to module `17` (`18DA17F1`). This section said
"functional broadcast, header `DB33F1`" until 2026-09-04; the recorder was
pointed at one module some time before that and the sentence survived the
change. `live.py` reads the addressing out of `drive.STANDARD_ADDRESS` rather
than describing it, for exactly this reason.

| Signal | Request | Level |
|---|---|---|
| Vehicle speed | `01 0D` | **measured** |
| Odometer | `01 A6` | **measured** |
| 12 V supply, per responding module | `01 42` | **measured** |
| Run time, distance since codes cleared, distance with MIL on, warm-ups | `01 1F` `01 21` `01 31` `01 30` | **read** |
| Malfunction lamp, stored-fault count | `01 01` | **measured** — lamp off, zero faults |
| Stored / pending / permanent DTCs | `03` `07` `0A` | **measured** — zero on this vehicle, and genuinely measured only since 2026-09-04. Every earlier check returned `NO DATA` and was recorded as "no codes", which `NO DATA` does not mean. Addressed to module `45` all three answer positively: `43 00`, `47 00`, `4A 00` — count zero. See [the validation record](VALIDATION.md#dtcs-were-never-actually-read-until-2026-09-04) |
| VIN, calibration IDs, CVNs, module names | `09 02` `09 04` `09 06` `09 0A` | **measured** |
| Freeze frame | `02` | service proven; no frame exists (no DTC) |
| On-board monitor results | `06` | proven; vehicle advertises **zero** monitor IDs |

---

## 5. Node health

12 V connector voltage (`ATRV`, adapter-only, reaches no CAN bus), CAN
transmit/receive counters (`ATCS`), PiSugar2 cell voltage over I²C, Bluetooth
link state, e-paper status panel.

---

## 6. Refused, and what the refusal told us

| Target | Result | What it means |
|---|---|---|
| Seventeen identifiers at module `CD`, at **both** priorities | `7F 22 31` requestOutOfRange | `CD` **is** alive and **does** serve service 22 — it returns a formed UDS refusal, not silence. But it refuses the ISO 14229-1 standard identification identifiers as readily as vendor ones, at `0x14` and `0x18` alike. A module declining `F187` through `F191` is not hiding a namespace behind identifiers nobody has guessed; it exposes nothing in the session it answers in. Asking for a different session is service `0x10`, which this project permanently forbids. `CD` is closed **from this access path**. |
| `F187` `F188` `F189` `F191` at module `45` | `7F 22 31` requestOutOfRange | The gateway's first-ever service 22 requests. Reachable, conversing, and holding none of the four. Nothing else has ever been asked of it, and no sourced identifier for a GM gateway exists in this project. |
| Nine identifiers at module `40`, at priority `0x14` | `NO DATA` | **This was misread.** Nothing replied, and the conclusion drawn was that the module was unreachable. It answers all nine at `0x18`. See [CAN priority](CAN_PRIORITY.md) — the failure was a hardcoded priority in this repository, not the vehicle. |
| `0x4A7A` `0x4C2D` `0x4C2F` at module `28`, at priority `0x18` | `7F 22 11` serviceNotSupported | A response code new to this project. Not "no such identifier" but "not this service, at this priority" — module `28` answers these normally at `0x14`. It is the clearest statement available that priority is part of the addressing. |

---

## 7. Not obtained

Stated as "no sourced identifier found", never as "impossible" — this project
has already been wrong once in that direction.

| Signal | Status |
|---|---|
| Motor RPM, torque, phase current, motor/inverter temperature | absent |
| Per-module battery temperature, coolant, state of health, contactor state | absent |
| Suspension height, rear-wheel steering, CrabWalk state | absent |
| Door, lock, lighting, HVAC state | **absent** — module `40` answers nine identifiers now, but none of them is a body signal, and no sourced identifier for one exists here |
| Gateway/network state (module `45`, never asked) | absent |
| GPS / location | **not an OBD service.** Needs a USB GNSS receiver or the OnStar path. |
| Raw internal CAN-FD | **hardware ceiling.** The MX+ implements Classical CAN only. |
| Lock, unlock, climate, preconditioning | out of scope by design — cloud broker, never this node |

---

## 8. How it is collected

| Mode | Command | Behaviour |
|---|---|---|
| **Service** | `hummer-drive.service` | Records a decoded CSV whenever the vehicle is awake — the column list is `drive.COLUMNS`, deliberately not restated here because a written count has gone stale twice; sends **only `ATRV`** while it sleeps. Rows are flushed and `fsync`ed as taken, so a session survives the vehicle cutting power. Enabled at boot. |
| One-shot | `hummer-obd-drive --confirm --max-cycles N` | Bounded manual session |
| Single identifier | `hummer-obd-enhanced --profile <p> --confirm` | Supervised one-shot, dry run by default |
| Standard only | `hummer-obd-collector` | Unattended collector. **Cannot send service 22.** |
| Offline | `hummer-obd-capabilities` | Report that opens no serial device |

## 9. What it will never do

Service `22` is refused by `validate_command`, the gate the unattended
collector uses, and an **import-time assertion fails the build** if anyone adds
it. Enhanced reads go through a second, narrower gate accepting an exact
enumeration of identifiers — `safety.ENHANCED_READ_DIDS`, rendered into
[enhanced candidates](GM_ENHANCED_CANDIDATES.md) from the gate itself so a
written count cannot drift from it — and refusing everything else,
including the identifiers immediately either side of ones that work. A test
walks all 768 identifiers around the allowlist and asserts the accepted set is
exactly the allowlist.

`04`, `08`, `10`, `11`, `14`, `27`, `28`, `2E`, `2F`, `31`, `34`–`38`, `3B`,
`3D`, `3E`, `83`, `84`, `85`, `87` are permanently forbidden. There is no
runtime bypass, no wildcard, and no identifier sweeping — an ECU asked for
thousands of identifiers in sequence is being probed, and that is not something
this project does to someone's vehicle.
