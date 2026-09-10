# CAN priority is per module, and assuming otherwise hid a module for a day

GM's extended diagnostic addressing puts a priority byte in front of the target
and tester addresses. This project found `0x14` first, because that is what the
battery system manager answers at, and then used it for everything.

That was wrong, and it cost the body control module. On 2026-09-03 module `40`
was probed thirteen times, drew `NO DATA` every time, and was documented as
unreachable — while answering perfectly well at a priority nothing had tried.

## The measured matrix

Every module this vehicle names for itself, asked at both priorities with
identifiers already proven at it where any existed, and with ISO 14229-1
standard identification identifiers where none did:

| Module | Name | `0x14` | `0x18` | Service 01 support bitmap |
|---|---|---|---|---|
| `17` | DMCM-DriveMotorCtrl | answers | **answers** | `01 0D 1C 1F 20 21 30 31 40 42 60 80 A0 A6` |
| `1D` | DMC2-DriveMotorCtrl2 | answers | **answers** | `01 20 40 42` |
| `1E` | DMC3-DriveMotorCtrl3 | answers | **answers** | `01 20 40 42` |
| `28` | BSCM-BrakeSystem | answers | **`7F 22 11`** | `01 20 40 42` |
| `40` | BCM-BodyControl | **`NO DATA`** | answers | `01 20 40 42` |
| `45` | Gateway Module - GWM | not tried | `7F 22 31` | `01 20 40 42` |
| `CB` | BSM-BatterySysMngr | answers | **answers** | `01 20 40 42` |
| `CD` | BSM, second address | — | — | `01 20 40 42` |

## Legislated does not mean dependable

Module `17` advertises fourteen service 01 PIDs and answers them -- while the
truck is moving. Parked, it mostly does not. Measured across two sessions on
2026-09-08 and 09, PID `0142` (control module supply voltage) landed in 100% of
moving samples (284/284 and 91/91) and 22-27% of stationary ones (48/178 and
31/138). A third session on 2026-09-10 caught eleven parked HV-awake samples
and `0142` answered none of them, while the enhanced identifier `0x33E5` --
the same rail, the same module -- answered all eight it was asked for.

That is worth stating because it is backwards from the intuition. The
legislated service is the one with a standard behind it, and it is the one
that goes quiet when the driveline does. Service 22 keeps answering. Any
reading this project wants while the vehicle sits should come from the
enhanced route, and the legislated one should be treated as a moving-vehicle
measurement that happens to sometimes work at rest.

## First result from the second route, 2026-09-10

Nine samples of a moving vehicle, 381.55-389.93 V, 0.6-412.2 A.

**Pack voltage agrees.** Modules `17` and `1D`, asked for `0x2885`
independently over 57 paired samples, differ by a mean of **+0.032 V** --
0.008% of a 386 V reading. The measurement the project calls its headline now
has a second ECU behind it.

The scatter is the interesting part, because it identifies itself. Deltas run
-1.43 V to +2.74 V, sd 0.682, worst case 0.706% of reading, and seven of the
fifty-seven samples sit more than a volt apart. That looks alarming until you
ask *when*:

| Current slewing | mean absolute delta |
|---|---|
| faster than median | 0.644 V (n=28) |
| slower than median | 0.311 V (n=28) |

The disagreement **doubles when current is changing fast**, and the largest
delta of all -- +2.74 V -- lands at -33.25 A, a regen crossing, which is the
steepest slew a drivetrain produces. Two ECUs sampling the same busbar
milliseconds apart during a 350 A swing will not return the same number, and
the size of the gap tracks how fast the busbar is moving.

That is the signature of sampling skew, and it is *not* the signature of a bad
decode. A wrong scaling is systematic: it biases the mean, and it grows with
the reading rather than with its derivative. This bias is +0.032 V on 386 V,
essentially zero, with symmetric scatter either side of it.

An earlier version of this section published a mean of +0.061 V and a worst
case of 0.403% from the first nine samples. The mean tightened and the worst
case nearly doubled, which is what a nine-sample extreme normally does. The
figures above are the fifty-seven-sample ones.

**The 12 V rail does not agree, and that is the interesting one.** At the same
module, PID `0142` reads a mean 13.513 V and `0x33E5` reads 13.167 V -- an
offset of +0.346 V with a standard deviation of 0.041. That is not noise and
not rounding; `0x33E5` has 0.1 V resolution, so the gap is three and a half
counts wide and holds steady.

