# What the vehicle's own data says about its battery pack

Everything here is derived from the recorded sessions in `evidence/sessions/`
and the raw transcript on the node. Nothing was looked up and then presented as
measured. Where a figure is inference rather than measurement it says so, and
where the evidence runs out it stops.

Reproduce any of it from a checkout: the sessions are committed.

## 96 cells in series

`pack_v` (`0x2885`, pack power module `0x17`) divided by `cell_avg_v`
(`0x2AF5`, battery manager `0xCB`) is the number of cells in series. These are
two different identifiers read from two different modules in the same cycle, so
their ratio is not an artefact of one decoder.

| | |
|---|---|
| samples | 297 |
| mean | **95.991** |
| standard deviation | 0.041 |
| min / max | 95.752 / 96.343 |

Ninety-six. The deviation is well inside what 0.01 V pack resolution and
0.0001 V cell resolution can explain, and the value does not drift with state
of charge, temperature, or whether the vehicle is charging or discharging.

## Usable capacity, and why both decoders can be trusted

`energy_kwh` (`0x27AF`, scaled `/100`) divided by `soc_pct` (`0x27C6`, scaled
`/655.35`) is the pack capacity the vehicle itself is working from. Two
different identifiers, two different scalings, so if either were wrong the ratio
would drift as state of charge changed.

It does not drift. Across the sessions where state of charge actually moved,
78.85 % to 89.65 %:

| session | samples | implied capacity | SoC range |
|---|---|---|---|
| 0415 | 168 | 191.915 kWh | 78.85–80.05 % |
| 0437 | 94 | 191.909 kWh | 80.05–80.85 % |
| 0449 | 31 | 191.944 kWh | 80.85–81.25 % |
| 0453 | 172 | 191.872 kWh | 81.25–82.85 % |
| 0516 | 9 | 191.879 kWh | 82.85 % |
| 0518 | 517 | 191.839 kWh | 82.85–89.65 % |

**191.84 to 191.94 kWh — a spread of 0.05 %** across an eleven-point swing in
state of charge. Two independently scaled fields holding a constant ratio that
tightly is strong evidence that both scalings are right, and it puts the pack's
usable capacity, as this vehicle computes it, at **about 191.9 kWh**.

The sessions where state of charge sat pinned at one value (1525 at 89.653 %,
1608 at 86.647 %) give 192.60 and 189.82 and are not counted above: with the
numerator moving and the denominator quantised, the ratio there measures the
quantisation, not the pack.

## The charge curve

Mean cell voltage against state of charge, over 1247 samples:

| SoC | mean cell V | | SoC | mean cell V |
|---|---|---|---|---|
| 79.0 % | 4.0059 V | | 85.0 % | 4.0662 V |
| 80.0 % | 4.0212 V | | 86.0 % | 4.0757 V |
| 81.0 % | 4.0294 V | | 87.0 % | 4.0815 V |
| 82.0 % | 4.0414 V | | 88.0 % | 4.0896 V |
| 83.0 % | 4.0486 V | | 89.0 % | 4.0943 V |
| 84.0 % | 4.0595 V | | | |

Monotonic, at **7.58 mV per percent** (correlation +0.974). Extrapolated to
100 %, that gives **4.1746 V per cell**, and at 96 cells in series, **400.8 V**
at the top of the pack.

That is a third independent corroboration of the 96-series count. A 400 V-class
pack reaching 400.8 V at full is the right answer, and 4.17 V is a physically
correct full-charge voltage for this chemistry — high, but below the ~4.2 V
ceiling, which is where a manufacturer holding a little margin for longevity
would stop. None of that was assumed; it falls out of a curve measured between
79 % and 89 %.

Two buckets sit below the trend (86.5 % and 89.5 %) and both come from sessions
where state of charge was pinned while the vehicle drove. Voltage sags under
load and state of charge did not update, so those points measure sag rather than
the curve.

## `0x2B43` is twenty-six per-module values

The recorder stores this identifier's 26 bytes as an undecoded hex string,
because the source that named it described a single value. It is not a single
value.

Across 1288 samples, **every one of the 26 positions correlates with
`soc_pct` at +0.995 or better** (position-by-position, the range is +0.9948 to
+0.9970). They are 26 parallel measurements of the same quantity, not a mixed
record with a few charge-related fields in it.

They are also not temperature: `temp_f` fell from 114.8 °F to 91.4 °F across
the corpus while these values *rose*, and the correlation with temperature is
−0.67 against +0.997 for charge.

### The array has structure

