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
  Sleep with the hardware attached and polling stopped has been observed; what
  remains unproven is overnight 12 V stability and whether the vehicle still
  sleeps while the collector is actively polling.
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

Mode 22 remains out of scope **for unattended collection**, permanently. What
changed on 2026-09-02 is not that the gate was widened but that a second,
narrower gate was added beside it; see the change record below. A third-party
app label or an Internet PID list is still not sufficient evidence to add an
identifier, and identifiers are never guessed, incremented or swept.

### Change record: supervised enhanced reads, 2026-09-02

Service `22` (`ReadDataByIdentifier`) became transmittable **for exactly one
identifier, on a path no unattended code calls.** Against the five requirements
above:

1. **Why it is read-only.** Service `22` reads a value the ECU already holds.
   It has no sub-function that writes, actuates, resets, unlocks or clears; the
   write counterpart is service `2E`, which is in `FORBIDDEN_SERVICES` and
   stays there. An ECU asked for an identifier it does not have answers
   `7F 22 31` rather than doing something unexpected.
2. **Allowlist updated without weakening the denylist.** `FORBIDDEN_SERVICES`
   is unchanged. `ALLOWED_OBD_MODES` is *also* unchanged — service `22` was
   deliberately **not** added to it, and an import-time assertion now fails the
   build if anyone ever adds it. The new capability lives in
   `validate_enhanced_command`, which accepts service `22` only for an
   identifier enumerated in `ENHANCED_READ_DIDS` and refuses every other
   service, including the ordinary read services the collector may send.
3. **Tests.** `tests/test_enhanced.py` asserts that the collector's gate still
   refuses `2227C6`; that the enhanced gate refuses the adjacent identifiers
   `0x27C5` and `0x27C7`, multi-identifier requests, every forbidden service,
   and command batching; and that the transport's default validator is still
   the unattended one, so a caller that forgets gets the safe behaviour.
4. **Offline acceptance.** The tool is a dry run by default: without `--confirm`
   it prints the exact byte sequence it would send, validates all of it, and
   never opens the serial device.
5. **Supervised first live request, byte-exact.** Run 2026-09-02 22:33 UTC with
   the vehicle awake and attended, transcript in `logs/enhanced-raw.jsonl`,
   evidence in `evidence/enhanced-bt1-decoded.json`. One request, no loop.

The identifier came from a published profile that names this vehicle, not from
inference. Full provenance and the response analysis are in
[GM enhanced candidates](GM_ENHANCED_CANDIDATES.md).

Two adapter-command groups were allowlisted alongside it: `ATCP` (CAN priority
byte, needed because `ATSH` carries only three of the four header bytes) and
`ATFCSH`/`ATFCSD`/`ATFCSM` (ISO 15765-2 flow control). These configure how the
adapter addresses and how it acknowledges a multi-frame *reply*; they cannot
originate a request, and mode 09 already depends on multi-frame reception.

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
5. **Supervised first live request.** *Done, and both answered.*
   - **Service 06 is proven**: `0600` returned a positive response advertising
     **zero** monitor IDs. The service works and this vehicle exposes no
     on-board monitors through it. An empty supported-MID bitmap is an answer,
     not a failure.
   - **Service 02 is proven at the service level**: `020000` returned a
     positive response advertising four freeze-frame PIDs (`02 0D 1F 20`).
     **No freeze frame has ever been read**, because a frame only exists once a
     DTC has been stored and this vehicle has none. That distinction is worth
     keeping: the request path is demonstrated, the frame contents are not, and
     they cannot be until the vehicle develops a fault of its own. Inducing one
     to exercise a decoder is not acceptable.

The freeze-frame read is additionally gated by behaviour rather than by the
gate. The probe always asks `020000` — what a frame *would* contain, which
costs one request and is how service 02 was demonstrated at all on a healthy
vehicle — but requests individual frame readings only when a DTC read actually
returned stored codes. With zero DTCs there is no freeze frame to fetch, and
asking for one would be bus traffic that buys nothing.

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
