# October 19 drive audit — reviewed October 20, 2026

Times below are Arizona time (MST). CSV recordings, append-only adapter logs
and the radar collector's read-only history were compared. Raw transcripts,
coordinates and hardware identifiers remain private.

| Observed movement | OBD capture | Evidence |
|---|---|---|
| 08:47–09:10 | Missing | Radar recorded movement; OBD wake probes used the wrong adapter priority |
| 16:11–16:54 | 588 rows, 16.09 miles | Recording begins 16:12; 3D GPS in every row; 8.09 kWh used across the whole wake session |
| 19:16–19:27 | 174 rows, 4.85 miles | Recording begins 19:17; 3D GPS in every row; 2.56 kWh used across the whole wake session |
| 20:01–20:09 | Missing | Radar and external GNSS history show another trip after the prior OBD session ended |

These are observed movement windows, not exact ignition times. The two OBD
recordings include parked time until 18:11 and 19:43 respectively. Their
20.94 miles and 10.65 kWh describe the recorded wake sessions, not the entire
day. A separate 08:30–08:36 session contains 29 stationary rows before the USB
GPS repair, and a noon file contains a header without samples.

## Confirmed recording fault

Normal recording restores enhanced CAN priority `ATCP14` after its standard
reads. The subsequent `_vehicle_answers()` wake check previously sent `010D`
without restoring standard addressing. The raw transcript shows these checks
using priority `14`, returning `NO DATA` or `CAN ERROR`, while independent
radar history proves the truck was moving. The watch consequently remained
outside a recording session.

The fix establishes the existing `STANDARD_ADDRESS` (`ATCP18`, `ATSHDA17F1`,
`ATCRA18DAF117`) before the existing `010D` wake probe. These AT commands
configure only the adapter. Vehicle request count, allowlists, low-voltage
behavior and bounded wake cadence are unchanged. Regression tests reproduce
the stale priority, an unrelated module filter, and sleep-to-next-session
recovery; all fail before the fix and pass after it.

## Capture quality and hardware

- Moving sample periods have medians of 4.97 and 4.55 seconds. The longest
  gaps touching moving rows are 8.89 and 9.85 seconds; none exceeds 20 seconds.
  Larger whole-session gap warnings occur in the parked portions.
- All 762 rows in the two driving recordings have mode-3 GPS and a position.
  The stable USB alias repair therefore worked during these recordings.
- Standard speed, wheel-integrated distance and odometer agree closely in
  the captured drives. Instantaneous pack-current power and energy-slope
  power still disagree at individual samples; this pre-existing warning is
  not resolved by relabeling or inventing sensor scaling.
- Radar history contains 3,697 telemetry rows for the day from its completed
  sessions, including the two drives missed by OBD. No completed alert events
  were recorded. Nine initial rows from today's boot carried the restored
  old clock and are excluded from that count.
- At the live check, OBD recording and radar streaming are active. GPSd serves
  the SiRF receiver through `/dev/hummer_gps`; its current cold-start fix is
  mode 1. Registration and available satellite reception are separate checks.
- No failed systemd units, undervoltage/throttling flags, current-boot HCI
  timeout/reset faults or filesystem-error evidence were found. Root usage is
  22%, with 22 GiB free. A dashboard bind retry before the VPN address existed
  recovered automatically.

## Power findings and remaining acceptance

The owner found that the truck-to-UPS cable may have been unplugged. Once
access returned, the UPS reported external input present and restore enabled
(`CTR1=0xFC`, firmware `v1.3.4`). The root-owned reserve monitor runs at 10%,
three low readings 30 seconds apart, with a 3.40 V fallback; the installed
helper and late shutdown hook hashes match committed source. The recorder
and monitor start after the boot GPS wait; early snapshots showing them
inactive were taken before that wait completed.

A missing input cable prevents a vehicle-start power restoration event.
The earlier button press therefore does not establish a firmware or settings
failure. Actual automatic shutdown, UPS output cutoff, restored vehicle input
and unattended boot still need a controlled physical test, previously deferred
to keep the Pi online. No power settings were changed speculatively.

Only this boot's volatile journal remains. Its initial timestamps use the
clock saved at the prior shutdown before network time corrects them; they do
not prove the Pi booted yesterday. Persistent CSV/raw/radar history support
the drive findings, but cannot establish yesterday's exact UPS cutoff reason.

The radar dashboard freshness fix also withholds old voltage, GPS-lock and
live alerts when telemetry is stale or disconnected. Both that fix and the
wake-probe fix require the owner to restart `hummer-dashboard` and
`hummer-drive` after the staged source is deployed. Activation and fresh live
checks must be recorded before calling the patched processes verified.
