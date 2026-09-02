# Future maintainer handoff

This is the durable starting point for the next human or coding agent. Read
[Safety](SAFETY.md) first, then check this file against the live node before
making a change.

## Mission

Maintain a small, read-only Hummer EV telemetry node without expanding vehicle
control authority. The current release proves standard OBD-II transport,
logging, decoding, storage, Bluetooth recovery, and e-paper status. It does not
authorize enhanced PID exploration or continuous polling until the remaining
power test is complete.

## Non-negotiable invariants

1. Never send Mode 04 or any DTC-clear command.
2. Never send Mode 08 or a UDS write/control/security/reset/routine command.
3. Keep Mode 22 rejected until an exact identifier set is independently
   validated for the exact vehicle and accepted through the safety process.
   Services 02 and 06 were added on 2026-09-01 because they are standard and
   need no guessed identifier; that reasoning does not extend to Mode 22.
4. Every command must pass `safety.validate_command()` immediately before
   serial I/O; do not add a bypass.
5. Preserve raw TX/RX bytes append-only before parsing.
6. Never commit credentials, private network identifiers, adapter addresses,
   an unmasked VIN, JSONL transcripts, SQLite databases, or provisioning
   captures.
7. Treat a sleeping vehicle as a wait condition. Do not increase traffic to
   wake it.
8. Keep the continuous collector disabled until the physical power gate is
   satisfied. Sleep with the hardware attached and polling stopped has been
   observed; overnight 12 V stability and sleep behaviour while actively
   polling are both still unproven, and either one is enough to keep it off.

## Last verified deployment state

| Component | State |
|---|---|
| Pi | Raspberry Pi Zero 2 W, Debian 13, headless multi-user target |
| Network | mobile 2.4 GHz profile preferred; stationary profile retained as fallback; SSH key login works |
| Display | Waveshare 2.13-inch V4; service enabled and active; shows host/network/uptime/temperature/OBD state |
| Adapter | OBDLink MX+ paired, bonded, trusted |
| Transport | one SDP-confirmed Serial Port channel; `/dev/rfcomm0`; persistent service enabled |
| Protocol | AUTO, ISO 15765-4 (CAN 29-bit / 500 kbit/s) |
| Probe | completed and reviewed; raw transcript remains private on the Pi |
| Collector | one-shot proven; continuous unit disabled and config flag false |
| Upload | disabled, endpoint empty |
| Tests | 347 passed / 328 subtests under both test runners |

The final controlled reboot was also accepted: both SSH paths returned in about
36 seconds, all required infrastructure/display/RFCOMM services were active,
the bond and channel survived, transcript hashes were unchanged, the collector
remained off, and the full on-Pi suite passed.

The public repository intentionally omits the Pi's address, tailnet name,
adapter MAC, SSIDs, and VIN. Discover live values locally rather than adding
them to this file.

## Reading the vehicle's state before you touch anything

Three adapter readings distinguish the states that otherwise look identical:

| Reading | Meaning |
|---|---|
| positive data from 5-8 ECUs | vehicle serving diagnostics |
| `7F <service> 22` from ECU `28` alone | `BSCM-BrakeSystem` alive, refusing (`conditionsNotCorrect`). Not the gateway; that is `45` |
| `NO DATA` **and** `ATCS T:00 R:00` | protocol right, request sent, bus silent: vehicle asleep |
| `SEARCHING... / UNABLE TO CONNECT` | auto-detect found nothing: vehicle asleep |

`ATCS T:00 R:00` is what makes "asleep" distinguishable from "adapter or wiring
fault". If you must check the bus state, force `ATSP7` rather than repeating
`ATSP0`: the protocol is known, and auto-search walks initialisation sequences
this vehicle has no use for.

## Start-of-work checklist

From a trusted checkout:

```bash
git status -sb
git pull --ff-only
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q
```

On the Pi, using a private SSH target:

```bash
hostname
uptime
systemctl --failed --no-pager
systemctl is-enabled hummer-display hummer-rfcomm hummer-btdiscover hummer-collector
systemctl is-active hummer-display hummer-rfcomm hummer-btdiscover hummer-collector
rfcomm
ls -l /dev/rfcomm0
```

Do not run a probe simply to discover state. First inspect existing private
logs, SQLite sessions, and journals.