| positions | count | behaviour |
|---|---|---|
| 0–1 | 2 | sit ~1.5 units below the rest in every sample |
| 2–13 | 12 | tight: mean internal spread **0.098** |
| 14–25 | 12 | a second group, mean internal spread **0.300**, sitting ~0.5 lower |

The mean spread *across* positions 2–25 is **0.423**, which is **2.1×** the mean
spread *within* either block. If those 24 values were one undifferentiated set,
those two numbers would be equal. They are not, so the 24 are two groups of
twelve. Block A reads above block B in 64% of samples, with the difference
averaging +0.53 units and a standard deviation of 0.59 — two groups that track
each other closely and cross occasionally, which is what parallel strings do.

### What that implies, and how confident to be

Twelve modules in series at eight cells each is 96 cells in series, and 96.0 is
exactly what the voltage ratio measures independently. Two blocks of twelve is
also how a 400 V / 800 V switchable pack is arranged. Those three facts agreeing
is the reason this reading is worth writing down.

It is still a reading. What is **measured** is: 96 series cells, 26 values that
all track charge, and two statistically distinct blocks of twelve. What is
**inferred** is that a block is a parallel string and a value is a module.
Nothing here needs the inference to be true.

A linear fit of the mean of the 26 values against `soc_pct` gives
`soc ≈ 0.439 × value − 7.13`, mean residual 0.20 %, but it is calibrated only
across 78–90 % state of charge. That is far too narrow a window to publish as a
scale, which is why the live view reports these as raw values and their drift
from their neighbours rather than as percentages.

## `0x2AF5` returns ten bytes; six were being read

Measured over 1315 reassembled replies, this identifier always answers with
**ten** bytes. The decoder read the first six — average, minimum and maximum
cell voltage — and discarded the rest, so four bytes per sample never reached
the CSV and no later analysis could recover them.

They are now preserved raw in `cell_extra_raw`. What they are is not known, but
they do not behave like measurements:

| byte | range | distinct values in 1315 samples |
|---|---|---|
| 6 | 97–190 | 23 |
| 7 | 13–24 | **7** |
| 8 | 177–184 | 8 |
| 9 | **23** | **1 — constant** |

A field that never changes across 1315 samples is not a reading. Small bounded
integers sitting next to a reported minimum and maximum look like **indices** —
which cell is weakest, which is strongest. If that is what they are, this
vehicle can name the cell and not merely its voltage, which would be a real jump
in granularity.

### The index hypothesis was tested and is refuted

`0x2AF5` and `0x2B43` are read back to back from module `CB` in the same cycle,
so adjacency in the transcript pairs them by cycle. Over **1314 paired cycles**:

| test | result |
|---|---|
| byte 7 == index of the array's minimum | **0 / 1314** |
| byte 7 == index of the array's maximum | 41 / 1314 (3.1 %, chance) |
| byte 9 == index of the array's maximum | **0 / 1314** |
| byte 9 == index of the array's minimum | **0 / 1314** |

Not weakly supported — *never true*. Byte 7 is not an index into this array.
The array's minimum sits at position 0 in almost every sample, while byte 7 is
15 in 966 of them; those two facts simply do not meet.

That does not rule out the bytes being indices into something else. Ninety-six
cells means valid indices 0–95, and both 13–24 and a constant 23 fit inside that
range comfortably. It rules out one specific, testable version of the idea,
which is what a hypothesis is for.

### What the four bytes do correlate with: not much

Against every quantity this node already measures, over 1315 samples:

| field | vs cell avg | vs cell min | vs cell max | vs cell spread |
|---|---|---|---|---|
| byte 6 | +0.34 | +0.34 | +0.34 | −0.45 |
| byte 7 | +0.34 | +0.34 | +0.34 | −0.46 |
| byte 8 | −0.46 | −0.47 | −0.46 | **+0.55** |
| u16(8,9) | −0.46 | −0.47 | −0.46 | **+0.55** |

Nothing reaches 0.6. Read as `/10000` volts like the three fields beside them,
`u16(6,7)` spans 2.48–4.87 V and `u16(8,9)` spans 4.53–4.71 V, against a real
cell range of 4.00–4.10 V — so they are not more cell voltages on that scale.

Weak correlation with everything already measured is itself informative: it is
what a field carrying information this node does **not** otherwise have looks
like. Byte 8's +0.55 against cell spread is the strongest thread, and it has a
physical story — a hotter pack spreads further — with `(byte8 − 40) / 2` landing
at 68.5–72 °C across the corpus, which is a plausible coolant or module
temperature. That is a story, not a result, and the corpus covers one warm day
with no charging session in it.

