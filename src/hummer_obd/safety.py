"""Command safety gate.

The vehicle is a live Hummer EV.  The only commands this project may put on the
wire are adapter configuration/inspection commands (AT/ST) and *read-only*
standard OBD-II service requests.  Anything that could write, actuate, reset,
unlock, or clear vehicle state is rejected here, before serial I/O.

Design notes
------------
* The gate is an **allowlist**.  Unknown commands are rejected, not guessed at.
* A denylist is applied as well, so a mistake in the allowlist cannot let a
  known-dangerous service through (defence in depth).
* The gate refuses command batching (``\r``, ``\n``, ``;``) so a validated
  command cannot smuggle a second, unvalidated one behind it.
* Mode ``22`` (enhanced read-by-identifier) is **refused by**
  :func:`validate_command`, which is the gate every unattended code path uses.
  A second, deliberately narrower gate --
  :func:`validate_enhanced_command` -- accepts service ``22`` for an *exact,
  enumerated* set of identifiers with published provenance, and nothing else.
  Keeping them as two functions is the point: the collector calls
  ``validate_command``, so no configuration mistake, flag, or bug in the
  experimental path can put an enhanced read on the wire during unattended
  collection.  There is still no runtime bypass and no DID sweeping.
"""

from __future__ import annotations

import re
from typing import Final, Iterable

__all__ = [
    "UnsafeCommandError",
    "ALLOWED_OBD_MODES",
    "FORBIDDEN_SERVICES",
    "ENHANCED_READ_DIDS",
    "MONITOR_CAN_MODE",
    "MONITOR_STREAM_COMMAND",
    "validate_monitor_setup_command",
    "validate_monitor_stream_command",
    "normalize",
    "validate_command",
    "validate_enhanced_command",
    "validate_supervised_command",
    "is_safe",
    "describe_command",
]


class UnsafeCommandError(ValueError):
    """Raised when a command is not provably read-only."""


#: OBD-II services this project is allowed to request.  Every one of these is
#: a *request for data the ECU already holds*; none of them writes, actuates,
#: resets, unlocks or clears anything.
#:
#:   01 current data              06 on-board monitoring test results
#:   02 freeze frame data         07 pending DTCs
#:   03 stored DTCs               09 vehicle information
#:                                0A permanent DTCs
#:
#: 02 and 06 were added on 2026-09-01.  They are standard SAE J1979 read
#: services, defined by the same specification as 01/03/07/09/0A, and unlike
#: mode 22 they need no vendor identifier to be guessed: 02 returns the stored
#: snapshot that accompanied a DTC, and 06 returns monitor test results the ECU
#: computed on its own.  Asking for them cannot change vehicle state, and an
#: ECU with nothing to report answers with an empty positive response.
ALLOWED_OBD_MODES: Final[frozenset[str]] = frozenset(
    {"01", "02", "03", "06", "07", "09", "0A"}
)

#: Services that must never be transmitted, whatever else changes.  ``04`` is
#: the OBD-II clear-DTC service; ``08`` actuates on-board components; the rest
#: are UDS write/control/security/reset services.
FORBIDDEN_SERVICES: Final[frozenset[str]] = frozenset(
    {
        "04",  # OBD-II: clear diagnostic trouble codes
        "08",  # OBD-II: control of on-board system, test or component
        "10",  # UDS: DiagnosticSessionControl
        "11",  # UDS: ECUReset
        "14",  # UDS: ClearDiagnosticInformation
        "27",  # UDS: SecurityAccess
        "28",  # UDS: CommunicationControl
        "2E",  # UDS: WriteDataByIdentifier
        "2F",  # UDS: InputOutputControlByIdentifier
        "31",  # UDS: RoutineControl
        "34",  # UDS: RequestDownload
        "35",  # UDS: RequestUpload
        "36",  # UDS: TransferData
        "37",  # UDS: RequestTransferExit
        "38",  # UDS: RequestFileTransfer
        "3B",  # SAE J1979 legacy write
        "3D",  # UDS: WriteMemoryByAddress
        "3E",  # UDS: TesterPresent (keeps a session alive; not needed read-only)
        "83",  # UDS: AccessTimingParameter
        "84",  # UDS: SecuredDataTransmission
        "85",  # UDS: ControlDTCSetting
        "87",  # UDS: LinkControl
    }
)

