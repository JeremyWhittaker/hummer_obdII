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
* Mode ``22`` (enhanced read-by-identifier) is *not* enabled in this build.  It
  is read-only in principle, but GM/Ultium identifiers are unproven on this VIN
  and the current assignment defers them.
"""

from __future__ import annotations

import re
from typing import Final, Iterable

__all__ = [
    "UnsafeCommandError",
    "ALLOWED_OBD_MODES",
    "FORBIDDEN_SERVICES",
    "normalize",
    "validate_command",
    "is_safe",
    "describe_command",
]


class UnsafeCommandError(ValueError):
    """Raised when a command is not provably read-only."""


#: OBD-II services this project is allowed to request.
#:   01 current data, 03 stored DTCs, 07 pending DTCs,
#:   09 vehicle information, 0A permanent DTCs.
ALLOWED_OBD_MODES: Final[frozenset[str]] = frozenset({"01", "03", "07", "09", "0A"})

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
)

_HEX_ONLY = re.compile(r"^[0-9A-F]+$")

# Requests that carry a PID/parameter byte.  Everything else in the allowlist
# is a bare service request.
_MODES_WITH_PID: Final[frozenset[str]] = frozenset({"01", "09"})
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

    raise _reject(raw, "unhandled command shape")  # pragma: no cover - defensive


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
        "03": "stored DTCs",
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
