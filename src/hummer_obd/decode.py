"""Decoders for the read-only OBD-II responses this project requests.

Only services 01, 03, 07, 09 and 0A are decoded.  Decoding never mutates or
replaces the raw transcript; it is a convenience layer over bytes that were
already written to the append-only log.

Adapter responses arrive as ASCII hex text, optionally with headers, spaces,
line feeds and a trailing ``>`` prompt, and may be multi-line (ISO-TP frames
reassembled by the adapter, or one line per responding ECU).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

__all__ = [
    "AdapterReply",
    "split_can_header",
    "parse_reply",
    "decode_pid",
    "decode_dtcs",
    "decode_vin",
    "supported_pids",
    "supported_service09_pids",
    "decode_ascii_items",
    "decode_cvns",
    "negative_response_name",
    "PID_DECODERS",
    "mask_vin",
]

_NO_DATA_MARKERS = (
    "NO DATA",
    "STOPPED",
    "UNABLE TO CONNECT",
    "CAN ERROR",
    "BUS INIT",
    "BUS ERROR",
    "DATA ERROR",
    "BUFFER FULL",
    "ERROR",
    "?",
)

_NEGATIVE_RESPONSE_CODES = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceedNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x78: "responsePending",
}


def negative_response_name(code: int) -> str:
    """Return the standard name for a UDS/OBD negative-response code."""
    return _NEGATIVE_RESPONSE_CODES.get(code, f"NRC_0x{code:02X}")


@dataclass
class AdapterReply:
    """A parsed adapter reply.  ``raw`` is always the untouched decoded text."""

    raw: str
    lines: list[str]
    #: Hex payload bytes per line, when the line is pure hex.
    frames: list[bytes]
    status: str  # "ok", "negative_response", "text", "incomplete", "no_data", "error", "empty"
    marker: str = ""
    #: CAN identifiers seen on responding lines, when headers are enabled.
    headers: list[str] = field(default_factory=list)
    #: Multi-frame messages whose consecutive frames never arrived.  They are
    #: counted, never decoded: a truncated payload must not look like an answer.
    incomplete: int = 0
    #: ``(requested service, response code)`` pairs from ``7F xx yy`` frames.
    negative_responses: list[tuple[int, int]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def parse_reply(data: bytes | str) -> AdapterReply:
    """Split an adapter reply into status and hex frames without losing text."""
    text = data.decode("ascii", errors="replace") if isinstance(data, (bytes, bytearray)) else data
    raw = text
    cleaned = text.replace("\r", "\n")
    lines = [ln.strip() for ln in cleaned.split("\n")]
    lines = [ln for ln in lines if ln and ln != ">"]
    upper = " ".join(lines).upper()
    for marker in _NO_DATA_MARKERS:
        if marker in upper:
            status = "no_data" if marker in ("NO DATA", "STOPPED") else "error"
            return AdapterReply(raw=raw, lines=lines, frames=[], status=status, marker=marker)
    if not lines:
        return AdapterReply(raw=raw, lines=[], frames=[], status="empty")

    incomplete: list[bytes] = []
    frames, header_hex = _extract_frames(lines, incomplete)
    # A reply with no hex frames is not necessarily a failure: "OK", "ELM327
    # v1.4b" and other adapter answers are plain text, and calling those errors
    # buries the ones that matter.
    negative_responses = _find_negative_responses(frames)
    if frames and negative_responses and len(negative_responses) == len(frames):
        status = "negative_response"
    elif frames:
        status = "ok"
    elif incomplete:
        status = "incomplete"
    else:
        status = "text"
    marker = "; ".join(
        f"7F {service:02X} {code:02X} ({negative_response_name(code)})"
        for service, code in negative_responses
    )
    return AdapterReply(raw=raw, lines=lines, frames=frames, status=status,
                        marker=marker, headers=header_hex,
                        incomplete=len(incomplete),
                        negative_responses=negative_responses)


def _find_negative_responses(frames: list[bytes]) -> list[tuple[int, int]]:
    """Extract ``7F <service> <NRC>`` from raw or length-prefixed frames."""
    found: list[tuple[int, int]] = []
    for frame in frames:
        # Auto-formatted replies begin at 7F. Header-preserving ELM replies
        # retain their one-byte ISO-TP length first (for example 03 7F 01 22).
        for offset in (0, 1):
            if len(frame) >= offset + 3 and frame[offset] == 0x7F:
                found.append((frame[offset + 1], frame[offset + 2]))
                break
    return found


_SEGMENT = re.compile(r"^([0-9A-Fa-f]):\s*(.*)$")


def _hex_or_none(text: str) -> Optional[bytes]:
    compact = text.replace(" ", "")
    if not re.fullmatch(r"[0-9A-Fa-f]*", compact) or not compact:
        return None
    if len(compact) % 2:
        # An 11-bit CAN header ("7E8 06 41 ...") leaves an odd digit count;
        # drop it into the header field rather than discarding the frame.
        return None
    return bytes.fromhex(compact)


def _extract_frames(lines: list[str],
                    incomplete: Optional[list[bytes]] = None) -> tuple[list[bytes], list[str]]:
    """Turn adapter text lines into frames, reassembling ISO-TP segments.

    ELM/STN adapters print a long (multi-frame) response as a total-length
    line followed by ``0:``/``1:``/``2:`` segment lines.  Those segments are
    one logical message and must be concatenated before decoding; per-line
    decoding silently loses everything after the first segment (for example
    all but the first three VIN characters).
    """
    headers: list[str] = []
    segments: list[str] = []
    plain: list[bytes] = []
    for line in lines:
        m = _SEGMENT.match(line)
        if m:
            segments.append(m.group(2).replace(" ", ""))
            continue
        compact = line.replace(" ", "")
        if not re.fullmatch(r"[0-9A-Fa-f]+", compact):
            continue
        if len(compact) % 2:
            # Odd length: an 11-bit CAN header prefixes the data bytes.
            headers.append(compact[:3])
            compact = compact[3:]
            if len(compact) % 2 or not compact:
                continue
        elif len(compact) <= 4 and segments == [] and len(lines) > 1:
            # A short standalone value ahead of segment lines is the ISO-TP
            # total length, not a frame.
            if any(_SEGMENT.match(other) for other in lines):
                continue
        plain.append(bytes.fromhex(compact))
    plain = _reassemble_isotp(plain, headers, incomplete)
    if segments:
        joined = "".join(segments)
        if len(joined) % 2:
            joined = joined[:-1]
        return ([bytes.fromhex(joined)] + plain if joined else plain), headers
    return plain, headers


def split_can_header(frame: bytes) -> tuple[bytes, bytes]:
    """Split a 29-bit CAN header off a frame.

    ISO 15765-4 extended addressing puts a four-byte identifier in front of the
    PCI byte (``18 DA F1 45 10 14 49 02 …``), which is what this vehicle uses.
    Without splitting it, byte 0 is ``0x18`` and every frame looks like the
    start of a multi-frame message, so nothing reassembles.
    """
    if len(frame) >= 5 and frame[0] == 0x18 and frame[1] in (0xDA, 0xDB):
        return frame[:4], frame[4:]
    return b"", frame


def _reassemble_isotp(frames: list[bytes], headers: list[str],
                      incomplete: Optional[list[bytes]] = None) -> list[bytes]:
    """Join raw ISO-TP first/consecutive CAN frames into logical messages.

    With headers enabled (``ATH1``) the adapter prints one line per CAN frame,
    including the PCI byte: ``10 14`` starts a 0x14-byte message and ``21``,
    ``22`` … carry the rest.  Decoding those lines independently would keep
    only the first few VIN characters, so they are joined here — per responding
    ECU, because several modules answer the same request and their frames
    interleave in the transcript.
    """
    out: list[bytes] = []
    incomplete = [] if incomplete is None else incomplete
    consumed: set[int] = set()
    for index, frame in enumerate(frames):
        if index in consumed or not frame:
            continue
        header, body = split_can_header(frame)
        if header:
            headers.append(header.hex().upper())
        if len(body) >= 2 and (body[0] & 0xF0) == 0x10:
            total = ((body[0] & 0x0F) << 8) | body[1]
            payload = bytearray(body[2:])
            expected = 0x21
            for later in range(index + 1, len(frames)):
                if later in consumed or len(payload) >= total:
                    continue
                next_header, next_body = split_can_header(frames[later])
                if next_header != header or not next_body:
                    continue
                if next_body[0] != expected:
                    continue
                payload.extend(next_body[1:])
                consumed.add(later)
                expected = 0x20 | ((expected + 1) & 0x0F)
            if len(payload) < total:
                # Consecutive frames are missing.  Emitting the fragment would
                # produce a plausible-looking short VIN; dropping it makes the
                # caller see "no answer", which is the truth.  The bytes
                # themselves are already safe in the append-only raw log.
                incomplete.append(bytes(payload))
                continue
            out.append(bytes(payload[:total]))
            continue
        out.append(body if header else frame)
    return out


def _payload_after(frame: bytes, mode: int, pid: Optional[int]) -> Optional[bytes]:
    """Return the data bytes following a positive response to *mode*/*pid*.

    Handles optional CAN headers by searching for the response mode byte
    (request mode + 0x40) at any even offset near the start of the frame.
    """
    target = mode + 0x40
    for i in range(0, min(len(frame), 8)):
        if frame[i] != target:
            continue
        if pid is None:
            return frame[i + 1:]
        if i + 1 < len(frame) and frame[i + 1] == pid:
            return frame[i + 2:]
    return None


# --- service 01 decoders -------------------------------------------------
def _t(fn, unit, name):
    return {"fn": fn, "unit": unit, "name": name}


PID_DECODERS: dict[str, dict] = {
    "04": _t(lambda d: d[0] * 100 / 255, "%", "calculated engine load"),
    "05": _t(lambda d: d[0] - 40, "degC", "engine coolant temperature"),
    "0B": _t(lambda d: d[0], "kPa", "intake manifold absolute pressure"),
    "0C": _t(lambda d: ((d[0] << 8) + d[1]) / 4, "rpm", "engine speed"),
    "0D": _t(lambda d: d[0], "km/h", "vehicle speed"),
    "0F": _t(lambda d: d[0] - 40, "degC", "intake air temperature"),
    "11": _t(lambda d: d[0] * 100 / 255, "%", "throttle position"),
    # An enumeration, not a measurement: the value is the SAE code for the OBD
    # standard the vehicle claims, so it carries no unit.
    "1C": _t(lambda d: d[0], "", "OBD standard conformance code"),
    "1F": _t(lambda d: (d[0] << 8) + d[1], "s", "run time since engine start"),
    "21": _t(lambda d: (d[0] << 8) + d[1], "km", "distance with MIL on"),
    "2F": _t(lambda d: d[0] * 100 / 255, "%", "fuel tank level input"),
    "30": _t(lambda d: d[0], "count", "warm-ups since codes cleared"),
    "31": _t(lambda d: (d[0] << 8) + d[1], "km", "distance since codes cleared"),
    "33": _t(lambda d: d[0], "kPa", "absolute barometric pressure"),
    "42": _t(lambda d: ((d[0] << 8) + d[1]) / 1000, "V", "control module voltage"),
    "46": _t(lambda d: d[0] - 40, "degC", "ambient air temperature"),
    "5B": _t(lambda d: d[0] * 100 / 255, "%", "hybrid/EV battery pack remaining life"),
    "5C": _t(lambda d: d[0] - 40, "degC", "engine oil temperature"),
    "5E": _t(lambda d: ((d[0] << 8) + d[1]) / 20, "L/h", "engine fuel rate"),
    # Four bytes at 0.1 km per bit (SAE J1979-DA).  This vehicle advertises A6
    # in its service 01 support bitmap, which makes it the one genuinely
    # useful standard-OBD reading the node was not asking for.
    "A6": _t(lambda d: ((d[0] << 24) + (d[1] << 16) + (d[2] << 8) + d[3]) / 10,
             "km", "odometer"),
}


@dataclass
class PidValue:
    pid: str
    name: str
    value: Optional[float]
    unit: str
    raw_hex: str
    status: str


def decode_pid(pid: str, reply: AdapterReply) -> PidValue:
    """Decode a service 01 reply for *pid* (two hex digits)."""
    pid = pid.upper()
    meta = PID_DECODERS.get(pid, {"fn": None, "unit": "", "name": f"PID {pid}"})
    raw_hex = " ".join(f.hex() for f in reply.frames) if reply.frames else ""
    if not reply.ok:
        return PidValue(pid, meta["name"], None, meta["unit"], raw_hex, reply.status)
    for frame in reply.frames:
        data = _payload_after(frame, 0x01, int(pid, 16))
        if data is None:
            continue
        if meta["fn"] is None:
            return PidValue(pid, meta["name"], None, meta["unit"], raw_hex, "undecoded")
        try:
            value = float(meta["fn"](data))
        except (IndexError, ZeroDivisionError):
            return PidValue(pid, meta["name"], None, meta["unit"], raw_hex, "short_frame")
        return PidValue(pid, meta["name"], value, meta["unit"], raw_hex, "ok")
    return PidValue(pid, meta["name"], None, meta["unit"], raw_hex, "unmatched")


def supported_pids(reply: AdapterReply, base_pid: str) -> list[str]:
    """Decode a support bitmap reply (PID 00/20/40/60/80/A0/C0)."""
    return _supported_pids_for_mode(reply, 0x01, base_pid)


def supported_service09_pids(reply: AdapterReply, base_pid: str = "00") -> list[str]:
    """Decode a Service 09 support bitmap (normally the ``0900`` reply)."""
    return _supported_pids_for_mode(reply, 0x09, base_pid)


def _supported_pids_for_mode(reply: AdapterReply, mode: int, base_pid: str) -> list[str]:
    base = int(base_pid, 16)
    found: list[str] = []
    for frame in reply.frames:
        data = _payload_after(frame, mode, base)
        if data is None or len(data) < 4:
            continue
        bits = int.from_bytes(data[:4], "big")
        for i in range(32):
            if bits & (1 << (31 - i)):
                found.append(f"{base + i + 1:02X}")
    return sorted(set(found))


# --- DTC decoding --------------------------------------------------------
_DTC_LETTER = {0: "P", 1: "C", 2: "B", 3: "U"}


def _dtc_from_bytes(hi: int, lo: int) -> Optional[str]:
    if hi == 0 and lo == 0:
        return None
    letter = _DTC_LETTER[(hi >> 6) & 0x03]
    return f"{letter}{(hi >> 4) & 0x03}{hi & 0x0F:X}{lo >> 4:X}{lo & 0x0F:X}"


def decode_dtcs(mode: str, reply: AdapterReply) -> list[str]:
    """Decode service 03/07/0A trouble-code replies."""
    mode_int = int(mode, 16)
    codes: list[str] = []
    for frame in reply.frames:
        data = _payload_after(frame, mode_int, None)
        if data is None:
            continue
        # CAN replies start with a count byte; ISO/KWP replies do not.  Try
        # both alignments and keep whichever yields valid codes.
        for offset in (1, 0):
            body = data[offset:]
            candidate = []
            for i in range(0, len(body) - 1, 2):
                code = _dtc_from_bytes(body[i], body[i + 1])
                if code:
                    candidate.append(code)
            if candidate:
                codes.extend(candidate)
                break
    # Preserve first-seen order while removing duplicates.
    seen: set[str] = set()
    ordered = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered


# --- service 09 ----------------------------------------------------------
def decode_vin(reply: AdapterReply) -> Optional[str]:
    """Decode a service 09 PID 02 (VIN) reply, including multi-frame form."""
    chunks: list[bytes] = []
    for frame in reply.frames:
        data = _payload_after(frame, 0x09, 0x02)
        if data is None:
            continue
        # The first data byte is the message count (usually 01); drop it when
        # present, then keep printable characters only.
        if data and data[0] in (0x01, 0x00):
            data = data[1:]
        chunks.append(data)
    if not chunks:
        return None
    joined = b"".join(chunks)
    text = "".join(chr(b) for b in joined if 0x20 <= b <= 0x7E).strip()
    text = text.replace(" ", "")
    if len(text) != 17:
        # A VIN is 17 characters.  Anything shorter is a truncated multi-frame
        # response, and reporting it would be inventing a vehicle identity.
        return None
    return text


def mask_vin(vin: Optional[str]) -> str:
    """Return a VIN safe to print in summaries, commits and dashboards."""
    if not vin:
        return "(none)"
    if len(vin) <= 6:
        return "*" * len(vin)
    return f"{vin[:3]}{'*' * (len(vin) - 5)}{vin[-2:]} (len={len(vin)})"


def decode_ascii_item(reply: AdapterReply, pid: int) -> Optional[str]:
    """Decode a printable-ASCII service 09 item such as CALID or ECU name."""
    values = decode_ascii_items(reply, pid)
    return " / ".join(values) if values else None


def decode_ascii_items(reply: AdapterReply, pid: int) -> list[str]:
    """Decode one printable Service 09 value per responding ECU."""
    values: list[str] = []
    for frame in reply.frames:
        data = _payload_after(frame, 0x09, pid)
        if data is None:
            continue
        if data and data[0] in (0x00, 0x01, 0x02, 0x03, 0x04):
            data = data[1:]
        text = "".join(chr(b) for b in data if 0x20 <= b <= 0x7E).strip()
        if text:
            values.append(text)
    return values


def decode_cvns(reply: AdapterReply) -> list[str]:
    """Decode Service 09 PID 06 calibration verification numbers.

    CVNs are four-byte binary values, not text. The byte before them is the
    message count on standard responses and is intentionally discarded.
    """
    values: list[str] = []
    for frame in reply.frames:
        data = _payload_after(frame, 0x09, 0x06)
        if data is None:
            continue
        if len(data) % 4 == 1:
            data = data[1:]
        for offset in range(0, len(data), 4):
            chunk = data[offset:offset + 4]
            if len(chunk) == 4:
                values.append(chunk.hex().upper())
    return values
