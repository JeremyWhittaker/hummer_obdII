"""Decoders for the read-only OBD-II responses this project requests.

Services 01, 02, 03, 06, 07, 09 and 0A are decoded — the same set the safety
gate allows onto the wire, and nothing else.  Decoding never mutates or
replaces the raw transcript; it is a convenience layer over bytes that were
already written to the append-only log.

Adapter responses arrive as ASCII hex text, optionally with headers, spaces,
line feeds and a trailing ``>`` prompt, and may be multi-line (ISO-TP frames
reassembled by the adapter, or one line per responding ECU).

On this vehicle a broadcast request is answered by *several* modules at once:
eight of them report their own supply voltage for ``0142``.  Decoders therefore
come in two shapes.  The singular ones (:func:`decode_pid`) answer "what did
the vehicle say", keep the first matching frame, and exist because most callers
want one number.  The plural ones (:func:`decode_pid_per_ecu`,
:func:`decode_monitor_tests`) answer "what did each module say" and keep every
frame, tagged with the address of the module that sent it.  Throwing seven of
eight readings away is a decoding choice, not a property of the bus, so the
plural form is the one to reach for when the answers are stored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

__all__ = [
    "AdapterReply",
    "split_can_header",
    "ecu_from_header",
    "parse_reply",
    "PidValue",
    "decode_pid",
    "decode_pid_per_ecu",
    "decode_freeze_frame",
    "decode_dtcs",
    "EcuDtcResult",
    "decode_dtcs_per_ecu",
    "decode_vin",
    "supported_pids",
    "supported_service09_pids",
    "supported_mids",
    "supported_freeze_frame_pids",
    "MonitorTest",
    "ReadinessBit",
    "MonitorStatus",
    "decode_monitor_status",
    "UnitAndScaling",
    "UAS_SCALINGS",
    "decode_monitor_tests",
    "decode_ascii_items",
    "decode_ascii_items_per_ecu",
    "decode_cvns",
    "decode_cvns_per_ecu",
    "parse_can_status",
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
    #: The identifier that carried each entry of ``frames``, one per frame and
    #: in the same order, or ``""`` where the adapter printed no header.
    #: ``headers`` is the historical field and is *not* aligned: it also records
    #: identifiers for frames that were dropped as incomplete, so it can be
    #: longer than ``frames``.  Attributing a reading to a module needs the
    #: aligned list, which is why it is kept separately rather than by making
    #: ``headers`` mean something new for its existing readers.
    frame_headers: list[str] = field(default_factory=list)

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
    frames, header_hex, frame_headers = _extract_frames(lines, incomplete)
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
                        negative_responses=negative_responses,
                        frame_headers=frame_headers)


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


def _extract_frames(
    lines: list[str],
    incomplete: Optional[list[bytes]] = None,
) -> tuple[list[bytes], list[str], list[str]]:
    """Turn adapter text lines into frames, reassembling ISO-TP segments.

    ELM/STN adapters print a long (multi-frame) response as a total-length
    line followed by ``0:``/``1:``/``2:`` segment lines.  Those segments are
    one logical message and must be concatenated before decoding; per-line
    decoding silently loses everything after the first segment (for example
    all but the first three VIN characters).

    Returns the frames, the historical unaligned ``headers`` list, and a third
    list holding the identifier for each returned frame in order.  The third
    list is what lets a reading be attributed to the module that sent it; it is
    built here, alongside the frames, because that is the only point where the
    pairing is still known.
    """
    headers: list[str] = []
    segments: list[str] = []
    plain: list[bytes] = []
    # Aligned with ``plain``: the 11-bit identifier stripped off each line, or
    # "" when the line carried none.  29-bit identifiers are still inside the
    # frame bytes at this point and are recovered during reassembly.
    plain_headers: list[str] = []
    for line in lines:
        m = _SEGMENT.match(line)
        if m:
            segments.append(m.group(2).replace(" ", ""))
            continue
        compact = line.replace(" ", "")
        if not re.fullmatch(r"[0-9A-Fa-f]+", compact):
            continue
        line_header = ""
        if len(compact) % 2:
            # Odd length: an 11-bit CAN header prefixes the data bytes.
            headers.append(compact[:3])
            line_header = compact[:3].upper()
            compact = compact[3:]
            if len(compact) % 2 or not compact:
                continue
        elif len(compact) <= 4 and segments == [] and len(lines) > 1:
            # A short standalone value ahead of segment lines is the ISO-TP
            # total length, not a frame.
            if any(_SEGMENT.match(other) for other in lines):
                continue
        plain.append(bytes.fromhex(compact))
        plain_headers.append(line_header)
    plain, plain_headers = _reassemble_isotp(plain, headers, incomplete, plain_headers)
    if segments:
        joined = "".join(segments)
        if len(joined) % 2:
            joined = joined[:-1]
        if joined:
            # The segment form drops the identifier before the payload is
            # printed, so the reassembled message has no module to name.
            return [bytes.fromhex(joined)] + plain, headers, [""] + plain_headers
    return plain, headers, plain_headers


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
                      incomplete: Optional[list[bytes]] = None,
                      line_headers: Optional[list[str]] = None) -> tuple[list[bytes], list[str]]:
    """Join raw ISO-TP first/consecutive CAN frames into logical messages.

    With headers enabled (``ATH1``) the adapter prints one line per CAN frame,
    including the PCI byte: ``10 14`` starts a 0x14-byte message and ``21``,
    ``22`` … carry the rest.  Decoding those lines independently would keep
    only the first few VIN characters, so they are joined here — per responding
    ECU, because several modules answer the same request and their frames
    interleave in the transcript.

    *line_headers* carries the 11-bit identifier already stripped from each
    line, so consecutive frames are matched on the identifier whichever
    addressing the vehicle uses.  Returns the messages and, aligned with them,
    the identifier each one arrived under.
    """
    out: list[bytes] = []
    out_headers: list[str] = []
    incomplete = [] if incomplete is None else incomplete
    line_headers = [""] * len(frames) if line_headers is None else line_headers
    # The identifier per input frame, whether it was stripped from the line as
    # an 11-bit header or still sits in front of the 29-bit payload.
    sources = [
        split_can_header(f)[0].hex().upper() or (line_headers[i] if i < len(line_headers) else "")
        for i, f in enumerate(frames)
    ]
    consumed: set[int] = set()
    for index, frame in enumerate(frames):
        if index in consumed or not frame:
            continue
        header, body = split_can_header(frame)
        if header:
            headers.append(header.hex().upper())
        source = sources[index]
        if len(body) >= 2 and (body[0] & 0xF0) == 0x10:
            total = ((body[0] & 0x0F) << 8) | body[1]
            payload = bytearray(body[2:])
            expected = 0x21
            for later in range(index + 1, len(frames)):
                if later in consumed or len(payload) >= total:
                    continue
                next_header, next_body = split_can_header(frames[later])
                if next_header != header or sources[later] != source or not next_body:
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
            out_headers.append(source)
            continue
        out.append(body if header else frame)
        out_headers.append(source)
    return out, out_headers


def ecu_from_header(header: str) -> str:
    """Return the responding module's address as printed in *header*.

    ISO 15765-4 extended (29-bit) response identifiers are ``18DAF1xx``: ``F1``
    is the tester and ``xx`` is the module that answered, so ``18DAF145``
    reduces to ``"45"`` — the one byte that distinguishes one responder from
    another.

    An 11-bit identifier is returned whole (``7E8``, not ``E8``).  There is no
    separate address byte to extract there: the entire identifier *is* the
    module's address on the bus, and dropping its first digit would throw away
    the part that separates the OBD response range from anything else the
    adapter might print.  The two forms therefore differ in length, which is
    honest — an ``ecu`` value means "the identifier this frame came under", not
    "a byte".
    """
    text = header.strip().upper()
    if not text:
        return ""
    if len(text) == 8 and text.startswith(("18DA", "18DB")):
        return text[-2:]
    return text


def _ecu_for_frame(reply: AdapterReply, index: int) -> str:
    """Name the module behind ``reply.frames[index]``, or "" when unknown.

    Replies assembled by hand (and those from adapters running ``ATH0``) carry
    no identifiers at all.  Those get "", never a guess: a wrong module name on
    a stored reading is worse than an absent one, because it reads as fact.
    """
    if index < len(reply.frame_headers):
        return ecu_from_header(reply.frame_headers[index])
    return ""


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
    #: Address of the module that reported this value, "" when the reply
    #: carried no identifier.  Defaults to "" so a value built by hand — or by
    #: any caller written before responders were told apart — stays valid.
    ecu: str = ""


def _pid_meta(pid: str) -> dict:
    """Look up the decoder, unit and name for *pid*, or an undecoded stand-in."""
    return PID_DECODERS.get(pid, {"fn": None, "unit": "", "name": f"PID {pid}"})


def _scale_pid_data(pid: str, meta: dict, data: bytes, raw_hex: str, ecu: str) -> PidValue:
    """Apply the PID's scaling to the data bytes of one positive response.

    Shared by the live (service 01), per-ECU and freeze-frame (service 02)
    paths so there is exactly one place where bytes become a number.  Anything
    that cannot be scaled comes back with ``value=None`` and a status naming
    the reason, never a partial number.
    """
    if meta["fn"] is None:
        return PidValue(pid, meta["name"], None, meta["unit"], raw_hex, "undecoded", ecu)
    try:
        value = float(meta["fn"](data))
    except (IndexError, ZeroDivisionError):
        # Fewer bytes than the formula needs.  The frame is in the raw log; a
        # value computed from what did arrive would be a different reading.
        return PidValue(pid, meta["name"], None, meta["unit"], raw_hex, "short_frame", ecu)
    return PidValue(pid, meta["name"], value, meta["unit"], raw_hex, "ok", ecu)


def decode_pid(pid: str, reply: AdapterReply) -> PidValue:
    """Decode a service 01 reply for *pid* (two hex digits), first answer only.

    Kept for callers that want one number.  When several modules answer — eight
    do for ``0142`` on this vehicle — this reports the first and discards the
    rest, so ``raw_hex`` deliberately still holds *every* frame: the value is
    one module's, the evidence is all of them.  Use
    :func:`decode_pid_per_ecu` to keep the other seven.
    """
    pid = pid.upper()
    meta = _pid_meta(pid)
    raw_hex = " ".join(f.hex() for f in reply.frames) if reply.frames else ""
    if not reply.ok:
        return PidValue(pid, meta["name"], None, meta["unit"], raw_hex, reply.status)
    for index, frame in enumerate(reply.frames):
        data = _payload_after(frame, 0x01, int(pid, 16))
        if data is None:
            continue
        return _scale_pid_data(pid, meta, data, raw_hex, _ecu_for_frame(reply, index))
    return PidValue(pid, meta["name"], None, meta["unit"], raw_hex, "unmatched")


def decode_pid_per_ecu(pid: str, reply: AdapterReply) -> list[PidValue]:
    """Decode one :class:`PidValue` per module that answered, in arrival order.

    A broadcast request on this vehicle is answered by every module that knows
    the PID: ``0142`` comes back eight times, each module reporting the supply
    voltage measured at its own connector.  Those are eight measurements of
    eight different things, so this returns eight values rather than picking a
    winner, each tagged with the address that sent it.

    Frames that are not answers to *pid* — another module's reply to a
    different request, a ``7F`` rejection — are skipped rather than turned into
    a value, and a module that answered with too few bytes still appears, with
    ``value=None`` and status ``short_frame``: it did respond, and the fact
    that its response was unusable is itself worth recording.

    A reply that is not ``ok`` yields an empty list, because no module
    answered.  Callers that need to record *why* should use :func:`decode_pid`,
    which reports the reply status in a single value; synthesising a
    placeholder row here would put a sample in the database with no module
    behind it.
    """
    pid = pid.upper()
    if not reply.ok:
        return []
    meta = _pid_meta(pid)
    values: list[PidValue] = []
    for index, frame in enumerate(reply.frames):
        data = _payload_after(frame, 0x01, int(pid, 16))
        if data is None:
            continue
        # Per-ECU rows carry only their own frame as evidence; the whole-reply
        # hex would repeat all eight frames against each of the eight values.
        values.append(_scale_pid_data(pid, meta, data, frame.hex(), _ecu_for_frame(reply, index)))
    return values


def decode_freeze_frame(pid: str, reply: AdapterReply, frame: int = 0) -> PidValue:
    """Decode a service 02 (freeze frame) reply for *pid* and frame number.

    A freeze frame is the snapshot an ECU stored at the moment a diagnostic
    trouble code set, and it is encoded exactly like live data with one extra
    byte: the response is ``42 <PID> <frame> <data…>``, where service 01 would
    have gone straight from the PID to the data.  Scaling is therefore reused
    from :data:`PID_DECODERS` — a freeze-framed ``0D`` is still km/h — but the
    frame byte is stripped first.  Feeding it to the decoder instead would slide
    every reading one byte along and yield numbers that look plausible.

    Only the frame the caller asked for is decoded; a module answering about a
    different stored frame is skipped rather than reported as this one.  The
    returned value is otherwise shaped exactly like a live reading, so callers
    that persist both must record which service produced it.
    """
    pid = pid.upper()
    meta = _pid_meta(pid)
    raw_hex = " ".join(f.hex() for f in reply.frames) if reply.frames else ""
    if not reply.ok:
        return PidValue(pid, meta["name"], None, meta["unit"], raw_hex, reply.status)
    for index, response in enumerate(reply.frames):
        data = _payload_after(response, 0x02, int(pid, 16))
        if data is None:
            continue
        ecu = _ecu_for_frame(reply, index)
        if not data:
            # "42 <pid>" with nothing after it: not even the frame number
            # arrived, so there is no way to tell which snapshot this is.
            return PidValue(pid, meta["name"], None, meta["unit"], raw_hex, "short_frame", ecu)
        if data[0] != frame:
            continue
        return _scale_pid_data(pid, meta, data[1:], raw_hex, ecu)
    return PidValue(pid, meta["name"], None, meta["unit"], raw_hex, "unmatched")


# --- service 01 PID 01: MIL, stored-code count and readiness monitors ----
#: The three continuous monitors, in the bit order byte B reports them.
_CONTINUOUS_MONITORS: tuple[str, ...] = ("misfire", "fuel_system", "components")

#: Non-continuous monitors by bit position, keyed by the ignition type that bit
#: 3 of byte B selects.  The two tables share bit positions and nothing else,
#: which is why the ignition type has to be read before any bit of bytes C and
#: D is named: bit 1 is the heated-catalyst monitor on a spark engine and the
#: NOx/SCR aftertreatment monitor on a compression one, so reading bit 3
#: wrongly does not fail, it mislabels every non-continuous row.
#:
#: Spark bit 4 is ``reserved_b4`` deliberately.  Older ELM-derived references
#: name it the A/C refrigerant monitor while J1979-DA lists the bit as
#: reserved, and those two cannot both be written onto a stored row.  The bit's
#: value is still reported in full; only the claim about what it monitors is
#: withheld, on the same grounds that keep an unverified UAS scaling out of
#: :data:`UAS_SCALINGS`.
_NON_CONTINUOUS_MONITORS: dict[str, tuple[str, ...]] = {
    "spark": (
        "catalyst",
        "heated_catalyst",
        "evaporative_system",
        "secondary_air_system",
        "reserved_b4",
        "oxygen_sensor",
        "oxygen_sensor_heater",
        "egr_or_vvt_system",
    ),
    "compression": (
        "nmhc_catalyst",
        "nox_scr_aftertreatment",
        "reserved_b2",
        "boost_pressure",
        "reserved_b4",
        "exhaust_gas_sensor",
        "pm_filter",
        "egr_or_vvt_system",
    ),
}


@dataclass
class ReadinessBit:
    """One monitor's support and completeness, and where both bits came from.

    ``complete`` is ``None`` whenever ``supported`` is ``False``.  The wire
    format still defines a completeness bit for a monitor the vehicle does not
    run, and that bit is normally zero — which, read as a boolean, says
    "complete" and puts a monitor that does not exist into the ready column.
    ``None`` states the only thing that is true: the question does not apply.

    ``src_byte`` and ``src_bit`` locate the *support* bit inside the four data
    bytes so a row can be checked back against ``raw_hex``.  The completeness
    bit is the paired one — byte B bit ``src_bit + 4`` for a continuous
    monitor, byte D bit ``src_bit`` for a non-continuous one — and is not given
    its own coordinates because the pairing, not the position, is the rule.
    """

    monitor: str
    #: "continuous" (byte B) or "non_continuous" (bytes C and D).
    kind: str
    supported: bool
    complete: Optional[bool]
    #: "B" for a continuous monitor, "C" for a non-continuous one.
    src_byte: str
    src_bit: int


@dataclass
class MonitorStatus:
    """What one module reported for service 01 PID 01.

    ``dtc_count`` is *this module's* count of stored emission-related codes,
    not the vehicle's: eight modules answer ``0101`` on this vehicle and each
    counts only its own.  Keeping one row per responder is what makes the
    counts addable; taking the first answer would report one module's zero as
    the whole vehicle's.

    ``mil_on`` and ``dtc_count`` are ``None`` only when the frame was too short
    to hold them, in which case ``status`` says ``short_frame`` and
    ``readiness`` is empty rather than partly decoded.
    """

    ecu: str
    mil_on: Optional[bool]
    dtc_count: Optional[int]
    #: "spark" or "compression", or "" when the frame was too short to say.
    ignition_type: str
    status: str
    raw_hex: str
    readiness: list[ReadinessBit] = field(default_factory=list)


def _readiness_bits(b: int, c: int, d: int, ignition: str) -> list[ReadinessBit]:
    """Expand bytes B, C and D into one row per monitor.

    Every monitor named by the tables gets a row, including the ones this
    vehicle does not run.  "This module runs no evaporative-system monitor" is
    a fact worth storing, and an absent row would be indistinguishable from a
    monitor the decoder forgot to look at.
    """
    bits: list[ReadinessBit] = []
    for index, name in enumerate(_CONTINUOUS_MONITORS):
        supported = bool(b & (1 << index))
        # Bits 4-6 mirror bits 0-2 and are set while the monitor has *not*
        # completed, so completeness is that bit inverted, four places up.
        not_complete = bool(b & (1 << (index + 4)))
        bits.append(ReadinessBit(name, "continuous", supported,
                                 (not not_complete) if supported else None, "B", index))
    for index, name in enumerate(_NON_CONTINUOUS_MONITORS[ignition]):
        supported = bool(c & (1 << index))
        not_complete = bool(d & (1 << index))
        bits.append(ReadinessBit(name, "non_continuous", supported,
                                 (not not_complete) if supported else None, "C", index))
    return bits


def decode_monitor_status(reply: AdapterReply) -> list[MonitorStatus]:
    """Decode service 01 PID 01 into one readiness report per responding module.

    PID 01 is deliberately absent from :data:`PID_DECODERS`: it is not a
    measurement but four bytes of packed flags, and squeezing it into the
    scalar :class:`PidValue` shape would mean picking one of them to report as
    "the value".  Callers that go through :func:`decode_pid` therefore still
    get ``status="undecoded"`` for ``01``, which is accurate, and this function
    is the one that unpacks it.

    Frames that are not positive answers to ``0101`` — another module's reply
    to a different request, a ``7F`` rejection — are skipped rather than turned
    into a row of ``False`` flags.  A module that answered with fewer than four
    data bytes still appears, with ``status="short_frame"``, no MIL state, no
    count and **no readiness bits at all**: two of the four bytes would decode
    into eleven confident-looking rows about monitors whose bytes never
    arrived, and a partial bit decode is the one failure mode here that cannot
    be spotted afterwards from the row itself.
    """
    rows: list[MonitorStatus] = []
    for index, frame in enumerate(reply.frames):
        data = _payload_after(frame, 0x01, 0x01)
        if data is None:
            continue
        ecu = _ecu_for_frame(reply, index)
        if len(data) < 4:
            rows.append(MonitorStatus(ecu, None, None, "", "short_frame", frame.hex()))
            continue
        a, b, c, d = data[0], data[1], data[2], data[3]
        # Bit 3 of byte B is not a monitor: it selects which table names the
        # bits of C and D, so it is read before any of them.
        ignition = "compression" if b & 0x08 else "spark"
        rows.append(MonitorStatus(
            ecu=ecu,
            mil_on=bool(a & 0x80),
            dtc_count=a & 0x7F,
            ignition_type=ignition,
            status="ok",
            raw_hex=frame.hex(),
            readiness=_readiness_bits(b, c, d, ignition),
        ))
    return rows


def supported_pids(reply: AdapterReply, base_pid: str) -> list[str]:
    """Decode a support bitmap reply (PID 00/20/40/60/80/A0/C0)."""
    return _supported_pids_for_mode(reply, 0x01, base_pid)


def supported_service09_pids(reply: AdapterReply, base_pid: str = "00") -> list[str]:
    """Decode a Service 09 support bitmap (normally the ``0900`` reply)."""
    return _supported_pids_for_mode(reply, 0x09, base_pid)


def supported_mids(reply: AdapterReply, base_mid: str = "00") -> list[str]:
    """Decode a service 06 supported-MID bitmap (normally the ``0600`` reply).

    Monitor IDs are advertised in exactly the same 32-bit shape as service 01
    PIDs, so the same bitmap walker serves both.
    """
    return _supported_pids_for_mode(reply, 0x06, base_mid)


def supported_freeze_frame_pids(reply: AdapterReply, base_pid: str = "00") -> list[str]:
    """Decode a service 02 support bitmap (the ``020000`` reply).

    This is the one service 02 request worth making when no trouble code is
    stored.  It asks what a freeze frame *would* contain rather than what one
    does contain, so it exercises the whole request/parse path -- and tells us
    which readings a future frame would carry -- without needing the vehicle to
    have a fault first.
    """
    return _supported_pids_for_mode(reply, 0x02, base_pid, skip=1)


def _supported_pids_for_mode(reply: AdapterReply, mode: int, base_pid: str,
                             skip: int = 0) -> list[str]:
    """Walk a 32-bit support bitmap.

    *skip* discards leading payload bytes that are not part of the bitmap.
    Service 02 needs one: its response is ``42 <PID> <frame> <bitmap…>``, so
    reading the first four bytes after the PID would take the frame number as
    the top bitmap byte and advertise a set of PIDs the vehicle never claimed.
    """
    base = int(base_pid, 16)
    found: list[str] = []
    for frame in reply.frames:
        data = _payload_after(frame, mode, base)
        if data is None:
            continue
        data = data[skip:]
        if len(data) < 4:
            continue
        bits = int.from_bytes(data[:4], "big")
        for i in range(32):
            if bits & (1 << (31 - i)):
                found.append(f"{base + i + 1:02X}")
    return sorted(set(found))


# --- service 06: on-board monitoring test results ------------------------
@dataclass(frozen=True)
class UnitAndScaling:
    """What a SAE J1979 unit-and-scaling identifier says a raw count means."""

    unit: str
    multiplier: float


#: UASID -> unit and multiplier.  **Deliberately partial.**
#:
#: Service 06 reports each test as raw 16-bit counts plus a one-byte UASID that
#: names the unit and the multiplier to reach a physical quantity.  Only the
#: identifiers whose meaning can be stated from SAE J1979 without guessing are
#: listed here; every other UASID is left out on purpose, so an unfamiliar test
#: comes back with ``scaled_value=None`` and ``unit=""`` while its raw counts
#: survive untouched.
#:
#: The reason to omit rather than approximate: a wrong multiplier does not fail,
#: it produces a well-formed number in a named unit that no one can tell from a
#: measurement.  "This module reported 27904, units unknown" is recoverable
#: later from the raw log; "this module reported 3.4 volts" when the scale was
#: really 0.001 is a fabricated reading in the database forever.
#:
#: Both entries below are unitless raw counts at a multiplier of 1, which is
#: also why they are safe to assert: they add the knowledge that the number is
#: a plain count and change nothing about its magnitude.  Adding any scaled
#: unit here means checking it against the J1979 UAS table first — the table is
#: meant to grow that way, one verified row at a time.
UAS_SCALINGS: dict[int, UnitAndScaling] = {
    0x01: UnitAndScaling("", 1.0),
    # 0x24 was here with a multiplier of 1.0 and was removed on review: the
    # entry could not be confirmed against the J1979 UAS table, and "a
    # multiplier of 1 changes no magnitude" is circular -- it only holds if 1.0
    # is the right multiplier.  An identifier this table does not know yields a
    # null scaled value with the raw counts intact, which is the honest answer.
}

#: Bytes per service 06 test record on CAN: MID, TID, UASID, then the test
#: value, minimum limit and maximum limit as 2-byte big-endian counts.
_MONITOR_RECORD_LENGTH = 9

#: MIDs reserved for support bitmaps rather than test results, mirroring the
#: service 01 bitmap PIDs.  A record claiming one of these is not a test.
_BITMAP_MIDS = frozenset({0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0})


@dataclass
class MonitorTest:
    """One on-board monitor test result, as the module reported it.

    ``value``, ``min_limit`` and ``max_limit`` are the two bytes off the wire
    read big-endian and **unsigned**, and are always populated.  J1979 defines
    some unit-and-scaling identifiers as signed; none of those is in
    :data:`UAS_SCALINGS`, so no scaled value can currently be wrong because of
    it, but a future signed row must carry its signedness rather than inherit
    this reading.  The ``scaled_*`` fields and ``unit``
    are populated only when :data:`UAS_SCALINGS` knows ``uasid``; otherwise
    they stay ``None``/``""`` and the raw counts are the whole of what is
    claimed.  ``mid``, ``tid`` and ``uasid`` are kept as integers because they
    are identifiers off the wire, not display strings.
    """

    mid: int
    tid: int
    uasid: int
    value: int
    min_limit: int
    max_limit: int
    unit: str = ""
    scaled_value: Optional[float] = None
    scaled_min: Optional[float] = None
    scaled_max: Optional[float] = None
    ecu: str = ""


def _monitor_test(record: bytes, ecu: str) -> MonitorTest:
    """Build one :class:`MonitorTest` from its nine bytes."""
    mid, tid, uasid = record[0], record[1], record[2]
    value = int.from_bytes(record[3:5], "big")
    min_limit = int.from_bytes(record[5:7], "big")
    max_limit = int.from_bytes(record[7:9], "big")
    scaling = UAS_SCALINGS.get(uasid)
    if scaling is None:
        return MonitorTest(mid, tid, uasid, value, min_limit, max_limit, ecu=ecu)
    return MonitorTest(
        mid, tid, uasid, value, min_limit, max_limit,
        unit=scaling.unit,
        scaled_value=value * scaling.multiplier,
        scaled_min=min_limit * scaling.multiplier,
        scaled_max=max_limit * scaling.multiplier,
        ecu=ecu,
    )


def decode_monitor_tests(reply: AdapterReply) -> list[MonitorTest]:
    """Decode a service 06 reply into one record per reported test.

    A module answers ``06<MID>`` with ``46`` followed by any number of
    nine-byte records, and several modules may answer at once, so this returns
    a flat list in arrival order with each test tagged by its responder.

    Two things are refused rather than decoded.  A trailing group of fewer than
    nine bytes is dropped: a record read out of a truncated tail would have a
    real-looking TID and invented limits.  And a reply whose first record
    claims a bitmap MID is a supported-MID bitmap, not test results — reading
    on past it would parse the bitmap's own bytes as a test — so that frame
    stops there and belongs to :func:`supported_mids` instead.
    """
    if not reply.ok:
        return []
    tests: list[MonitorTest] = []
    for index, frame in enumerate(reply.frames):
        data = _payload_after(frame, 0x06, None)
        if data is None:
            continue
        ecu = _ecu_for_frame(reply, index)
        for offset in range(0, len(data) - _MONITOR_RECORD_LENGTH + 1, _MONITOR_RECORD_LENGTH):
            record = data[offset:offset + _MONITOR_RECORD_LENGTH]
            if record[0] in _BITMAP_MIDS:
                break
            tests.append(_monitor_test(record, ecu))
    return tests


# --- DTC decoding --------------------------------------------------------
_DTC_LETTER = {0: "P", 1: "C", 2: "B", 3: "U"}


def _dtc_from_bytes(hi: int, lo: int) -> Optional[str]:
    if hi == 0 and lo == 0:
        return None
    letter = _DTC_LETTER[(hi >> 6) & 0x03]
    return f"{letter}{(hi >> 4) & 0x03}{hi & 0x0F:X}{lo >> 4:X}{lo & 0x0F:X}"


def _dtcs_from_payload(data: bytes) -> list[str]:
    """Read the trouble codes out of one service 03/07/0A payload.

    CAN replies start with a count byte; ISO/KWP replies do not.  Both
    alignments are tried and whichever yields valid codes wins.

    That heuristic is why this is a function rather than a loop body: it
    cannot tell "no codes" from "wrong alignment", so its empty result means
    only "no codes were read here" and must never be used to decide whether a
    module answered.  :func:`decode_dtcs_per_ecu` makes that decision from the
    frame instead, which is the whole difference between the two decoders.
    """
    for offset in (1, 0):
        body = data[offset:]
        candidate = []
        for i in range(0, len(body) - 1, 2):
            code = _dtc_from_bytes(body[i], body[i + 1])
            if code:
                candidate.append(code)
        if candidate:
            return candidate
    return []


def decode_dtcs(mode: str, reply: AdapterReply) -> list[str]:
    """Decode service 03/07/0A trouble-code replies, de-duplicated.

    Kept for callers that want the vehicle's fault list.  It cannot express
    "every module answered and none holds a code", which is what this vehicle
    says: that reply and a reply nobody sent both come back as ``[]``.  Use
    :func:`decode_dtcs_per_ecu` where the answer is stored as evidence.
    """
    mode_int = int(mode, 16)
    codes: list[str] = []
    for frame in reply.frames:
        data = _payload_after(frame, mode_int, None)
        if data is None:
            continue
        codes.extend(_dtcs_from_payload(data))
    # Preserve first-seen order while removing duplicates.
    seen: set[str] = set()
    ordered = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered


@dataclass
class EcuDtcResult:
    """What one module said when it was asked for trouble codes.

    ``codes`` empty with ``status="ok"`` is a positive observation: this module
    answered, and it holds no codes.  On this vehicle that is every row, which
    makes it the row that matters most — "the brake controller answered and has
    nothing" and "the brake controller was silent" are different facts, and
    only the first is evidence that the request reached a live module.  A list
    of rows is therefore not a list of faults; the count of rows is a count of
    responders.

    ``detail`` carries the reason behind a non-``ok`` status — the standard
    name of a negative-response code, for instance — and stays empty otherwise.
    """

    ecu: str
    #: The service asked for, as two hex digits ("03", "07", "0A").
    mode: str
    codes: list[str]
    status: str
    raw_hex: str
    detail: str = ""


def _negative_response_for(frame: bytes, mode: int) -> Optional[int]:
    """Return the NRC when *frame* is a ``7F`` rejection of *mode*, else ``None``.

    Mirrors the two offsets :func:`_find_negative_responses` handles: an
    auto-formatted reply begins at ``7F``, while a header-preserving one keeps
    its ISO-TP length byte in front of it.  A rejection naming a *different*
    service belongs to some other request and is not this module's answer to
    this one, so it is not matched here.
    """
    for offset in (0, 1):
        if len(frame) >= offset + 3 and frame[offset] == 0x7F and frame[offset + 1] == mode:
            return frame[offset + 2]
    return None


def decode_dtcs_per_ecu(mode: str, reply: AdapterReply) -> list[EcuDtcResult]:
    """Decode one row per module that answered a service 03/07/0A request.

    :func:`decode_dtcs` answers "what is wrong with the vehicle" and returns a
    flat, de-duplicated list of codes.  This answers "what did each module
    say", which on a vehicle with no faults is the more useful question: eight
    modules reply ``43 00`` to a broadcast ``03``, and eight rows reporting no
    codes are eight pieces of evidence that the bus is alive.  The flat list
    for the same reply is empty and indistinguishable from silence.

    That is the trap this function is built around.  :func:`decode_dtcs` keeps
    whichever byte alignment *yields codes*, so a module reporting zero codes
    yields nothing at either alignment; a naive per-ECU port of that loop would
    drop exactly the rows this vehicle produces.  Here the code list is a
    property of a row, never its admission ticket: **any frame whose payload
    answers the requested mode is a row**, empty codes and all.

    Three statuses are possible.  ``ok`` means the module answered, with
    ``codes`` holding whatever it reported and an empty list meaning it
    reported none.  ``negative_response`` means it refused, with ``detail``
    naming the code.  ``short_frame`` means the response byte arrived with
    nothing behind it, not even the count: that is a truncated answer, and
    reporting it as a module that holds no codes would invent the one fact the
    frame failed to carry.

    Unlike :func:`decode_dtcs`, codes are **not** de-duplicated across modules.
    Two modules reporting ``P0143`` is two modules reporting ``P0143``.
    """
    mode_int = int(mode, 16)
    label = f"{mode_int:02X}"
    rows: list[EcuDtcResult] = []
    # No ``reply.ok`` gate: a reply in which every module refused is parsed
    # with status "negative_response", and those refusals are precisely the
    # rows this function exists to keep.  Replies that carry no frames at all
    # (no_data, error, text, incomplete) yield no rows on their own.
    for index, frame in enumerate(reply.frames):
        ecu = _ecu_for_frame(reply, index)
        nrc = _negative_response_for(frame, mode_int)
        if nrc is not None:
            rows.append(EcuDtcResult(ecu, label, [], "negative_response",
                                     frame.hex(), negative_response_name(nrc)))
            continue
        data = _payload_after(frame, mode_int, None)
        if data is None:
            continue
        if not data:
            rows.append(EcuDtcResult(ecu, label, [], "short_frame", frame.hex(),
                                     "positive response with no data bytes"))
            continue
        rows.append(EcuDtcResult(ecu, label, _dtcs_from_payload(data), "ok", frame.hex()))
    return rows


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
    """Decode one printable Service 09 value per responding ECU.

    The untagged view of :func:`decode_ascii_items_per_ecu`, kept because most
    callers want the strings.  Values stay in arrival order, so a caller that
    needs to know which module said what can switch decoders without its
    indices moving.
    """
    return [value for _ecu, value in decode_ascii_items_per_ecu(reply, pid)]


def decode_cvns(reply: AdapterReply) -> list[str]:
    """Decode Service 09 PID 06 calibration verification numbers.

    CVNs are four-byte binary values, not text. The byte before them is the
    message count on standard responses and is intentionally discarded.

    The untagged view of :func:`decode_cvns_per_ecu`, in arrival order.
    """
    return [value for _ecu, value in decode_cvns_per_ecu(reply)]


def decode_ascii_items_per_ecu(reply: AdapterReply, pid: int) -> list[tuple[str, str]]:
    """Decode printable Service 09 values as ``(ecu, value)`` pairs.

    The values are exactly the ones :func:`decode_ascii_items` returns, tagged
    with the address of the module that sent each.

    **The tag is often empty, and that is the honest answer.**  Service 09
    items are long enough to arrive as ISO-TP multi-frame messages, and when
    the adapter prints one in its ``0:``/``1:`` segment form the identifier is
    gone before the payload is: :func:`_extract_frames` has nothing left to
    attribute the reassembled message to and returns ``""`` for it.  Raw
    frame-per-line replies (``ATH1``, which is what this project runs) keep the
    identifier and do attribute correctly, so both shapes appear in practice.
    An empty address means "this transcript does not say"; filling it in from
    the surrounding frames would be a guess, and a guessed module on a stored
    calibration ID reads exactly like a measured one.
    """
    values: list[tuple[str, str]] = []
    for index, frame in enumerate(reply.frames):
        data = _payload_after(frame, 0x09, pid)
        if data is None:
            continue
        if data and data[0] in (0x00, 0x01, 0x02, 0x03, 0x04):
            data = data[1:]
        text = "".join(chr(b) for b in data if 0x20 <= b <= 0x7E).strip()
        if text:
            values.append((_ecu_for_frame(reply, index), text))
    return values


def decode_cvns_per_ecu(reply: AdapterReply) -> list[tuple[str, str]]:
    """Decode Service 09 PID 06 calibration verification numbers per module.

    One module may report several CVNs in one answer — one per calibration it
    holds — so the same address can appear on several rows; that repetition is
    the module's, not this decoder's.

    The same caveat as :func:`decode_ascii_items_per_ecu` applies, and applies
    harder: a CVN reply is four bytes per number plus a count byte, so it is
    multi-frame as soon as a module reports more than one, and a
    segment-reassembled reply comes back with ``ecu=""``.
    """
    values: list[tuple[str, str]] = []
    for index, frame in enumerate(reply.frames):
        data = _payload_after(frame, 0x09, 0x06)
        if data is None:
            continue
        if len(data) % 4 == 1:
            data = data[1:]
        ecu = _ecu_for_frame(reply, index)
        for offset in range(0, len(data), 4):
            chunk = data[offset:offset + 4]
            if len(chunk) == 4:
                values.append((ecu, chunk.hex().upper()))
    return values


# --- adapter status ------------------------------------------------------
#: ``T:xx R:xx`` inside an ``ATCS`` reply.  Each counter is printed as a
#: two-character field, so a wider one is not this reply and is refused rather
#: than reinterpreted.  The negative lookaheads are what enforce that on *both*
#: counters: without them the trailing ``R:`` field of ``T:00 R:002`` still
#: matches its first two digits and a reply nobody parsed is reported as a
#: plausible pair of error counts.
_CAN_STATUS = re.compile(
    r"T:\s*([0-9A-F]{1,2})(?![0-9A-F])\s*R:\s*([0-9A-F]{1,2})(?![0-9A-F])")


def parse_can_status(text: str) -> tuple[Optional[int], Optional[int]]:
    """Parse an ``ATCS`` reply into ``(transmit errors, receive errors)``.

    ``ATCS`` reads the adapter's own CAN error counters without transmitting,
    which is how :mod:`hummer_obd.voltage` tells a sleeping bus apart from a
    broken one during an overnight watch.  The live adapter answers exactly
    ``T:00 R:00``.

    **The radix is unresolved and the parse is hex.**  Every value this project
    has ever observed is ``00``, where hexadecimal and decimal agree, so
    nothing in the evidence settles which the adapter prints — and a counter
    read in the wrong radix does not fail, it reports 16 errors as 22 or 22 as
    16.  What survives the ambiguity is the only distinction the counters are
    used for here: zero and non-zero are the same in either radix for the
    two-character fields this parses.  **Callers must compare against zero and
    must not treat the magnitude of a non-zero counter as a count of errors**
    until the radix is confirmed against a bus that is actually producing them.

    Anything that is not a counter pair — an ``OK``, an error marker, a reply
    carrying only one of the two counters — yields ``(None, None)``.  Half a
    status is not a status: the pair is the observation.
    """
    if not text:
        return (None, None)
    match = _CAN_STATUS.search(text.upper())
    if match is None:
        return (None, None)
    return (int(match.group(1), 16), int(match.group(2), 16))