#: Adapter commands with no vehicle-side effect.  Exact matches only.
_ALLOWED_AT_EXACT: Final[frozenset[str]] = frozenset(
    {
        "ATZ",       # adapter reset (adapter only)
        "ATD",       # restore adapter defaults
        "ATE0", "ATE1",      # echo off/on
        "ATL0", "ATL1",      # linefeeds
        "ATS0", "ATS1",      # spaces in responses
        "ATH0", "ATH1",      # headers off/on
        "ATAL",               # allow long (>7 byte) receive messages
        "ATI",               # adapter identification
        "AT@1", "AT@2",      # device description / device identifier
        "ATRV",              # read battery voltage at the connector
        "ATDP", "ATDPN",     # describe current protocol
        "ATCS",              # CAN status counts
        "ATAT0", "ATAT1", "ATAT2",   # adaptive timing
        "ATCAF0", "ATCAF1",          # CAN auto formatting
        "ATM0",              # memory off
        "ATPC",              # protocol close
        # OBDLink/STN informational commands
        "STI",               # STN firmware version
        "STDI",              # STN device identifier
        "STDIX",             # extended device identifier
        "STSN",              # serial number
        "STPRS",             # report current protocol
        "STPO",              # protocol open (no vehicle write)
        "STPC",              # protocol close
        "STCSEGR",           # report CAN segmentation state
    }
)

#: Adapter commands that take a bounded parameter.
_ALLOWED_AT_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^ATSP[0-9A-C]$"),     # select protocol 0-C (auto through user)
    re.compile(r"^ATTP[0-9A-C]$"),     # try protocol
    re.compile(r"^ATST[0-9A-F]{2}$"),  # response timeout
    re.compile(r"^ATSH[0-9A-F]{3,6}$"),# set header (request addressing only)
    re.compile(r"^ATCRA[0-9A-F]{3,8}$"),  # CAN receive address filter
    re.compile(r"^ATCF[0-9A-F]{3,8}$"),   # CAN filter
    re.compile(r"^ATCM[0-9A-F]{3,8}$"),   # CAN mask
    re.compile(r"^STP[0-9]{1,2}$"),    # STN set protocol
    # CAN priority byte for 29-bit headers.  ``ATSH`` only carries the low three
    # bytes of a 29-bit ID, so the priority nibble that turns DACBF1 into
    # 0x14DACBF1 has to be set separately.  Addressing configuration only.
    re.compile(r"^ATCP[0-9A-F]{2}$"),
    # ISO 15765-2 flow control.  These decide what the *adapter* sends back
    # when an ECU answers with a multi-frame response; they cannot originate a
    # request.  Mode 09 already needs multi-frame reception, so this is read
    # path plumbing rather than anything new in kind.
    re.compile(r"^ATFCSH[0-9A-F]{3,8}$"),   # flow control header
    re.compile(r"^ATFCSD[0-9A-F]{2,10}$"),  # flow control data (max 5 bytes)
    re.compile(r"^ATFCSM[0-2]$"),           # flow control mode
)

_HEX_ONLY = re.compile(r"^[0-9A-F]+$")

# Requests that carry one PID/MID parameter byte.
_MODES_WITH_PID: Final[frozenset[str]] = frozenset({"01", "06", "09"})

# Freeze frame requests carry a PID *and* a frame number, so their payload is
# two bytes rather than one.  Giving 02 its own shape keeps the check exact
# instead of loosening the one-byte rule for every mode that uses it.
_MODES_WITH_PID_AND_FRAME: Final[frozenset[str]] = frozenset({"02"})

# Everything else in the allowlist is a bare service request.
_MODES_WITHOUT_PID: Final[frozenset[str]] = frozenset({"03", "07", "0A"})

MAX_COMMAND_LENGTH: Final[int] = 32


def normalize(command: str) -> str:
    """Return the canonical uppercase, whitespace-free form of *command*.

    Normalisation never changes meaning: it only removes the spacing an
    operator may type (``01 0C`` -> ``010C``) and upper-cases hex digits.
    """
    if not isinstance(command, str):
        raise UnsafeCommandError(f"command must be a string, got {type(command)!r}")
    return "".join(command.split()).upper()