### A drive supplied the state variation, and it changed the picture

The paragraph above used to end by saying the missing evidence was state
variation the corpus did not contain. A highway drive on 2026-09-03 supplied
some, and it is worth more than the whole parked corpus was:

| state | bytes 6–9 | as decimal |
|---|---|---|
| parked, pack near idle | `74 0F B3 17` | 116, **15**, 179, **23** |
| 117 km/h cruise, 40.9 kW | `76 0F B8 17` | 118, **15**, 184, **23** |
| hard acceleration, **97.8 kW** | `76 0F B4 17` | 118, **15**, 180, **23** |
| later in the same drive | `78 0F B3 17` | 120, **15**, 179, **23** |

Bytes 7 and 9 held at **15 and 23** through all of it — a parked pack, a
highway cruise, and a 97.8 kW pull with the pack sagging from 384.88 V to
381.73 V under 256 A. A value that does not move across that range is not
measuring anything about it.

That also partly revives the hypothesis the section above refuted. What was
disproved is that byte 7 indexes *this array*. If 15 and 23 instead index
**cells or modules** — "cell 15 is the weakest, cell 23 is the strongest" —
then holding constant is exactly what they should do, because the same cells
stay weakest and strongest from one moment to the next. Bytes 6 and 8 would
then be those cells' associated values, and they *do* move: 116 → 120 and
179 → 184 across the same states.

That is a live hypothesis, not a result. Testing it needs the two indices to be
checked against something that resolves individual cells, which nothing read
here does. It is recorded so the next person does not have to rediscover that
two of these four bytes are frozen.

## `0x5401` is not charger power

The decoder kept this byte raw with a note saying it would stay that way "until
a charging session gives it a reference." The recorded corpus contains one, and
it settles the question in the negative.

| evidence | value |
|---|---|
| correlation with pack current | **−0.09** over 1907 paired rows (see the correction below) |
| value while charging | 147–152 |
| power range across those samples | **1.85 – 16.51 kW** |
| value while not charging | zero in **227 of 254** samples |

So it is certainly tied to charging — and just as certainly not power, because a
ninefold change in measured power moves it by at most five counts.

### Correction: the −0.81 was an artefact of the corpus

This table read **−0.81 over 297 paired samples** until `hummer-obd-decode`
existed to re-derive it. It does not hold. Over 1907 paired rows the correlation
is **−0.09**, and restricting to the same early sessions the original used gives
−0.09 as well.

The original figure was computed when almost everything recorded was a parked or
charging vehicle, with pack current spanning −22 to +105 A. `0x5401` sat near
150 while charging and 0 otherwise, and pack current was negative while charging,
so the two moved together. Once real driving entered the corpus — pack current
reaching **+836 A** while this byte stayed at zero throughout — the relationship
collapsed. It was measuring what the corpus happened to contain, not a property
of the signal.

**The conclusion it supported is unchanged and was reached by other evidence:**
the plateau at 147–152 across a ninefold power range, the zero in 227 of 254
non-charging samples, and the monotonic decay to zero after a charge ended. Those
are what establish that `0x5401` tracks charging state and is not power. The
correlation was never load-bearing — it was quoted as though it were.

This is the first thing the decode tool did, and the reason it was built: a
number that no one can recompute is a number no one can check.

The decisive evidence is the end of a charge. Over three and a half minutes,
with state of charge flat at 89.653 %, energy flat at 172.03 kWh, and pack
current at zero — that is, with charging already finished — the byte decayed
monotonically to zero:

```
07:00:01   36        soc 89.653   energy 172.04   pack_a  0.0
07:00:16   33        soc 89.653   energy 172.04   pack_a -0.55
07:00:30   30        soc 89.653   energy 172.04   pack_a  0.0
07:01:18   26        soc 89.653   energy 172.03   pack_a  0.3
07:01:43   23        soc 89.653   energy 172.03   pack_a -0.25
07:02:10   20        soc 89.653   energy 172.03   pack_a  0.0
07:02:43   16        soc 89.653   energy 172.03   pack_a  0.0
07:03:10   13        soc 89.653   energy 172.03   pack_a  0.0
07:03:30    6        soc 89.653   energy 172.03   pack_a  0.0
07:03:37    0        soc 89.653   energy 172.03   pack_a  0.0
```

A plateau while working and a slow ramp to zero after the work stops is what a
**demand or duty signal** looks like — a pump or fan winding down, or a
thermal-management output. It is not battery temperature: that correlation is
−0.25, and a temperature does not decay to zero.

