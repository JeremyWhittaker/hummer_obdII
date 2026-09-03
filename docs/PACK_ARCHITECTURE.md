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

The bytes are preserved. The next useful evidence is state variation the corpus
does not yet contain: an AC charge, a cold start, a hard drive with a hot pack.
Until then they stay raw and unnamed.

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