def _reject(command: str, reason: str) -> "UnsafeCommandError":
    return UnsafeCommandError(f"refused {command!r}: {reason}")


def validate_command(command: str) -> str:
    """Validate *command* and return its normalised form.

    Raises :class:`UnsafeCommandError` if the command is not on the read-only
    allowlist.  Callers must treat this as the last gate before serial I/O.
    """
    if command is None:
        raise UnsafeCommandError("refused None: no command")
    raw = command
    if any(ch in raw for ch in ("\r", "\n", ";", "\x00")):
        raise _reject(raw, "command batching/termination characters are not allowed")

    cmd = normalize(raw)
    if not cmd:
        raise _reject(raw, "empty command")
    if len(cmd) > MAX_COMMAND_LENGTH:
        raise _reject(raw, f"longer than {MAX_COMMAND_LENGTH} characters")
    if not re.fullmatch(r"[0-9A-Z@]+", cmd):
        raise _reject(raw, "contains characters outside [0-9A-Z@]")

    if cmd.startswith("AT") or cmd.startswith("ST"):
        if cmd in _ALLOWED_AT_EXACT:
            return cmd
        for pattern in _ALLOWED_AT_PATTERNS:
            if pattern.match(cmd):
                return cmd
        raise _reject(raw, "adapter command is not on the read-only allowlist")

    if not _HEX_ONLY.match(cmd):
        raise _reject(raw, "not a hexadecimal OBD request")
    if len(cmd) < 2:
        raise _reject(raw, "hexadecimal request is shorter than one service byte")

    mode = cmd[:2]
    if mode in FORBIDDEN_SERVICES:
        raise _reject(raw, f"service {mode} is permanently forbidden (write/control/clear)")
    if mode not in ALLOWED_OBD_MODES:
        raise _reject(raw, f"service {mode} is not in the read-only allowlist {sorted(ALLOWED_OBD_MODES)}")

    payload = cmd[2:]
    if mode in _MODES_WITHOUT_PID:
        # A bare service request, optionally with a response count ("0301").
        if not payload:
            return cmd
        if len(payload) == 1 and payload in "0123456789ABCDEF":
            return cmd
        raise _reject(raw, f"service {mode} takes no parameter")
    if mode in _MODES_WITH_PID:
        # One PID byte, optionally followed by an explicit response count
        # (for example "010C1"), which ELM/STN adapters accept and which stops
        # the adapter waiting out its full timeout on every request.
        if len(payload) == 2:
            return cmd
        if len(payload) == 3 and payload[2] in "0123456789ABCDEF":
            return cmd
        raise _reject(raw, f"service {mode} takes one PID byte and an optional response count")
    if mode in _MODES_WITH_PID_AND_FRAME:
        # "0202" (PID, frame 0 implied) or "020200" (PID and frame number),
        # each optionally followed by a response count.
        if len(payload) in (2, 4):
            return cmd
        if len(payload) in (3, 5) and payload[-1] in "0123456789ABCDEF":
            return cmd
        raise _reject(
            raw,
            f"service {mode} takes a PID byte, an optional frame byte, and an "
            f"optional response count",
        )

    raise _reject(raw, "unhandled command shape")  # pragma: no cover - defensive