The project had seen a version of this before, comparing `0x33E5` at module
`1D` against PID `0142` at module `17`, and could not tell a misdecoded field
from two modules genuinely sitting at different points on a harness. Asking
module `17` for both removes that confound entirely -- one module, one rail,
two routes -- and the offset survives it. So it is not a wiring gradient
between modules.

### Offset or scale? Not answerable from a moving vehicle, and here is why

The obvious next question is whether `0x33E5` is out by a constant or by a
factor. It cannot be settled with this data, and the reason is worth writing
down because it will be the reason next time too.

The rail does not move enough. Over 39 paired samples PID `0142` spans
0.286 V -- **smaller than the 0.359 V gap being explained**. Fit both models
and compare them the only fair way, as residuals in volts:

| Model | Residual sd |
|---|---|
| offset, `y = x - 0.3594` | 0.0447 V |
| scale, `y = 0.97329 x` | 0.0448 V |
| `0x33E5` quantisation floor (0.1 V step) | 0.0289 V |

They differ by one ten-thousandth of a volt, and both sit at about 1.5x the
resolution floor -- which is to say both fit as well as an 0.1 V field can be
made to fit anything. Over a span narrower than the offset itself, a constant
and a factor draw the same line.

This is the trap `confidence.CONFIDENCE['2429']` records in prose, now with
numbers on it. Compare the two by relative spread instead and the ratio looks
**36 times tighter** -- sd/|mean| of 0.0034 against 0.1243 -- entirely because
one mean sits near 1.0 and the other near 0.36. That is a property of the
denominators, not of the vehicle, and it is how an earlier version of this
project talked itself into "the differences are multiplicative".

What would settle it is a state where the rail genuinely swings: DC-DC off at
rest near 12.4 V against DC-DC active near 13.9 V is roughly 1.5 V, four times
the offset, and enough to separate the models cleanly.

There is a catch, and it is the finding from the section above turned against
us: **the state that would answer this is the state where PID `0142` stops
answering.** Service 01 sleeps with the driveline. Eleven parked HV-awake
samples produced zero `0142` readings while `0x33E5` produced eight. Pairing
the two across a wide rail swing therefore needs the narrow window where the
vehicle is awake enough for service 01 and the DC-DC has not yet settled --
key-on, before drive. The recorder already crosses that boundary on every
session it opens; nothing new needs to be asked of the vehicle. It is a matter
of accumulating the transitions rather than of instrumenting anything.

### 0x2885 is settled: no scale error, no meaningful offset

The truck opened its HV contactors on the way to sleep, and pack voltage
collapsed from 394 V to under 1 V. That is the measurement the 12 V rail could
not provide: a span of **393.66 V against a 0.0405 V offset**, separable by a
factor of nine thousand seven hundred.

| State | module `17` | module `1D` | delta |
|---|---|---|---|
| contactors open (n=10) | 4.678 V | 4.532 V | -0.146 V |
| contactors closed (n=117) | 388.464 V | 388.520 V | **+0.056 V** |

**The delta does not scale with the reading**, which is the whole test. A scale
error of the size seen at 4.6 V would put the 388 V delta at roughly -12 V. It
is +0.056 V. Fit a factor and it comes out at **1.000147** -- one part in
seven thousand, and indistinguishable from unity at this resolution. Fit an
offset and it is 0.0405 V, four counts of a field whose step is 0.01 V, and
0.01% of a working reading.

A small second benefit, noted rather than claimed: across 200 samples module
`1D` answered once when module `17` did not -- at t=1220 s, mid sleep-settle,
where `pack_v` and `pack_a` were both empty and `pack_v_1d` read 1.13 V -- and
there was no sample the other way round. One row out of two hundred proves
nothing about relative reliability, but it does show the redundancy covering a
dropout as well as checking a decode, which was not part of the argument for
adding it.

So the two modules do not merely agree at one operating point. They return the
same number across essentially the entire dynamic range of the field, in both
the state where the pack is connected and the state where it is not.
`0x2885` is confirmed.

Worth noting against the section below it: this is the same analysis that
could *not* be done on the 12 V rail, and the difference is entirely dynamic
range. Pack voltage moved 393 V against an 0.04 V offset. The 12 V rail moved
0.30 V against a 0.37 V offset. Identical method, opposite outcomes, decided by
how far the quantity being measured was willing to travel -- which is why the
span belongs beside every one of these numbers.

### The predicted swing arrived, and so did the predicted catch

Written above, before it happened: the models separate only if the rail swings
wide, and the catch is that the swinging state is the one where service 01 goes
quiet. Both halves came true in the same session, about nine minutes apart.

