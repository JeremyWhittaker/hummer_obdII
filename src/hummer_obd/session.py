"""Adapter session: fingerprint, protocol selection and read-only queries.

The session owns the ordered, read-only conversation with the adapter:

1. reset and quiet the adapter (``ATZ``, ``ATE0``, ``ATL0``, ``ATS0``),
2. turn headers on so responding ECUs are identifiable (``ATH1``),
3. identify the adapter (``ATI``, ``AT@1``, ``STI``, ``STDI``) and read the
   connector voltage (``ATRV``),
4. let the adapter auto-detect the vehicle protocol (``ATSP0`` then ``0100``)
   and record what it chose (``ATDP``/``ATDPN``),
5. answer read-only questions: supported PIDs, current data, freeze frames,
   on-board monitor results, DTC reads and service 09 vehicle information.

Several modules answer the same request, so the answers are kept per module
wherever that is possible.  Collapsing them to one value would let whichever
ECU happened to reply first speak for the whole truck.

No step here can transmit anything the safety gate has not approved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from .decode import (
    AdapterReply,
    decode_ascii_item,
    decode_ascii_items,
    decode_dtcs,
    decode_freeze_frame,
    supported_freeze_frame_pids as _decode_freeze_frame_support,
    decode_monitor_tests,
    decode_pid,
    decode_pid_per_ecu,
    decode_vin,
    ecu_from_header,
    parse_reply,
    supported_mids,
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

#: Support bitmaps for service 06 monitor IDs.  Service 06 numbers its monitors
#: in banks of 32 exactly as service 01 numbers its PIDs, so the same walk
#: applies: ask ``0600``, and only ask ``0620`` when the first bitmap says the
#: vehicle has a MID 20 to point at.
SUPPORT_MIDS_06 = ("0600", "0620", "0640", "0660", "0680", "06A0", "06C0")

#: The 29-bit response identifier this vehicle answers on.  ISO 15765-4
#: extended addressing replies to ``18DAF1<ecu>``: ``F1`` is the tester, and the
#: final byte names the module that spoke.
RESPONSE_ID_PREFIX = "18DAF1"

#: Restores unfiltered CAN reception after a single-module query.  A bare
#: ``ATCRA`` is the adapter's own reset for the receive filter, but it is not on
#: the safety allowlist and must not be added for this.  An all-zero CAN mask is
#: equivalent: the adapter accepts a frame when ``id & mask`` equals
#: ``filter & mask``, and a zero mask makes that true for every identifier.
RECEIVE_FILTER_CLEAR = "ATCM00000000"


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

    def supported_monitor_mids(self) -> list[str]:
        """Ask which service 06 on-board monitors the vehicle advertises.

        The banks are walked the same way service 01's PID banks are: the next
        request is only sent when the bitmap just read actually points at it.
        Asking for every bank unconditionally would put six pointless requests
        on a live bus and make a vehicle that supports one bank look like one
        that timed out five times.
        """
        found: list[str] = []
        for command in SUPPORT_MIDS_06:
            reply = self.ask(command, timeout=8.0)
            base = command[2:4]
            mids = supported_mids(reply, base)
            self._say(f"{command}: {reply.status} -> {len(mids)} mids")
            if not mids:
                break
            found.extend(mids)
            next_bank = f"{int(base, 16) + 0x20:02X}"
            if next_bank not in mids:
                break
        return sorted(set(found))

    def read_monitor_tests(self, mid: str, timeout: float = 8.0):
        """Read the on-board monitoring test results for one monitor ID.

        Service 06 is what the ECU concluded from its own self-tests, with the
        limits it judged them against.  It is read-only in the strongest sense:
        the numbers already exist inside the module, and asking for them starts
        no test.
        """
        reply = self.ask(f"06{mid.upper()}", timeout=timeout)
        return decode_monitor_tests(reply), reply

    def supported_freeze_frame_pids(self) -> list[str]:
        """Ask what a freeze frame would hold, without needing one to exist.

        Worth asking even with no stored codes: it proves the service 02 path
        end to end and records which readings a future frame would carry.  A
        vehicle with nothing stored may answer with an empty bitmap or with no
        data at all, and both are recorded rather than treated as failures.
        """
        reply = self.ask("020000", timeout=8.0)
        pids = _decode_freeze_frame_support(reply)
        self._say(f"020000: {reply.status} -> {len(pids)} pids")
        return pids

    def read_freeze_frame(self, pid: str, frame: int = 0, timeout: float = 8.0):
        """Read the stored freeze frame value of *pid* from snapshot *frame*.

        A freeze frame is the snapshot an ECU kept of the moment it set a
        trouble code, so it is only worth asking for when a code exists; the
        caller decides that, because only the caller has seen the DTC reads.
        """
        command = f"02{pid.upper()}{frame:02X}"
        reply = self.ask(command, timeout=timeout)
        return decode_freeze_frame(pid, reply, frame=frame), reply

    def read_pid_per_ecu(self, pid: str, timeout: float = 6.0):
        """Read *pid* and keep every module's answer instead of the first.

        The request is byte for byte the one :meth:`read_pid` sends -- this is
        not extra bus traffic, it is the same traffic read honestly.  On this
        truck several modules answer the same PID with different numbers, and
        reporting only the first makes the others invisible.
        """
        command = f"01{pid.upper()}"
        reply = self.ask(command, timeout=timeout)
        values = decode_pid_per_ecu(pid, reply)
        if not values:
            # Never hand back an empty list.  Silence is an observation, and the
            # caller has to be able to record it in the same shape as a reading
            # rather than special-casing "nothing came back" at every call site.
            values = [decode_pid(pid, reply)]
        return values, reply

    def ecu_name_map(self, addresses: Iterable[str], timeout: float = 8.0) -> dict[str, str]:
        """Ask each responding module for its own name, one module at a time.

        Every module answers ``090A`` at once and the adapter prints the replies
        interleaved, so the only way to say *which* module is called what is to
        listen to one of them at a time.  ``ATCRA18DAF1<addr>`` narrows reception
        to a single responder.  ``ATSH`` cannot do this job: the allowlisted
        header pattern stops at six hex digits and a 29-bit request header needs
        eight, and the allowlist is not going to be widened for a convenience.

        The filter is always taken off again, including when a request fails.  A
        filter left in place would quietly turn every later request into a
        one-module answer, and nothing downstream reports the difference.
        """
        names: dict[str, str] = {}
        # Only a 29-bit module address can be turned into a receive filter.  An
        # 11-bit identifier such as 7E8 would build a command the safety gate
        # rejects, so it is skipped rather than mangled into a valid-looking one.
        # Only 29-bit sources, which are two hex digits.  The receive filter
        # below is built as ``ATCRA18DAF1<addr>``, a 29-bit response identifier;
        # feeding an 11-bit source ("7E8") into that shape would transmit a
        # filter that matches nothing and quietly return a map of empty names.
        # Supporting 11-bit needs its own verified filter form, not a reused
        # one, so those addresses are skipped and reported rather than mangled.
        seen = {a for a in (ecu_from_header(x) for x in addresses) if a}
        wanted = sorted({a for a in seen if len(a) == 2})
        for skipped in sorted(seen - set(wanted)):
            self._say(f"090A @{skipped}: skipped (11-bit source; filter here is 29-bit)")
        if not wanted:
            return names
        try:
            for address in wanted:
                self.ask(f"ATCRA{RESPONSE_ID_PREFIX}{address}", timeout=4.0)
                reply = self.ask("090A", timeout=timeout)
                values = decode_ascii_items(reply, 0x0A)
                # Exactly one answer means the filter took effect and the name
                # belongs to this address.  Several answers mean it did not, and
                # any name picked out of them would be a guess -- so record
                # nothing.  A wrong module name reads as fact; a blank does not.
                names[address] = values[0] if len(values) == 1 else ""
                self._say(f"090A @{address}: {names[address] or reply.status}")
        finally:
            self.ask(RECEIVE_FILTER_CLEAR, timeout=4.0)
        return names