#: Enhanced (UDS service ``22`` ReadDataByIdentifier) identifiers this project
#: will transmit *under supervision only*.  This is an exact enumeration, not a
#: range and not a prefix: an identifier that is not a key here is refused.
#:
#: The rule for adding an entry is that a **public source must name the exact
#: identifier**, so that nothing here is a guess.  "Plausible", "adjacent to a
#: known one", or "the next number up" are not sources.  Sweeping the
#: identifier space is what this structure exists to prevent -- an ECU asked
#: for an identifier it does not have answers ``7F 22 31``, but an ECU asked
#: for thousands of them in sequence is being probed, and that is neither
#: read-only in spirit nor something this project does to someone's vehicle.
#:
#: Provenance for each entry is recorded in ``docs/GM_ENHANCED_CANDIDATES.md``.
#: Being listed here means "may be transmitted", **not** "is known to work on
#: this VIN" -- validation status lives in the documentation, not the gate.
#: A note on "adjacent" identifiers.  An earlier version of this file used
#: 0x27C7 as the example of an identifier a sweep would try next, on the
#: assumption that it was fictional.  It is not: 0x27C7 is a documented range
#: identifier on this platform.  Nearness to a real identifier is therefore no
#: evidence either way, which is precisely why the rule is enumeration rather
#: than distance -- and why finding a source is the only way in.
ENHANCED_READ_DIDS: Final[dict[str, str]] = {
    "27C6": (
        "HV battery state of charge -- meatpiHQ/wican-fw vehicle_profiles/bt1/"
        "bt1.json, a profile whose car_model names the Hummer EV explicitly; "
        "independently attested in vehicle_profiles/gmc/sierra-ev.json"
    ),
    "27AF": (
        "HV battery energy remaining -- meatpiHQ/wican-fw vehicle_profiles/gmc/"
        "sierra-ev.json (HV_CAPACITY_R). Sierra EV is BT1, the same platform "
        "family bt1.json groups with the Hummer EV"
    ),
    "27C7": (
        "remaining range -- meatpiHQ/wican-fw vehicle_profiles/gmc/"
        "sierra-ev.json (RANGE), BT1 platform family"
    ),
    "27C0": (
        "distance since full charge -- meatpiHQ/wican-fw vehicle_profiles/gmc/"
        "sierra-ev.json (DIST_SINCE_FULL_CHARGE), BT1 platform family"
    ),
    "0046": (
        "temperature -- meatpiHQ/wican-fw vehicle_profiles/gmc/sierra-ev.json "
        "(TMP_A), BT1 platform family"
    ),
    "5401": (
        "DC charger power -- meatpiHQ/wican-fw vehicle_profiles/gmc/"
        "sierra-ev.json (CHARGER_DC_PWR), BT1 platform family"
    ),
    # From OBDb/Chevrolet-Equinox-EV signalsets/v3/default.json, fetched
    # 2026-09-02.  That is a BEV3 vehicle rather than BT1, so these are a
    # weaker claim than the sierra-ev entries above and may simply answer
    # 7F 22 31.  They are included because the file addresses the same module
    # addresses this vehicle has already named for itself, and because its
    # entry for 0x27C6 (16-bit, *100/65535) is arithmetically identical to the
    # /655.35 this vehicle has already confirmed -- an independent third source
    # agreeing on an identifier we have measured.
    "2AF5": (
        "HV battery cell voltage average/minimum/maximum -- "
        "OBDb/Chevrolet-Equinox-EV signalsets/v3/default.json, hdr DACB, "
        "three 16-bit fields divided by 10000, volts"
    ),
    "2B43": (
        "HV battery state of charge, 8-bit -- OBDb/Chevrolet-Equinox-EV "
        "signalsets/v3/default.json, hdr DACB, byte * 100 / 255, percent"
    ),
    # Chassis identifiers from OBDb/Cadillac-LYRIQ test fixtures
    # (tests/test_cases/2024/commands/DA28.*.yaml), fetched 2026-09-02.  Those
    # files pair a captured response with its expected decoded value, so the
    # scaling below was *derived arithmetically from the vectors* and matches
    # every one exactly -- a stronger form of evidence than a stated formula.
    #
    # The same directory's three DACB identifiers are the ones this vehicle has
    # already answered, so the fixture set is three-for-three on this truck
    # before these five are tried.  Address 28 is BSCM-BrakeSystem, which this
    # vehicle names for itself.
    "4A7A": (
        "wheel speed, four corners -- OBDb/Cadillac-LYRIQ test fixture "
        "DA28.224A7A, one byte per wheel FL/FR/RL/RR, km/h"
    ),
    "4A7C": (
        "brake pressure -- OBDb/Cadillac-LYRIQ test fixture DA28.224A7C, "
        "(byte - 10) * 100, kPa"
    ),
    "4C2D": (
        "steering wheel angle -- OBDb/Cadillac-LYRIQ test fixture DA28.224C2D, "
        "signed 16-bit * 0.022, degrees"
    ),
    "4C2F": (
        "lateral acceleration -- OBDb/Cadillac-LYRIQ test fixture DA28.224C2F, "
        "signed 16-bit * 0.0015928, g"
    ),
    "4C30": (
        "longitudinal acceleration -- OBDb/Cadillac-LYRIQ test fixture "
        "DA28.224C30, signed 16-bit * 0.0015928, g"
    ),
    # The two highest-value identifiers in the project: traction pack voltage
    # and pack current, both at module 17.  Both are WEAKER sources than
    # anything else on this list and are labelled as such:
    #
    #   2885 - meatpiHQ/wican-fw issue #884, an OPEN, UNMERGED, zero-comment
    #          report about a 2027 Chevrolet Bolt (BEV3).  One person, never
    #          reviewed, and wican-fw disagrees with itself about the byte
    #          window for this exact header elsewhere.
    #   2414 - OBDb/Cadillac-LYRIQ open PR #14, a 2025 Lyriq (BEV3).  Also
    #          unmerged -- but it ships TEST VECTORS pairing a captured frame
    #          with its expected value, and the stated formula reproduces both
    #          exactly, which is a materially stronger claim than 2885's.
    #
    # Neither is BT1.  Both are pure service 22 reads, so the worst outcome of
    # asking is 7F 22 31.  They are allowlisted to be *tested*, and nothing
    # here asserts they work on this vehicle.
    "2885": (
        "HV traction pack voltage candidate -- meatpiHQ/wican-fw issue #884, "
        "DMCM1_BATTERY_PACK_VOLTAGE, [B4:B5]/100 volts, min 0 max 500. "
        "UNMERGED single-author report, 2027 Bolt BEV3, not BT1"
    ),
    "2414": (
        "HV pack current candidate -- OBDb/Cadillac-LYRIQ PR #14, LYRIQ_HVBAT_A, "
        "signed 16-bit / 20 amps, negative = charging. Ships test vectors "
        "(0xFE39 -> -22.75 A, 0x0012 -> 0.9 A) which the formula reproduces "
        "exactly. UNMERGED, 2025 Lyriq BEV3, not BT1"
    ),
    # ------------------------------------------------------------------
    # ISO 14229-1 standardised DataIdentifiers.  These are not vendor
    # identifiers and not guesses: the standard assigns these exact values in
    # the "identification" range, and any ECU implementing service 22 is
    # expected to answer at least some of them.
    #
    # They exist here for one purpose -- answering "is this module reachable at
    # all", which is a different question from "does this module have that
    # vendor identifier".  Module 40 returned NO DATA to nine sourced vendor
    # identifiers on 2026-09-03.  NO DATA means nothing replied, so the vendor
    # identifiers were never really the thing under test; the route was.  A
    # standard identifier separates those two questions: an answer proves the
    # module is reachable and speaks service 22, and 7F 22 31 proves the same
    # while saying it lacks that particular one.  Only continued silence means
    # the request is not arriving.
    #
    # F190 (VIN) is deliberately NOT included.  It would answer the same
    # question while pulling vehicle identity into an evidence file, and the
    # part and version numbers below establish reachability without it.
    "F187": ("ISO 14229-1 vehicleManufacturerSparePartNumber -- standard "
             "identification DID, used here to test reachability, not content"),
    "F188": ("ISO 14229-1 vehicleManufacturerECUSoftwareNumber -- standard "
             "identification DID, used here to test reachability"),
    "F189": ("ISO 14229-1 vehicleManufacturerECUSoftwareVersionNumber -- "
             "standard identification DID, used here to test reachability"),
    "F191": ("ISO 14229-1 vehicleManufacturerECUHardwareNumber -- standard "
             "identification DID, used here to test reachability"),

    # ------------------------------------------------------------------
    # Sourced candidates added 2026-09-03, none proven on this vehicle.
    # Every one is a service 22 read, so the worst outcome of asking is a
    # negative response.  They are here to be TESTED.  Nothing below claims
    # they work, and no decoder consumes them until a Hummer answer is
    # cross-checked against independently observable vehicle state.
    #
    # Group 1 -- battery system manager (CB), the module this vehicle already
    # answers 27C6/27AF/27C7/27C0/0046/5401/2AF5/2B43 from.
    # Source: meatpiHQ/wican-fw issue #884, a 2027 Bolt on the same Ultium/BEV3
    # extended-addressing scheme.  UNMERGED single-author report, not BT1.
    "27BF": ("charge-cycle regeneration-related field candidate -- "
             "meatpiHQ/wican-fw issue #884, BEV3 Bolt, UNMERGED, not BT1"),
    "27BB": ("thermal-management energy candidate -- meatpiHQ/wican-fw "
             "issue #884, BEV3 Bolt, UNMERGED, not BT1"),
    "27B5": ("thermal-management distance candidate -- meatpiHQ/wican-fw "
             "issue #884, BEV3 Bolt, UNMERGED, not BT1"),
    "2709": ("A/C compressor temperature candidate -- meatpiHQ/wican-fw "
             "issue #884, BEV3 Bolt, UNMERGED, not BT1"),
    "2AF1": ("battery module temperature candidate -- meatpiHQ/wican-fw "
             "issue #884, BEV3 Bolt, UNMERGED, not BT1"),
    #
    # Group 2 -- body control module (40).  This vehicle named 40 as
    # BCM-BodyControl in its own service 09 module inventory, and nothing has
    # ever been asked of it.  Source: OBDb/Cadillac-LYRIQ PR #14, which ships
    # real-vehicle test vectors; the same PR is where 2414 came from, and 2414
    # is proven on this vehicle, which is the reason to take the rest of its
    # register families seriously.  UNMERGED, 2025 Lyriq BEV3, not BT1.
    "4149": ("EVSE advertised/pilot current candidate -- OBDb/Cadillac-LYRIQ "
             "PR #14, module 40, UNMERGED, not BT1"),
    "416C": ("HV battery group voltage 1 candidate -- OBDb/Cadillac-LYRIQ "
             "PR #14, module 40, UNMERGED, not BT1"),
    "416D": ("HV battery group voltage 2 candidate -- OBDb/Cadillac-LYRIQ "
             "PR #14, module 40, UNMERGED, not BT1"),
    "416E": ("HV battery group voltage 3 candidate -- OBDb/Cadillac-LYRIQ "
             "PR #14, module 40, UNMERGED, not BT1"),
    "434F": ("HV battery temperature candidate -- OBDb/Cadillac-LYRIQ PR #14, "
             "module 40, UNMERGED, not BT1"),
    "4127": ("HV battery temperature A candidate -- OBDb/Cadillac-LYRIQ PR "
             "#14, module 40, UNMERGED, not BT1"),
    "4124": ("HV battery temperature B candidate -- OBDb/Cadillac-LYRIQ PR "
             "#14, module 40, UNMERGED, not BT1"),
    "40E5": ("battery coolant temperature 1 candidate -- OBDb/Cadillac-LYRIQ "
             "PR #14, module 40, UNMERGED, not BT1"),
    "40E6": ("battery coolant temperature 2 candidate -- OBDb/Cadillac-LYRIQ "
             "PR #14, module 40, UNMERGED, not BT1"),
    "2429": ("nominal battery voltage -- OBDb/Cadillac-LYRIQ PR #14 "
             "(LYRIQ_HVBAT_NOMINAL_V), hdr DA17, 16-bit / 64 volts, max 1023. "
             "The source calls it the constant rated pack voltage rather than a "
             "live measurement, so a value that does not move is the expected "
             "result and not a failed decode. UNMERGED, 2025 Lyriq BEV3, "
             "not BT1"),
    "33E5": (
        "drive motor control module battery voltage -- "
        "OBDb/Chevrolet-Equinox-EV signalsets/v3/default.json, hdr DA1D "
        "(this vehicle names 1D as DMC2-DriveMotorCtrl2), byte / 10, volts"
    ),
}

