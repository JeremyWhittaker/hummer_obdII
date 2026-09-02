# Vehicle safety rules

This project talks to a live vehicle. The rules below are enforced in code
(`src/hummer_obd/safety.py`), tested (`tests/test_safety.py`), and are not
negotiable at runtime — there is no flag that turns the gate off.

## What may be transmitted

| Category | Examples | Why it is safe |
|---|---|---|
| Adapter configuration | `ATZ`, `ATE0`, `ATH1`, `ATSP0`, `ATST64` | Acts on the OBDLink adapter, not the vehicle |
| Adapter information | `ATI`, `AT@1`, `AT@2`, `ATRV`, `STI`, `STDI` | Read-only identification and connector voltage |
| Service 01 | `0100`, `010C`, `0142` | Standard current-data reads |
| Service 02 | `0202`, `020200` | Freeze frame: the snapshot an ECU stored beside a DTC |
| Service 03 / 07 / 0A | `03`, `07`, `0A` | Stored, pending and permanent DTC **reads** |
| Service 06 | `0600`, `0601` | On-board monitoring test results the ECU computed itself |
| Service 09 | `0902`, `0904`, `090A` | Vehicle information (VIN, calibration ID, ECU name) |

## What must never be transmitted

* **Service 04** — clears diagnostic trouble codes. Never, under any
  circumstances, including "just to clean up".
* **Service 08** — controls on-board systems, tests and components.
* **UDS `2E`** (WriteDataByIdentifier), **`27`** (SecurityAccess), **`2F`**
  (InputOutputControlByIdentifier), **`31`** (RoutineControl), `11` (ECUReset),
  `14`, `34`–`38`, `3D`, `3E`, `85` and the rest of the write/control/security
  set.
* Adapter macros that would embed any of the above.
* **Service 22** (enhanced read-by-identifier) is *deferred*, not permitted, in
  this build. It is read-only in principle, but GM/Ultium identifiers are
  unproven on this VIN; guessing identifiers is not acceptable.

The gate is an allowlist: anything not listed as safe is rejected before any
byte reaches the serial port, and `FORBIDDEN_SERVICES` is checked as a second,
independent barrier.

## Data handling rules

* Raw request and response bytes are written **append-only** to
  `logs/raw/*.jsonl`, base64 **and** hex, before parsing.
* Nothing rewrites, normalises or deletes raw frames.
* The VIN is never printed in summaries, commits, dashboards or terminals: it
  is masked by `decode.mask_vin()` (`1G1************67 (len=17)`). The
  unmasked value exists only inside the raw log, which is git-ignored.
* `hummer_pi_sd.env` (credentials) and `logs/`, `data/` (vehicle data) are
  git-ignored and must never be committed.

## Operational rules

* A sleeping vehicle answering `NO DATA` is a wait condition, never a reason to
  probe harder. The collector backs off; it does not escalate.
* A raw probe must be run and reviewed before any collector cycle.
* Continuous collector autostart additionally requires a verified vehicle
  sleep/wake cycle with acceptable 12 V draw, or confirmed ignition-switched
  power for the Pi. A successful one-shot does not satisfy this power gate.
* Bluetooth pairing acts only on a device that unambiguously identifies itself
  as an OBDLink adapter. Ambiguity stops the process.

## Change-control rules

Any expansion of the command set requires all of the following in one reviewed
change:

1. a written explanation of why the command is read-only for the target
   protocol and vehicle;
2. an explicit allowlist update without weakening the forbidden-service checks;
3. tests proving unsafe variants and command batching still transmit zero
   bytes;
4. an offline/simulated acceptance path before vehicle use; and
5. a supervised first live request with byte-exact logging and transcript
   review.

Mode 22 remains out of scope until identifiers are validated for the exact
vehicle. A third-party app label or an Internet PID list is not sufficient
evidence.

### Change record: services 02 and 06, 2026-09-01

Services `02` (freeze frame data) and `06` (on-board monitoring test results)
were added to `ALLOWED_OBD_MODES`. Against the five requirements above:

1. **Why they are read-only.** Both are request/response *data* services in the
   same SAE J1979 specification that defines `01`, `03`, `07`, `09` and `0A`.
   Service 02 returns a snapshot the ECU already stored alongside a DTC.
   Service 06 returns results of tests the ECU ran on its own schedule. Neither
   has a sub-function that writes, actuates, resets, unlocks or clears, and an
   ECU with nothing to report answers with an empty positive response rather
   than changing state. Critically, and unlike mode 22, **neither requires a
   vendor identifier to be guessed**: the PIDs and MIDs are standard, and an
   unsupported one is refused by the ECU rather than doing something unexpected.
2. **Allowlist updated without weakening the denylist.** `FORBIDDEN_SERVICES`
   is unchanged, and a test asserts the allowlist and the denylist remain
   disjoint. Service 02 was given its own request shape rather than relaxing
   the existing one-parameter-byte rule, so no other service became more
   permissive as a side effect.
3. **Tests.** `tests/test_safety.py` proves the accepted shapes, and proves
   that a bare `02` or `06`, an over-long payload, a forbidden service, and
   command batching behind the new services are all still rejected before any
   byte is written.
4. **Offline acceptance.** Exercised against the PTY/ELM simulator with an
   assertion on the exact transmitted command list.
5. **Supervised first live request.** *Not yet done.* The vehicle was asleep
   when these were added. Until a supervised, byte-exact live request has been
   run and its transcript reviewed, treat services 02 and 06 as **permitted but
   unproven on this vehicle**.

The freeze-frame read is additionally gated by behaviour rather than by the
gate: the probe only requests it when a DTC read actually returned stored
codes. With zero DTCs there is no freeze frame to fetch, and asking would be
bus traffic that buys nothing.

## Publication rules

The public repository and public issue trackers must not contain:

- credentials, Wi-Fi keys, VPN keys, or SSH private keys;
- private LAN/VPN names or addresses;
- the adapter or Pi Bluetooth MAC address;
- an unmasked VIN;
- raw JSONL transcripts or SQLite databases; or
- provisioning captures that enumerate nearby devices.

Publish curated, masked summaries instead. The authoritative detailed evidence
remains on the Pi or in a private backup.