The truck parked at roughly t=460 s and settled toward sleep. `0x33E5` at
module `17` fell 13.20 -> 12.1 V, and the adapter at the connector fell
13.9 -> 12.8 V. That is a swing of 1.1 V, three times the 0.37 V offset --
comfortably enough to tell a constant from a factor, which is exactly the
measurement this was waiting for.

PID `0142` answered for the last time at t=520.4 s, reading 13.313 V. Every row
from t=1075 s on carries `module_voltage` empty. **The legislated route went
silent before the swing began and stayed silent through all of it.** The
window closed before the useful part started.

So the pairing window is narrower than "parked and awake", which is what this
document guessed at. Key-off does not work: service 01 stops answering long
before the DC-DC finishes disengaging. What is left is **key-on** -- the
vehicle coming awake, service 01 alive again, DC-DC not yet settled. That is a
transition the recorder already opens a session for; it is a matter of catching
one, not of asking the vehicle for anything new.

**First attempt at that window, and what it actually caught.** The truck woke
again at t=1310 s of the same session: contactors closed, pack voltage back to
389 V from 1.13 V, and the 12 V rail climbed 12.1 -> 12.8 V. PID `0142` stayed
empty through all of it.

That is not yet a test of the key-on hypothesis, because it was not a key-on.
`charger_5401_raw` went `00` -> `94` and pack current went negative at
-15.55 A: the truck had started **charging**. So what this observed is a
different and also useful thing -- charging brings the HV bus and the 12 V rail
up without waking service 01 at all. The set of states where `0142` answers is
narrower than "the vehicle is awake" and narrower than "the HV bus is live".
On the evidence so far it wants the driveline, and the driveline being up is
also what pins the rail near 13.5 V. Those two conditions may simply not
overlap, which would mean this pairing is not obtainable by this route. One
wake is not enough to say that, and it is written here so the next few wakes
are read against it rather than for it.

Recorded in passing, not analysed: `0x5401` took the values `0x94` and `0x91`
during this charge. Neither is among the states this project has previously
seen (`00`, `14`, `24`, `27`, `2A`, `2B`, `54`).

**And one thing this session did measure about `0x5401`.** Across the drive it
held `0x00` through regen down to **-314.95 A**, and went non-zero only once
the truck was plugged in, at -8 to -17 A. So it is a plug state and not a
current: three hundred amps flowing *into* the pack does not move it.

That matters because the dashboard's charge animation is gated on exactly this.
The rule written for it -- energy pulses appear only while pack power is
negative *and* the vehicle is not plugged in, so a charge cable can never
animate mid-drive -- rests on `plugged` being false during regen. That was
reasoned from the field's name when it was written. It is now measured, at the
hardest case available: the heaviest regen in the session.

The converse is measured too, on the same evening: plugged and drawing -15 A
sets the state, so a genuine charge is not mistaken for regen either. Both
directions of the guard now have a number behind them.

One thing the swing did settle. Through that entire 1.1 V descent, `0x33E5` at
module `17` and at module `1D` tracked each other to within 0.1 V -- 12.6/12.5,
12.3/12.3, 12.2/12.1, 12.1/12.1. The enhanced route is internally consistent
across two modules across a rail change eight times wider than anything the
driving data contained. Whatever the disagreement with PID `0142` turns out to
be, it is not `0x33E5` being unstable.

What it is remains open, and is recorded as open. Three readings of one rail
now sit in a consistent order -- the adapter at the connector highest near
13.9 V, then PID `0142`, then `0x33E5` at `17`, then `0x33E5` at `1D` lowest --
which is suggestive of a sense-point difference but is nine samples and should
not be argued from yet. `0x33E5`'s scaling is not in doubt: it is merged
upstream at `len 8, div 10`. Whether it means precisely what PID `0142` means
is now a live question with a number attached to it, which it did not have
before.

## Only one module serves legislated data

The last column is the measured part that had not been written down. The
census of 2026-09-03 asked all eight modules what service 01 they support,
using the standard's own bitmaps rather than any vendor identifier, and the
answer is lopsided enough to settle a question the project keeps re-asking.

Read `20`, `40`, `60`, `80` and `A0` out of those rows first: they are not
measurements, they are the "PIDs supported in the next twenty" continuation
bitmaps. Read `01` out too -- monitor status since codes cleared, a bitfield
about readiness rather than a quantity. What is left of every module except
`17` is **`42` alone**: its own control-module supply voltage.