# The production gate must never learn service 22.  If a future edit adds it to
# ALLOWED_OBD_MODES, the collector would start sending enhanced reads
# unattended, which is exactly the outcome the two-gate split exists to make
# impossible.  Fail at import rather than on a vehicle.
assert "22" not in ALLOWED_OBD_MODES, (
    "service 22 must never be in the unattended allowlist; "
    "enhanced reads go through validate_enhanced_command()"
)


#: Receive-only CAN monitoring: the adapter listens and does not acknowledge.
#:
#: The vendor's *OBDLink Family Reference and Programming Manual* documents
#: ``STCMM`` modes as 0 = receive only with no CAN ACKs, 1 = normal node *with*
#: ACKs, 2 = receive all frames including errors, no ACKs.  On CAN a listening
#: node normally asserts a dominant bit in every frame's ACK slot; that is a
#: transmission, short and unaddressed and invisible in any frame log, and it
#: still means the adapter is driving the bus.  Mode 0 is the difference between
#: "we only read" and "we do not transmit", which is the stronger promise this
#: project makes elsewhere and must keep here.
#:
#: An exact string, never a pattern: ``^STCMM.$`` would admit modes 1 and 2.
MONITOR_CAN_MODE: Final[str] = "STCMM0"

#: The one monitor command.  The same manual marks ``ATMA``, ``ATMR`` and
#: ``ATMT`` deprecated in favour of ``STM``/``STMA``, so ``ATMA`` staying
#: refused is a deliberate choice rather than an oversight to tidy up.
#:
#: Exact, never a pattern: ``^STM.*$`` would admit anything beginning "STM".
MONITOR_STREAM_COMMAND: Final[str] = "STMA"


