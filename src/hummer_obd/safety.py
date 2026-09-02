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
    "normalize",
    "validate_command",
    "validate_enhanced_command",
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
ENHANCED_READ_DIDS: Final[dict[str, str]] = {
    "27C6": (
        "HV battery state of charge -- meatpiHQ/wican-fw vehicle_profiles/bt1/"
        "bt1.json, a profile whose car_model names the Hummer EV explicitly"
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
