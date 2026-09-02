"""Adapter session: fingerprint, protocol selection and read-only queries.

The session owns the ordered, read-only conversation with the adapter:

1. reset and quiet the adapter (``ATZ``, ``ATE0``, ``ATL0``, ``ATS0``),
2. turn headers on so responding ECUs are identifiable (``ATH1``),
3. identify the adapter (``ATI``, ``AT@1``, ``STI``, ``STDI``) and read the
   connector voltage (``ATRV``),
4. let the adapter auto-detect the vehicle protocol (``ATSP0`` then ``0100``)
   and record what it chose (``ATDP``/``ATDPN``),
5. answer read-only questions: supported PIDs, current data, DTC reads and
   service 09 vehicle information.

No step here can transmit anything the safety gate has not approved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .decode import (
    AdapterReply,
    decode_ascii_item,
    decode_dtcs,
    decode_pid,
    decode_vin,
    parse_reply,
    supported_pids,
    supported_service09_pids as _decode_service09_support,
)
from .transport import Transport, TransportError

__all__ = ["AdapterSession", "Fingerprint"]

#: Adapter setup, in order.  Every entry is on the safety allowlist.
INIT_SEQUENCE = ("ATZ", "ATE0", "ATL0", "ATS0", "ATH1", "ATAT1")

#: Informational adapter queries.  ``ST*`` commands only answer on STN chips
#: (OBDLink); an ELM327 clone answers ``?`` and that is recorded as evidence.
IDENT_SEQUENCE = ("ATI", "AT@1", "AT@2", "STI", "STDI", "ATRV")

#: Support bitmaps for service 01 and service 09.
SUPPORT_PIDS_01 = ("0100", "0120", "0140", "0160", "0180", "01A0", "01C0")


@dataclass
class Fingerprint:
    adapter_id: str = ""
    device_description: str = ""
    device_identifier: str = ""
    stn_version: str = ""
    stn_device_id: str = ""
    voltage: str = ""
    protocol: str = ""
    protocol_number: str = ""
    responses: dict[str, str] = field(default_factory=dict)


class AdapterSession:
    """A read-only conversation with the OBD adapter."""

    def __init__(self, transport: Transport, *, logger=None) -> None:
        self.transport = transport
        self.log = logger
        self.fingerprint = Fingerprint()

    # -- helpers ---------------------------------------------------------
    def _say(self, message: str) -> None:
        if self.log:
            self.log(message)

    def ask(self, command: str, timeout: Optional[float] = None) -> AdapterReply:
        """Send one command and return its parsed reply (raw bytes are logged)."""
        response = self.transport.send(command, timeout=timeout)
        reply = parse_reply(response.data)
        return reply

    def _text(self, reply: AdapterReply) -> str:
        return " / ".join(reply.lines)

    # -- start-up --------------------------------------------------------
    def initialize(self) -> Fingerprint:
        for command in INIT_SEQUENCE:
            reply = self.ask(command, timeout=6.0)
            self.fingerprint.responses[command] = self._text(reply)
            self._say(f"{command}: {self._text(reply)}")

        for command in IDENT_SEQUENCE:
            reply = self.ask(command, timeout=6.0)
            text = self._text(reply)
            self.fingerprint.responses[command] = text
            self._say(f"{command}: {text}")
            if command == "ATI":
                self.fingerprint.adapter_id = text
            elif command == "AT@1":
                self.fingerprint.device_description = text
            elif command == "AT@2":
                self.fingerprint.device_identifier = text
            elif command == "STI":
                self.fingerprint.stn_version = text
            elif command == "STDI":
                self.fingerprint.stn_device_id = text
            elif command == "ATRV":
                self.fingerprint.voltage = text
        return self.fingerprint

    def negotiate_protocol(self, timeout: float = 20.0) -> Fingerprint:
        """Ask the adapter to auto-detect the vehicle protocol.

        ``0100`` is a standard read-only request for the service 01 support
        bitmap; it is what forces protocol detection.  A sleeping vehicle
        answers ``NO DATA`` / ``UNABLE TO CONNECT``, which is recorded and is
        not an error to retry aggressively.
        """
        reply = self.ask("ATSP0", timeout=6.0)
        self.fingerprint.responses["ATSP0"] = self._text(reply)
        probe = self.ask("0100", timeout=timeout)
        self.fingerprint.responses["0100"] = self._text(probe)
        self._say(f"0100: {self._text(probe)} [{probe.status}]")
        for command, attr in (("ATDP", "protocol"), ("ATDPN", "protocol_number")):
            reply = self.ask(command, timeout=6.0)
            text = self._text(reply)
            self.fingerprint.responses[command] = text
            setattr(self.fingerprint, attr, text)
            self._say(f"{command}: {text}")
        return self.fingerprint

    # -- read-only queries ----------------------------------------------
    def supported_service01_pids(self) -> list[str]:
        found: list[str] = []
        for command in SUPPORT_PIDS_01:
            reply = self.ask(command, timeout=8.0)
            base = command[2:4]
            pids = supported_pids(reply, base)
            self._say(f"{command}: {reply.status} -> {len(pids)} pids")
            if not pids:
                break
            found.extend(pids)
            # Only continue to the next bank if this bank advertises it.
            next_bank = f"{int(base, 16) + 0x20:02X}"
            if next_bank not in pids:
                break
        return sorted(set(found))

    def supported_service09_items(self) -> list[str]:
        """Ask which service 09 items the vehicle advertises.

        Service 09 has a single support bitmap at ``0900`` rather than the
        chain of banks service 01 uses, so one request is enough.
        """
        reply = self.ask("0900", timeout=8.0)
        items = _decode_service09_support(reply, "00")
        self._say(f"0900: {reply.status} -> {len(items)} items")
        return items

    def read_pid(self, pid: str, timeout: float = 6.0):
        command = f"01{pid.upper()}"
        reply = self.ask(command, timeout=timeout)
        return decode_pid(pid, reply), reply

    def read_dtcs(self, mode: str = "03", timeout: float = 10.0):
        reply = self.ask(mode, timeout=timeout)
        return decode_dtcs(mode, reply), reply

    def read_vin(self, timeout: float = 12.0):
        reply = self.ask("0902", timeout=timeout)
        return decode_vin(reply), reply

    def read_service09_item(self, pid: str, timeout: float = 10.0):
        reply = self.ask(f"09{pid.upper()}", timeout=timeout)
        return decode_ascii_item(reply, int(pid, 16)), reply
