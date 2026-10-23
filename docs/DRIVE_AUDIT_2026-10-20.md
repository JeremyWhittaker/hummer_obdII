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
- At the initial live check, OBD recording and radar streaming were active. GPSd serves
  the SiRF receiver through `/dev/hummer_gps`; its current cold-start fix is
  mode 1. Registration and available satellite reception are separate checks.
- Initially no failed systemd units, undervoltage/throttling flags, HCI
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

## Activation and additional Bluetooth fault

The owner restarted the patched dashboard and recorder at 11:46 MST. The new
dashboard API reports packet age separately and withheld voltage/GPS-lock
when a fresh heartbeat carried a 16.5-second-old packet, proving that fix is
loaded. Full source validation passed 1,612 tests and 4,180 subtests, with one
skip; 24 targeted tests also passed on the Pi.

The same verification caught a controller hang after restart. Kernel HCI
timeouts (`0x0406`, `0x2005`, `0x200b`) coincided with the recorder blocked in
`tty_wait_until_sent`, no raw-log progress, and radar packet age exceeding
90 seconds. Cached connected flags made the watchdog return healthy without
escalating despite that evidence.

The watchdog correction uses passive successful-reply and packet-age checks.
Only positive staleness of both streams together with recent kernel errors
can override cached connected flags. Fresh traffic or missing evidence keeps
the existing restraint; escalation spacing, service allowlists and cooldown
remain unchanged. Regression tests cover the captured failure and protect a
recovered controller while old errors remain in the journal.

The candidate is staged outside the active root import path. The owner-run
`scripts/repair-bluetooth-controller.sh` validates it, backs up the old source,
pauses the timer, installs the candidate and reloads the Bluetooth UART
driver, then restores previously running consumers. Wi-Fi, Pi power and the
reserve monitor remain online. Live Bluetooth recovery and fresh recorder
rows remain unverified until that manual privileged step completes.

## October 20 live-drive follow-up

At 13:28 MST, the Pi remained reachable. GPSd reported the SiRF receiver
registered through `/dev/hummer_gps` with a mode-3 position fix. The UPS
reported 100% charge, external input present and restore enabled; the original
reserve-monitor process remained running without restarts. No throttling flags
or failed systemd units were reported. A separate 30-second GPS sample during
movement contained 60 position reports, all mode 3, with 10 of 12 visible
satellites in use and speeds of 36.71–49.73 km/h (about 23–31 mph).

The staged Bluetooth recovery had not been applied: the active watchdog still
matched the old source, the recorder remained blocked, and kernel HCI timeouts
continued. The dashboard correctly marked the old OBD and radar readings
stale. A running recorder process does not establish that a drive is being
recorded; fresh rows and detector packets remain the acceptance criteria.

## October 21 — recovery applied, and the recordings it was owed

### The controller fault did not survive a power cycle

The node powered off some time after 00:56 MST and came up again at 12:06 MST.
The boot's first entries carry the clock restored from the previous shutdown,
so they are stamped 00:56; `hummer-gpstime` corrected the clock from the GPS at
12:07 and real timestamps begin there. The recorder opened a session 70 seconds
after that boot and recorded normally, with the wedged controller gone and the
staged watchdog still not installed. The power cycle, not the repair, cleared
the October 20 hang.

| Session (UTC) | Rows | Speed rows | Position rows | Odometer | Peak |
|---|---|---|---|---|---|
| 19:07:18 – 21:07:15 | 902 | 693 | 902 | 4242.3 → 4270.3 km | 95 km/h |
| 21:07:21 – 22:45:26 | 411 | 152 | 411 | 4270.3 → 4275.3 km | 80 km/h |

Together 33.0 km (20.5 miles) across two wake sessions, every row with a
mode-3 GPS position. This is the wake-probe addressing fix working in
production rather than in a test: the second session begins immediately after
the first ends, which is the sleep-to-next-session recovery the regression
tests cover.

Standard mode-01 PIDs stop answering partway through the second session — the
last speed and odometer sample is at 21:37, and the remaining 192 rows carry
neither, while the extended `22` DIDs keep answering (state of charge is
present in 404 of 411 rows). The same shape appears in the parked session that
follows. Absent standard PIDs on a parked truck are therefore expected here;
they are not evidence of a wake-probe regression.

### Owner-run Bluetooth recovery

The owner ran `sudo bash scripts/repair-bluetooth-controller.sh` at 15:46:06
MST. The script backed the old watchdog up under `/var/backups/hummer-bluetooth`,
installed the candidate — the active source now matches the staged
`.foreman/btwatch-next.py` byte for byte — and restored the consumers it had
stopped: the recorder came back at 15:46:09 and reopened the serial device, the
radar collector at 15:46:14, and the watchdog timer's next run finished cleanly
at 15:47:47. The adapter reports connected, the character device's timestamp
advances, and a new session is growing.

Acceptance read from behaviour, not from process state: of 64 published
signals, 29 are fresh, including state of charge at 74.696%, the 12 V rail at
12.8 V, cell minimum, maximum and spread, the 26-position `array_2b43` array,
and GPS mode 3 with 11 satellites in use. `speed_kph` and `odometer_km` are
reported `missing` with 0 of 10 samples, for the parked-truck reason above.

