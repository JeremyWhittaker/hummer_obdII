# Module map

Every address here was reported by **this vehicle**, not taken from an internet
module list. Each was queried behind its own `ATCRA18DAF1<addr>` receive filter
and answered service 09 PID `0A` for itself, so the pairings are measured rather
than inferred from the order of a concatenated string.

That distinction has already earned its keep once: an earlier version of this
project inferred that address `28` was the gateway, because `28` was the only
module still answering while the vehicle shut down. The per-address query showed
`28` is the brake controller and `45` is the gateway. The inference was
reasonable, was drawn from real observed behaviour, and was wrong.

## The eight modules

| Address | Name reported by the vehicle | Enhanced reads proven here |
|---|---|---|
| `17` | `DMCM-DriveMotorCtrl` | `0x33E5` → 13.2 V |
| `1D` | `DMC2-DriveMotorCtrl2` | `0x33E5` → 13.1 V |
| `1E` | `DMC3-DriveMotorCtrl3` | `0x33E5` → 13.1 V |
| `28` | `BSCM-BrakeSystem` | `0x4A7A` `0x4A7C` `0x4C2D` `0x4C2F` `0x4C30` |
| `40` | `BCM-BodyControl` | **nine proven**, but only at CAN priority `0x18`: `0x4149`, `0x416C`, `0x416D`, `0x416E`, `0x434F`, `0x4127`, `0x4124`, `0x40E5`, `0x40E6`. At `0x14` it returns nothing at all — see [CAN priority](CAN_PRIORITY.md) |
| `45` | `Gateway Module - GWM` | asked four ISO 14229-1 identification identifiers at `0x18`; `7F 22 31` to all four. Reachable, holds none of them |
| `CB` | `BSM-BatterySysMngr` | `0x27C6` `0x27AF` `0x27C7` `0x27C0` `0x0046` `0x5401` `0x2AF5` `0x2B43` |
| `CD` | `BSM-BatterySysMngr` | **refuses CB's identifiers** — see below |

## Addressing

Enhanced diagnostics on this platform do not use the legislated header form.

```
request    0x14 DA <ecu> F1        e.g. 0x14DACBF1
response   0x14 2A F1 <ecu>        e.g. 0x142AF1CB
```

`ATSH` carries only the low three bytes, so the priority byte must be set
separately with `ATCP14`. Without it the request leaves as `0x18DA…` and no
module answers. Legislated OBD is the opposite: a functional broadcast at
priority `0x18`, `ATCP18` + `ATSHDB33F1`.

## What the three drive motor controllers showed

All three answer `0x33E5`, and they answer with *different* values:

```
17  DMCM   0x84  13.2 V
1D  DMC2   0x83  13.1 V
1E  DMC3   0x83  13.1 V
```

Small differences matter here. If all three had returned an identical byte, the
reading might have been a shared constant or a gateway artefact. A 0.1 V spread
across three modules is what independent measurements of the same rail look
like, which is evidence that each controller is answering for itself.

This is a **12 V domain** reading, not traction-pack voltage. The source labels
it "DMCM battery voltage" and 13.1 V is nowhere near a pack measurement, so it
must not be presented as one.

Pack voltage **was** obtained later the same day: `0x2885` at module `17`, and
(asked at priority `0x18`) at `1D` and `1E` as well. This paragraph said it
remained unobtained for a day after it stopped being true.

## What module CD showed, and why it is the most interesting result

`CD` is a second `BSM-BatterySysMngr`. Every enhanced identifier proven on `CB`
was put to it once:

```
2227C6  ->  142AF1CD 03 7F 22 31
2227AF  ->  142AF1CD 03 7F 22 31
222AF5  ->  142AF1CD 03 7F 22 31
222B43  ->  142AF1CD 03 7F 22 31

**Superseded in part on 2026-09-03**: `CD` was retested at CAN priority `0x18`
as well and refuses there too, so the conclusion below holds — but module `40`,
recorded elsewhere in this file as silent, turned out to be answering at `0x18`
all along. See [CAN priority](CAN_PRIORITY.md) for the full matrix.

On 2026-09-03 it was asked thirteen more: the four CB-proven identifiers left
out above (27C7, 27C0, 0046, 5401), the five discovered at CB that day (27BF,
27BB, 27B5, 2709, 2AF1), and four ISO 14229-1 standard identification DIDs
(F187, F188, F189, F191).  Seventeen identifiers now, one answer:

  everything  ->  142AF1CD 03 7F 22 31

A module that refuses the standard identification identifiers as readily as
vendor ones is not hiding a namespace behind identifiers nobody has guessed.
It is exposing nothing in the session it answers in, and the way to ask for a
different session is service 0x10, which this project permanently forbids.
CD is reachable, alive, and closed from this access path.  See
docs/PROBE_2026-09-03.md.
```

`7F 22 31` is `requestOutOfRange`. Read carefully, that is not a failure — it is
the most informative negative available:

* **The module is alive and serving service 22.** It returned a properly formed
  UDS negative response on its own response identifier, not silence and not a
  timeout. A module that did not implement service 22 would answer `7F 22 11`;
  one that was asleep or unreachable would answer nothing at all.
* **It does not hold CB's identifiers.** So the two battery managers are *not*
  mirrors of each other. Whatever `CD` is for — a second pack section, a
  different role in the same pack — it has its own identifier set, and none of
  it is known.

That answers a question worth asking and opens a better one. `CD` is a proven
service-22 responder whose entire identifier space is undiscovered.

## Where sourced identifiers should be aimed next

Module identity is not permission to guess identifiers. It only says where a
*sourced* one should be directed. In priority order:

| Module | What would live there | Status |
|---|---|---|
| `CB` / `CD` | state of health, contactor state | `CD` refuses everything at both priorities, including the ISO standard set — closed from this access path |
| `17` / `1D` / `1E` | motor RPM, torque requested and delivered, motor and inverter temperature, regen power | one identifier proven, and it is a 12 V reading |
| `28` | already productive — wheel speeds, brake pressure, steering, accelerations | five identifiers proven |
| `40` | door and lock state, lighting, HVAC state | nine identifiers proven at priority `0x18`, none of them a body signal |
| `45` | gateway — routing and network state | reachable at `0x18`; holds none of the four ISO identification identifiers, the only things ever asked of it |

## The rule that does not bend

An identifier is added only when a fetchable public source names it exactly.
Proximity to a working identifier is not evidence: `0x27C7` was once used in
this project's own tests as an example of a *fictional* identifier "a sweep
would try next", precisely because it sits one step from `0x27C6`. It is real.
It is the range signal, and it is now proven on this vehicle.

Nearness proved nothing in either direction, which is exactly why the rule is
enumeration from a source and never distance from something that worked.