def validate_monitor_setup_command(command: str) -> str:
    """The production gate, plus ``STCMM0``, and nothing else.

    Deliberately **not** implemented by widening ``_ALLOWED_AT_EXACT``.  That
    set feeds :func:`validate_command`, which is the unattended collector's gate
    and the default validator every :class:`SerialTransport` gets.  Putting a
    monitor command there would make it reachable from the collector, which is
    precisely the thing that must not be possible.  A separate function is the
    same shape the enhanced path already uses, and for the same reason.

    Note it accepts the *mode* command but not the *stream* command.  A capture
    tool built on this validator therefore cannot start monitoring through
    ``send()`` even by mistake -- and ``send()`` would be badly wrong for it,
    because a monitor stream never emits the ``>`` prompt that function waits
    for, so it would block for its full timeout and return truncated bytes
    flagged as a timeout.  That failure is unreachable rather than merely
    avoided.
    """
    if command is None:
        raise UnsafeCommandError("refused None: no command")
    cmd = normalize(command)
    if cmd == MONITOR_CAN_MODE:
        return cmd
    return validate_command(command)


def validate_monitor_stream_command(command: str) -> str:
    """Accept exactly :data:`MONITOR_STREAM_COMMAND`, and nothing else.

    Not even ``STCMM0``: the two gates do not overlap, so neither command can
    be used through the other's path.
    """
    if command is None:
        raise UnsafeCommandError("refused None: no command")
    cmd = normalize(command)
    if cmd != MONITOR_STREAM_COMMAND:
        raise _reject(
            command,
            f"the monitor gate accepts only {MONITOR_STREAM_COMMAND}; "
            f"adapter configuration goes through "
            f"validate_monitor_setup_command()",
        )
    return cmd