`/api/r8` carries `telemetry_age_s` and reports `stale` with a null voltage, so
the radar cached-heartbeat correction is loaded in the running API.

### Radar still has not connected this boot

The collector is active, but the detector has not been reachable at any point
this boot — the status panel has said `r8 connecting` since 12:06, which is
before the Bluetooth repair as well as after it, and `/api/r8` reports
`connected: false` with no packets. The radar link is BLE and independent of
the RFCOMM path that was repaired. Nothing here establishes why; a detector
that is unpowered while the truck is parked looks exactly the same from the
node. The earlier alert history remains readable.

### The node is on UPS battery now, and the journal still cannot record it

At 16:00 MST the board reports 3.949 V, 91%, `external=False`, `restore=True`,
`CTR1=0x7C`: the vehicle feed is absent and the node is running on the UPS with
the reserve monitor armed. That is the first observation of the new monitor in
the state that ends in an automatic shutdown.

It would leave no evidence. `/etc/systemd/journald.conf` has said
`Storage=persistent` since 2026-09-03 and has never had any effect, because
Raspberry Pi OS ships `/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf`
and a drop-in is applied after the main file. `/var/log/journal` has been empty
since the image was written; every boot logs to `/run` and is destroyed with
it. That is why this audit cannot say what powered the node off at 00:56, and
why `LOW BATTERY: graceful reserve shutdown` and `/run/hummer-low-battery` —
the only two traces an automatic reserve shutdown leaves — would both be gone
by the time anyone looked.

`scripts/enable-persistent-journal.sh` installs one `/etc` drop-in that sorts
after the vendor's and restarts `systemd-journald`; it is Pi-guarded, has a
read-only `--check`, and verifies that a journal file actually appears under
`/var/log/journal` instead of trusting an exit code. Until an owner runs it,
the deferred UPS acceptance stays unprovable after the fact, however many
times the cycle actually happens.

### What this did not establish

- The cause of the 00:56 MST power-off. The previous boot's journal was never
  written and the shutdown marker lives in a tmpfs. The 12:06 boot is
  *consistent with* restored vehicle input and nothing more; whether anyone
  pressed the button is not recorded anywhere.
- The complete UPS lifecycle: automatic shutdown at reserve, output cutoff,
  restored input, unattended return. Still owed, as before.
- Why the radar detector has not connected this boot.

## October 22 — the reserve shutdown probably fired, and the bond was lost

### Evidence that the automatic shutdown happened

The last complete recording of the day ends at 09:51 MST with 244 rows, and its
final row carries all 64 fields. A flushed, whole last row is what a *graceful*
stop looks like; a power cut tears it. From the previous evening's drain — 77%
at 23:18, about 1.9 points per hour with the vehicle feed absent — the 10%
reserve falls around 10:00 MST. It stopped at 09:51.

That is the first apparent occurrence of the automatic reserve shutdown, and it
is **not proven**: the journal was volatile, so the monitor's own
`LOW BATTERY: graceful reserve shutdown` line and the `/run` marker were both
destroyed by the power loss they describe. This is the second consecutive day on
which the pending `scripts/enable-persistent-journal.sh` would have settled the
question outright.

The node was not dark afterwards. The 20:41 MST boot restored its clock to
19:30, and a restored clock can only come from a later write than 09:51, so at
least one boot ran in between and also ended. The owner then supplied power
directly; the board charged while the Pi was off and reads 4.197 V, 100%,
`external=True`, `restore=True`, `CTR1=0xFC`, with `throttled=0x0` — the direct
supply is adequate and the board is healthy.

### The Bluetooth bond was lost, and what that looks like

After the 20:41 boot the recorder could not open a link: unanswered `ATRV`
probes, then `reopen failed: read failed for ATZ`. The cause is narrower than
it appears:

- `bluetoothctl devices Paired` and `Trusted` are both **empty**. The pairing
  store's mtime is 11:57 MST, during the intermediate boot.
- The binding is intact: `rfcomm0 … channel 1 closed [tty-attached]`, and
  `/etc/default/hummer-rfcomm` still holds the right address and channel.
- The adapter is **present**: it answers a direct page as `OBDLink MX+`. A
  classic inquiry found nothing, which proves nothing — a bonded classic device
  stops advertising. The radar detector was also seen advertising, so the node
  is still with the vehicle.

So the repair is a re-pair of a known address, not a first-time setup. Why the
bond was lost is not established, and the journal that would have shown it was
erased.

The watchdog behaved correctly throughout, logging
`nothing to do (controller=up obd=down rfcomm=closed)`: an absent peer is not a
controller fault, and it did not reset the Bluetooth stack over it. That is the
restraint added on October 20 working on a case it had not seen.

### What October 22 did not establish

- That the shutdown at 09:51 was the reserve monitor's. The reading is
  circumstantial — a clean final row at the hour the drain predicted — and the
  two records that would decide it were in a tmpfs.
- Why the Bluetooth bond disappeared, or exactly when within the 11:57 window.
- What ran between roughly 10:00 and 19:30, beyond the fact that something did.
- Whether the radar detector can connect; it advertises, but nothing has
  attempted a connection since the bond was lost.