## Safe next milestone

The next meaningful milestone is a power/sleep experiment, not a new decoder:

1. ~~confirm whether the Pi supply is ignition-switched or always live~~ —
   **answered on 2026-09-01: the OBD-II port is always live.** The adapter's
   own `STDIX` counters showed a continuous multi-hour power-on session
   spanning periods when the vehicle was off, and the owner independently
   observed the port still powered after the truck shut itself down. Treat the
   Pi and adapter as a permanent parasitic load;
2. measure baseline 12 V current with the vehicle asleep. `ATRV` gives a
   voltage trend with **zero CAN traffic** and is the cheapest first version of
   this measurement;
3. observe a complete vehicle sleep/wake cycle with the Pi and OBDLink attached
   while the continuous collector remains off. **Partly answered 2026-09-01**:
   with polling stopped, the vehicle reached sleep about five minutes after
   parking (13.9 V to 12.7 V), observed by `hummer-obd-voltage` with zero CAN
   traffic. Still open: whether the rail holds at rest over hours, and whether
   the vehicle still sleeps while the collector is actively polling;
4. if safe, run a time-bounded polling trial and verify the vehicle still
   sleeps; and
5. record measurements, duration, service state, and rollback criteria.

Only a successful physical result permits changing both
`collector.enabled = true` and the systemd enable state. If the result is
uncertain, leave both off.

One encouraging observation, which is **not** sufficient on its own: the
vehicle powered itself off normally with the adapter connected, shortly after
diagnostic traffic. Adapter presence did not hold it awake. That says nothing
yet about a two-second polling loop.

`hummer-collector-trial.service` is installed and reports `static`: it cannot
be enabled at boot, only started deliberately for one bounded run. Use it, or
`scripts/run_trial.sh` when there is no stable link for `sudo`.

Step 4 now has real tooling. `hummer-obd-collector --duration-s S
--poll-interval-s S --force` runs a bounded trial that stops itself, and
`hummer-obd-capabilities` reports node state without ever opening
`/dev/rfcomm0`, so it is safe to run at any point during a sleep observation.

## Change procedure

For any code change:

1. state which safety invariant and data path are affected;
2. add focused tests before live deployment;
3. run the full hardware-free suite;
4. deploy without restarting the collector;
5. run `scripts/pi_smoke.sh` (it sends no vehicle commands);
6. verify the relevant systemd unit loaded the new bytes; and
7. update README and the applicable document in `docs/`.

For a command-set change, also follow the five-part change-control process in
[Safety](SAFETY.md). The first live request must be supervised and preserved in
a byte-exact transcript.

## Private state locations

```text
/home/jeremy/hummer-obd/config/hummer.toml
/home/jeremy/hummer-obd/logs/raw/
/home/jeremy/hummer-obd/data/hummer_obd.sqlite3
/home/jeremy/hummer-obd/evidence/
/etc/default/hummer-rfcomm
```

These paths are operational state, not source. Back them up privately and do
not solve a missing-source problem by copying them into Git.

## Known pitfalls

- The Pi Zero 2 W is 2.4 GHz-only. A saved 5 GHz hotspot SSID never associates.
- NetworkManager priority does not preempt an already healthy connection; use
  the guarded switch script for a deliberate move.
- The validated OBDLink requires interactive six-digit pairing confirmation;
  a headless BlueZ agent fails.
- A bonded adapter may no longer be discoverable. Recovery must inspect known
  BlueZ devices, not assume a fresh scan will find it.
- The adapter exposes a non-SPP service as well as Serial Port. Bind only the
  channel under the Serial Port / `STN-SPP` record.
- 29-bit CAN replies have a four-byte identifier before the ISO-TP PCI byte and
  must be reassembled independently per ECU.
- Large package purges can make a Zero 2 W appear offline for a long time due
  to SD-card I/O and thermal throttling. Never interrupt `apt`/`dpkg` blindly.
- An unchanged e-paper screen may be intentional; identical frames are skipped.

## Definition of done

A future change is complete only when tests pass, the deployed process has
loaded the changed code, private/runtime files remain untracked, documentation
matches reality, the commit is pushed, and any intentionally disabled safety
gate remains disabled unless its exact prerequisite was proven.