# The monitor commands must never leak into the production allowlist.  Asserted
# at import so a well-meaning edit fails here rather than on a vehicle.
assert MONITOR_CAN_MODE not in _ALLOWED_AT_EXACT, (
    "STCMM0 must not be in the production allowlist: that set is the "
    "collector's gate"
)
assert MONITOR_STREAM_COMMAND not in _ALLOWED_AT_EXACT, (
    "the monitor command must not be in the production allowlist"
)
assert not any(
    pattern.match(MONITOR_STREAM_COMMAND) or pattern.match(MONITOR_CAN_MODE)
    for pattern in _ALLOWED_AT_PATTERNS
), "no production pattern may admit a monitor command"


def validate_enhanced_command(command: str) -> str:
    """Validate a *supervised* enhanced read and return its normalised form.

    This is a second gate, narrower than :func:`validate_command` rather than
    wider: it accepts adapter commands on exactly the same terms (by delegating
    to ``validate_command``), and it accepts service ``22`` for an identifier
    listed in :data:`ENHANCED_READ_DIDS` -- nothing else.  Every other OBD
    service is refused here, *including the ones the collector is allowed to
    send*, because a caller reaching for this function is asking for the
    experimental path and should not get the routine one by accident.

    Nothing in the unattended collection path calls this.  That separation is
    the safety property, so it is worth stating plainly: enabling an enhanced
    read requires editing :data:`ENHANCED_READ_DIDS` in source and running a
    tool that transmits it explicitly.  There is no flag that widens
    ``validate_command``.
    """
    if command is None:
        raise UnsafeCommandError("refused None: no command")
    raw = command
    if any(ch in raw for ch in ("\r", "\n", ";", "\x00")):
        raise _reject(raw, "command batching/termination characters are not allowed")

    cmd = normalize(raw)
    if not cmd:
        raise _reject(raw, "empty command")

    # Adapter configuration reuses the production gate verbatim, so the
    # experimental path cannot quietly widen what AT/ST commands are legal.
    if cmd.startswith("AT") or cmd.startswith("ST"):
        return validate_command(cmd)

    if len(cmd) > MAX_COMMAND_LENGTH:
        raise _reject(raw, f"longer than {MAX_COMMAND_LENGTH} characters")
    if not _HEX_ONLY.match(cmd):
        raise _reject(raw, "not a hexadecimal request")

    mode = cmd[:2]
    if mode in FORBIDDEN_SERVICES:
        raise _reject(raw, f"service {mode} is permanently forbidden (write/control/clear)")
    if mode != "22":
        raise _reject(
            raw,
            "the enhanced gate only accepts service 22; routine read services "
            "go through validate_command()",
        )

    did = cmd[2:]
    if not did:
        raise _reject(raw, "service 22 requires an identifier")
    # A bare ``22`` with no identifier, or one carrying several identifiers in a
    # single request, is refused: one supervised read means one identifier.
    if len(did) != 4:
        raise _reject(
            raw,
            f"service 22 takes exactly one two-byte identifier, got {len(did)} "
            f"hex digits ({did!r})",
        )
    if did not in ENHANCED_READ_DIDS:
        raise _reject(
            raw,
            f"identifier {did} is not in the supervised enhanced allowlist "
            f"{sorted(ENHANCED_READ_DIDS)}; identifiers are never guessed",
        )
    return cmd


