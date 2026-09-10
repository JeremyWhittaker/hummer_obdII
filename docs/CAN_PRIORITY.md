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
