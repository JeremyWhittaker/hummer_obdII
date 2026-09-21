# What each module answered, and what can be done with it

The first full service-22 pass ([DEEP_SCAN.md](DEEP_SCAN.md)) asked 7,424
identifiers across six modules and got 912 answers. This note is the per-module
reading of that result: what each module holds, what shape it is in, and the
cheapest honest route from a payload to a signal.

**Nothing here is a decoded signal.** A hit is a payload at a known module in a
known state. Identifiers and payload *sizes* appear below; values do not, and
values stay on the node.

## The three routes from a hit to a reading

1. **Arithmetic self-check — no vehicle time.** An array must agree with a
   scalar the project already trusts: per-cell voltages must reproduce the
   `0x2AF5` min/max/average, per-module temperatures must bracket the pack
   temperature, a sum must make the pack voltage.
2. **Correlation against the recorded corpus — no vehicle time.** A candidate
   must track something already validated across drives and charges: pack
   current, wheel speed, state of charge, coolant temperature.
3. **A deliberate state change — minutes of an operator's time.** Doors,
   locks, lights, climate, drive modes: change one thing, read the candidate
   set before and after, keep what flips and flips back.

Route 3 is the only one that needs a person, and it is the only one that works
for body signals. Routes 1 and 2 can run unattended against evidence already
captured.

## Module `CB` — battery system manager (357 hits, 2 locked)

Scanned `2700–27FF`, `2800–28FF`, `2A00–2AFF`, `2B00–2BFF`. The richest module
on the truck, and the most structured: 54 payloads are larger than 32 bytes,
including `2B74` at **448 bytes**, four at 224 bytes (`2B65`, `2B66`, `2B67`,
`2B72`), and runs of `2AE1–2AF0` (16 × 36 B) and `2B48–2B56` (15 × 45 B).
Two 24-long runs of single bytes (`276E–2785`, `2793–27AA`) match the pack's
module count.

*What it is worth:* per-cell and per-module pack detail — the project's
longest-standing gap. The pack is **~96 cells in series** (the `0x2AF5` summary
reads 3.9952–3.9986 V per cell, average 3.9963, spread 3.4 mV; 96 × 3.9963 =
383.6 V against a measured 383.9 V).

*Work available now (route 1):* every array was tested against the `0x2AF5`
summary across 1- and 2-byte elements, both endians, three scalings and three
offsets. **No captured array reproduces it**, so the per-cell array is not in
the four ranges scanned. Next: the rest of `CB`'s identifier space, and module
`CD` (the second battery manager, never scanned). A warning from this pass:
`0x2AF1` appeared to be a 12-element voltage array summing to the pack voltage,
and is already known to be **temperatures** — the "match" was manufactured by
an offset the search was allowed to invent.

## Module `40` — body control, and this platform's charging data (291 hits)

Scanned `4000–43FF`. Mostly small values: 203 single bytes, 38 two-byte, and
five payloads over 32 bytes (`418E` at 170 B, `418F` 55 B, `418D`/`4190`/`4191`
40 B). Ten runs of consecutive single bytes, including `41A5–41AE`,
`4255–4264` (16 long) and `434A–4354`.

*What it is worth:* the only module where **body state** has been demonstrated,
and the one carrying the LYRIQ-documented charging and pack-temperature
identifiers this project already reads.

*Work available now:* route 3 has already produced four leads — `423D`/`423E`
follow the driver's door, `41A9`/`41B5` follow the climate fan, with `4120` and
`41AC` probably analogue climate values. One repeat experiment promotes them.
Route 1 applies to ~15 identifiers whose Bolt labels and 1-byte size agree; see
[SOURCING_2026-09-18.md](SOURCING_2026-09-18.md).

## Modules `17`, `1D`, `1E` — the three drive units (95 / 70 / 66 hits; 11 / 11 / 9 locked)

Scanned `2400–24FF`, `2700–27FF`, `2800–28FF`, `2A00–2AFF`, `3300–33FF`,
`5400–54FF` on each. The three answer a nearly identical map — `2739` at 32 B,
`27AB` at 16 B and the `28EF–28FB` block of thirteen 2-byte values on all three
— which is what three instances of one controller family should look like, and
a useful consistency check on the scan itself.

**All 31 security-locked identifiers live here**, in one band: `27AC`, `27AD`,
`27DC`–`27E2`, plus `27E9`/`27EC` on `17` and `1D`. They answer `7F 22 33`,
which means the data exists and needs a security unlock this project will never
send. Recording where they are is the whole value: no further attempt is made.

*What it is worth:* motor speed, torque, inverter and stator temperature — the
headline gap. *What it costs:* these cannot be confirmed parked, because the
quantities are all zero when the truck is not moving. Confirming them needs a
supervised drive experiment reading a small named candidate set at low rate,
which is a deliberate change to the rules in [SAFETY.md](SAFETY.md) and
[DEEP_SCAN.md](DEEP_SCAN.md) and needs the owner's approval first. Until then
these 231 hits stay parked evidence.

## Module `28` — brake system / chassis (33 hits)

Scanned `4A00–4CFF`. Sparse, and the least structured: 16 two-byte values, 11
single bytes, one 10-byte (`4C2C`) and a `4C15–4C1B` run of seven 2-byte
values.

*What it is worth:* suspension height, rear-steer and CrabWalk, brake state.
A seven-long run of equal-width values is the shape of per-corner data.

*Work available now:* route 2 — the recorder already logs four wheel speeds, so
a per-corner candidate can be correlated against the existing drive corpus with
no new vehicle traffic. Route 3 suits ride-height changes, which are operator
selectable while parked.

## What has not been scanned

`CD` (second battery manager) and `45` (gateway) have only a single reachability
identifier each. `CD` is the strongest remaining candidate for the per-cell
array. Beyond that, every module has identifier space outside the ranges the
section 6 order prioritised; a full `0000–FFFF` on all eight would be roughly
524,000 reads, about 350 hours at this link speed, so it stays targeted.

## Recommended order

1. **Scan `CD`, then wider `CB` ranges** — parked, unattended, hunting the
   per-cell array. Each session runs until the truck leaves ready (~40 min).
2. **Run routes 1 and 2 across every array hit** — no vehicle time, using
   evidence already captured plus the borrowed-label checklist.
3. **Repeat the door/climate experiment** — five minutes, promotes four body
   signals into the recorder and dashboard.
4. **Draft the supervised drive experiment** for the drive-unit candidates, for
   the owner to approve before anything is read while moving.