One gap is worth stating rather than glossing: no value between 37 and 146 was
ever observed. The drop from the charging plateau to the start of that ramp
happened faster than the sample interval, so the shape between them is
unmeasured.

It stays raw. "Tied to charging and shaped like a duty cycle" is not a unit, and
naming a column after it would be inventing one. But the open question the code
posed is now answered: whatever `0x5401` is, it is not charger DC power.

## Range, energy and charge agree with each other and with the EPA figure

`range_mi` (`0x27C7`) is a third battery field, and it can be checked against
the two already trusted:

| ratio | mean | standard deviation | implies |
|---|---|---|---|
| `range_mi / soc_pct` | 3.3308 | 0.0124 | **333.1 mi** at 100 % |
| `range_mi / energy_kwh` | 1.7364 mi/kWh | 0.0043 | the vehicle's own efficiency assumption |

Those two are independent routes to the same number, and they land on the same
answer: 1.7364 mi/kWh × 191.9 kWh usable = **333 mi**, against 333.1 mi from the
state-of-charge route. The published EPA rating for this vehicle is 329 mi, so
the truck's own arithmetic sits **1.2 %** above its sticker.

That is four fields — state of charge, energy, range, and cell voltage — all
mutually consistent, which is a much stronger statement than any one of them
being individually plausible.

Worth noting for its own sake: **1.74 mi/kWh** is the efficiency this vehicle
assumes when it converts remaining energy into remaining miles.

## The 12 V rail, measured two ways

`volts` comes from the adapter's `ATRV`, which never touches the CAN bus.
`dmc2_v` comes from `0x33E5` at drive motor controller `1D`. They are entirely
independent measurements of the same rail, and over 774 paired samples they
correlate at **+0.955** — which on its own validates the `byte / 10` scaling on
`0x33E5`, since a wrong scale would not track.

They do not agree on the value. The adapter reads consistently higher:

| relationship | mean | standard deviation | relative spread |
|---|---|---|---|
| `ATRV − module` | 0.785 V | 0.046 | 5.8 % |
| `ATRV / module` | 1.0603 | 0.0038 | **0.36 %** |

The ratio is the tighter of the two, which points at a scale difference rather
than a fixed offset — an `ATRV` reading about 6 % high would explain it, and
ELM-style adapters are roughly calibrated for this by default. But the honest
statement is that **over the observed range of 12.9–13.9 V the two models cannot
be told apart**: a constant offset and a constant ratio predict nearly the same
values across one volt of span. Distinguishing them needs a session with a much
wider voltage swing.

This matters for `WAKE_VOLTS`, which is applied to `ATRV` readings. The
threshold is self-consistent because it is compared against the same source it
was measured from, so nothing is wrong today. But if the adapter really does
read ~6 % high, the true rail voltage while this vehicle drives is nearer 12.3 V
than 13.0 V, and any future threshold derived from a module reading rather than
from `ATRV` must not reuse the same number.

## What this node can and cannot see

| granularity | available |
|---|---|
| pack voltage, current, power | yes, every sample |
| all 96 cells: average, minimum, maximum, spread | yes — the envelope, typically 2–5 mV |
| 26 per-module values, individually | yes, since `0x2B43` is broken out in `hummer-obd-live` |
| each of the 96 cells individually | **no** — nothing read here exposes them |

Per-cell voltages for all 96 would need an identifier from a fetchable source
that names it. This project does not sweep for identifiers and does not guess
them: the gate refuses `0x27C5` and `0x27C7`, one step either side of one that
works. Findable, perhaps. Not by guessing.

## Cell format

The measurements above say nothing about cell chemistry or physical format, and
this section is therefore knowledge rather than evidence: the Ultium pack uses
large-format pouch cells, not cylindrical 18650s.

The measurements do make 18650s implausible, which is worth stating because it
is the one part of this that the data can speak to. An 18650 holds roughly
3.5 Ah. A pack of this energy built from them would need on the order of 16,000
cells, and at 96 series positions that is about 170 in parallel per position —
not how anyone packages a vehicle pack. Large-format cells at 100 Ah or more are
consistent with 96 series positions; 18650s are not.

Arithmetic from there, with assumptions stated: at roughly 212 kWh gross across
24 modules, a module holds about 8.8 kWh; eight cells in series at ~3.7 V is
about 30 V per module, so about 300 Ah per module, so roughly three ~100 Ah
pouch cells in parallel per series position — on the order of 576 cells. Every
step of that is arithmetic on figures this node did not measure. Treat it as an
estimate, not a result.