So seven of the eight modules on this vehicle serve no legislated vehicle data
whatsoever. Module `17` serves fourteen, and it is where every legislated
reading this project records already comes from.

This matters most for the drive motor controllers, which are the project's
largest remaining gap: motor speed, torque, inverter and stator temperature.
`1D` and `1E` are named for those signals -- DMC2 and DMC3 -- and their support
bitmaps say they will hand over their own supply voltage and nothing else.
Whatever route eventually reaches motor data, **it is not service 01**, and no
amount of asking those modules more politely changes that. Combined with
`SOURCING_2026-09-04.md`, which found no enhanced identifier for any of those
signals on any public platform, both routes currently available to this project
are closed.

The raw census is `evidence/census.json`, which is deliberately not committed
-- `evidence/` is deny-by-default because it holds vehicle data. That is the
right rule and it had a cost: this result existed only in an ignored file for
six days, so a measurement the project had already paid for was one `rm` away
from having to be taken again. The conclusion belongs in a tracked document
even when the recording cannot be.
| `CD` | BSM-BatterySysMngr | `7F 22 31` | `7F 22 31` |

Read the three failure shapes carefully, because they are not
interchangeable:

`NO DATA`
: Nothing replied. The adapter waited and gave up. This says nothing whatever
  about the identifier — it says the request did not reach a module willing to
  answer it. Module `40` produced this thirteen times at `0x14`.

`7F 22 31` — `requestOutOfRange`
: A module replied, from its own address, with a well-formed negative response.
  It is present, it speaks service 22, and it does not hold that identifier.
  Module `CD` produces this for everything, at both priorities.

`7F 22 11` — `serviceNotSupported`
: **New to this project on 2026-09-03.** Module `28` returns it at `0x18` for
  identifiers it answers normally at `0x14`. Not "no such identifier" but "not
  this service, at this priority". It is the clearest possible statement that
  priority is part of the addressing rather than a formality.

## The consequence: there is no priority to standardise on

Most modules answer at both. Two do not, and they disagree:

* `28` answers only at `0x14` — moving it would lose wheel speeds, brake
  pressure, steering angle and both acceleration axes.
* `40` answers only at `0x18` — leaving it would lose an EVSE current, three
  battery group voltages, three battery temperatures and two coolant
  temperatures.

They cannot both be served by one global setting, which is exactly what the
drive recorder had. `AddressGroup` therefore carries a `priority` field, and
each group sends its own before its header. Both priorities go out in a single
cycle, and a test asserts that they do.

## How it was found

Not by reasoning. The wrong conclusion about module `40` was published with
thirteen data points behind it and a clean argument on top: `NO DATA` is not a
negative response, therefore nothing replied, therefore the route is the
problem. Every step of that was correct, and the broken part of the route was a
hardcoded constant in this repository.

What exposed it was `hummer-obd-discover`, which asks each module what it
supports using SAE J1979's own support bitmaps rather than any vendor
identifier. Module `40` answered:

```
17    DMCM-DriveMotorCtrl    svc01: 01 0D 1C 1F 21 30 31 42 A6   svc09: 02 04 06 0A
40    BCM-BodyControl        svc01: 01 42                        svc09: 04 06 0A
```

A module that answers the legislated services is a module that is reachable.
The census also proves its receive filter isolates rather than returning one
loud responder: module `17` advertised nine service 01 PIDs where every other
module advertised two, and only `17` advertises service 09 item `02`, the VIN.

## What this changes about how to probe

**When a module is silent, vary how you are asking before concluding anything
about what you are asking for.** Thirteen identifiers at one priority is one
data point about the priority, not thirteen about the identifiers.

The cheap way to do that is the support bitmaps. They are defined by the
standard for exactly this purpose, they need no source and no guess, and a
module answering them settles reachability in one request. Refusing to sweep
vendor identifiers is right; refusing to *ask* the standard's own question is
not the same thing.

## Things this does not establish

The matrix says which priority each module answers service 22 at **on this
vehicle, in the states tested** — parked and awake, at 13.7–13.9 V. It does not
establish:

* that `0x14` and `0x18` are the only priorities that work, since no others
  were tried;
* that a module answering at both exposes the *same* identifiers at both, which
  was spot-checked at `CB` and `17` and not exhaustively;
* anything about module `45` beyond its holding none of the four ISO
  identification identifiers, which is the only thing ever asked of it;
* why the split falls where it does. `28` and `40` being the two exceptions,
  and being the chassis and body controllers, is suggestive of a network
  boundary, but nothing here measures one.