def validate_supervised_command(command: str) -> str:
    """Accept whatever *either* gate accepts, and nothing else.

    The supervised drive recorder needs both: standard PIDs for speed and
    odometer, and the enumerated enhanced identifiers.  Rather than give it a
    third allowlist to drift out of step with the other two, this is the union
    of the existing gates -- so it can never admit a command that neither gate
    allows, and every restriction in either one still applies.

    It is emphatically not the gate for unattended collection.  ``collector.py``
    calls :func:`validate_command`, which refuses service ``22``, and that is
    what keeps enhanced reads a thing a person starts deliberately.
    """
    try:
        return validate_command(command)
    except UnsafeCommandError as ordinary:
        try:
            return validate_enhanced_command(command)
        except UnsafeCommandError:
            # Report the ordinary gate's reason: for anything that is not a
            # service 22 request -- which is most mistakes -- it is the more
            # useful of the two messages.
            raise ordinary from None


def is_safe(command: str) -> bool:
    """Return ``True`` if :func:`validate_command` would accept *command*."""
    try:
        validate_command(command)
    except UnsafeCommandError:
        return False
    return True


def describe_command(command: str) -> str:
    """Human-readable description used in logs and the runbook."""
    cmd = normalize(command)
    if cmd.startswith("AT") or cmd.startswith("ST"):
        return f"adapter command {cmd}"
    mode = cmd[:2]
    names = {
        "01": "current data",
        "02": "freeze frame data",
        "03": "stored DTCs",
        "06": "on-board monitoring test results",
        "07": "pending DTCs",
        "09": "vehicle information",
        "0A": "permanent DTCs",
    }
    label = names.get(mode, "unknown service")
    if len(cmd) > 2:
        return f"service {mode} ({label}) PID {cmd[2:4]}"
    return f"service {mode} ({label})"


def validate_all(commands: Iterable[str]) -> list[str]:
    """Validate a sequence of commands, returning their normalised forms."""
    return [validate_command(c) for c in commands]
