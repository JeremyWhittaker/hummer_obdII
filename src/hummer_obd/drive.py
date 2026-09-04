"""Bounded drive/charge session recorder.

Everything proven so far was fired by hand, one identifier at a time, with a
person reading hex off a terminal.  That is the right way to *validate* an
identifier and a useless way to *use* one: a drive happened on 2026-09-02 and
recorded nothing, because nothing was armed to catch it.

This module closes that gap.  It samples the enumerated enhanced identifiers
and a few standard PIDs on a timer, decodes them with the equations this
project has verified, and writes both a decoded CSV and the byte-exact
transcript.

What it deliberately is not
---------------------------
It is **not** the unattended collector, and it must never become one.
``collector.py`` goes through :func:`safety.validate_command`, which refuses
service ``22`` outright, and that stays true.  This recorder is operator-
started, transmits only with ``--confirm``, and stops on its own at a duration
or cycle limit.  Enhanced reads remain a thing a person decides to do.

Addressing groups
-----------------
The identifiers live behind three different module addresses, and switching
address costs three commands rather than a full re-initialisation.  The adapter
is set up once and then re-pointed per group, which is what makes a useful
sample rate possible at all -- a full ``ATZ`` sequence per group would dominate
the cycle.

Decoding
--------
Every equation here was verified before it was written down:

* the battery group against the vehicle's own cross-checks (range over state of
  charge landing on the published EPA figure, a charger reading of zero while
  the pack discharges),
* the chassis group against OBDb test vectors, which pair a captured frame with
  its expected value, so each formula was derived from data and re-checked
  against every vector.

``0x2B43`` is deliberately *not* decoded.  This vehicle returns a 26-byte array
where the source describes a single byte, so the column holds raw hex and the
meaning stays an open question rather than a guess.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Final, Optional

from .decode import decode_monitor_status, decode_pid, parse_reply
from .rawlog import RawLog
from .safety import (
    validate_command,
    validate_enhanced_command,
    validate_supervised_command,
)
from .transport import SerialTransport, Transport, TransportError

__all__ = ["AddressGroup", "GROUPS", "DECODERS", "COLUMNS",
           "STANDARD_ADDRESS", "POWER_WINDOW_S", "record", "main"]


def _u16(p: bytes, i: int) -> int:
    return (p[i] << 8) | p[i + 1]


def _s16(p: bytes, i: int) -> int:
    v = _u16(p, i)
    return v - 0x10000 if v & 0x8000 else v


def _u24(p: bytes, i: int) -> int:
    return (p[i] << 16) | (p[i + 1] << 8) | p[i + 2]


#: ``did -> payload -> {column: value}``.  A decoder returns ``{}`` rather than
#: raising when the payload is shorter than it expects: a truncated reply is a
#: missing sample, not a reason to end a session that is recording a drive.
def _cell_summary(p: bytes) -> dict:
    """Cell voltage summary from ``0x2AF5``, keeping the bytes it does not decode.

    This identifier answers with **ten** bytes on this vehicle, measured over
    1315 replies, and only the first six were ever read.  The other four were
    dropped on the floor every sample: they never reached the CSV, so no later
    analysis could recover them and nobody could tell they existed.

    They are kept raw rather than decoded, because what they are is not yet
    known.  What is known, from those 1315 replies, is that they do not look
    like measurements: byte 9 is *constant* at 23, and byte 7 takes only seven
    values, all between 13 and 24.  Small bounded integers next to a
    minimum and a maximum look like indices -- which cell is weakest, which is
    strongest -- and if that is what they are, this vehicle can name the cell
    rather than only its voltage.  That would be a real jump in granularity, so
    the bytes are preserved until something can confirm or refute it.  A guess
    is not worth writing into a column name; the bytes are worth keeping.
    """
    if len(p) < 6:
        return {}
    row = {
        "cell_avg_v": round(_u16(p, 0) / 10000, 4),
        "cell_min_v": round(_u16(p, 2) / 10000, 4),
        "cell_max_v": round(_u16(p, 4) / 10000, 4),
        # Spread in millivolts, computed from the raw counts so it does not
        # inherit rounding from the three volt columns above.
        "cell_spread_mv": round((_u16(p, 4) - _u16(p, 2)) / 10, 2),
    }
    if len(p) > 6:
        row["cell_extra_raw"] = p[6:].hex().upper()
    return row


DECODERS: dict[str, Callable[[bytes], dict]] = {
    "27C6": lambda p: {"soc_pct": round(_u16(p, 0) / 655.35, 3)} if len(p) >= 2 else {},
    "27AF": lambda p: {"energy_kwh": round(_u16(p, 0) / 100, 2)} if len(p) >= 2 else {},
    "27C7": lambda p: {"range_mi": round(_u24(p, 0) / 103, 2)} if len(p) >= 3 else {},
    "27C0": lambda p: {"dist_since_chg_mi": round(_u24(p, 0) / 16.09344, 2)} if len(p) >= 3 else {},
    "0046": lambda p: {"temp_f": round((p[0] - 40) * 1.8 + 32, 1)} if p else {},
    # This vehicle answers 0x5401 with a SINGLE byte, so the published two-byte
    # "/4350 kW" equation cannot apply and is not used.  A charging session has
    # now given it the reference this comment used to be waiting for, and the
    # answer is that it is not power at all:
    #
    #   * It is tied to charging.  An earlier note here claimed a -0.81
    #     correlation with pack current over 297 paired samples; that figure
    #     did not survive a larger corpus.  Over 1907 paired rows the
    #     correlation is -0.09.  The original was computed when the recorded
    #     data was almost entirely parked and charging, with pack current
    #     spanning -22 to +105 A; once real driving was recorded that span
    #     reached +836 A while this byte stayed at zero throughout.  It was
    #     measuring the composition of the corpus, not the signal.
    #   * But while charging it sits at 147-152 across a measured 1.85 to
    #     16.51 kW -- a ninefold power range holding one plateau.  Power it is
    #     not.
    #   * It is zero in 227 of 254 samples taken while not charging.
    #   * And after a charge ended -- state of charge flat at 89.653, energy
    #     flat at 172.03, current at zero -- it decayed monotonically to zero
    #     over three and a half minutes: 36, 33, 30, 26, 23, 20, 16, 13, 6, 0.
    #
    # A plateau while working, then a slow ramp to zero after the work stops,
    # is what a demand or duty signal looks like -- a pump or fan winding down,
    # or a thermal-management output.  It is not battery temperature either:
    # that correlation is -0.25.  Still kept raw, because "tied to charging and
    # shaped like a duty cycle" is not a unit, and naming it would be inventing
    # one.  Recorded in docs/PACK_ARCHITECTURE.md.
    "5401": lambda p: {"charger_5401_raw": p.hex().upper()} if p else {},
    "2AF5": _cell_summary,
    "33E5": lambda p: {"dmc2_v": round(p[0] / 10, 1)} if p else {},
    "4A7A": lambda p: (
        {
            "wheel_fl_kph": p[0],
            "wheel_fr_kph": p[1],
            "wheel_rl_kph": p[2],
            "wheel_rr_kph": p[3],
        }
        if len(p) >= 4
        else {}
    ),
    "4A7C": lambda p: {"brake_kpa": (p[0] - 10) * 100} if p else {},
    "4C2D": lambda p: {"steering_deg": round(_s16(p, 0) * 0.022, 2)} if len(p) >= 2 else {},
    "4C2F": lambda p: {"lateral_g": round(_s16(p, 0) * 0.0015928, 5)} if len(p) >= 2 else {},
    "4C30": lambda p: {"longitudinal_g": round(_s16(p, 0) * 0.0015928, 5)} if len(p) >= 2 else {},
    # Not decoded on purpose: 26 bytes where the source describes one.
    "2B43": lambda p: {"array_2b43": p.hex().upper()},
    # Twenty-four values, proven to answer on this vehicle on 2026-09-03.  The
    # source calls it battery module temperature, and twenty-four is exactly the
    # module count three independent structural results agree on.  Kept RAW all
    # the same: under (x-40)/2 the probe's values landed at 37.0-37.5 C against
    # a pack temperature of 39.0 C measured in the same minute -- close enough
    # to be tempting, nowhere near enough to be true.  That was one sample at
    # one temperature, and a scaling that lands near the right answer at 39 C
    # says nothing about 5 C or 55 C.  Capturing it every cycle is how the
    # temperature range needed to settle it gets collected.
    "2AF1": lambda p: {"array_2af1": p.hex().upper()},
    # Proven to answer at CB on 2026-09-03 and then not captured, which made
    # them impossible to decode: the only way to learn what a field means is to
    # watch it across states it has never been seen in, and a value probed once
    # while parked has been seen in exactly one.  Raw, because a single sample
    # is not a scaling -- 27BF read 33, 27BB read 100, 27B5 read 21 and 2709
    # read 110, all on a warm parked truck that had just been driven.
    "27BF": lambda p: {"regen_field_raw": p.hex().upper()} if p else {},
    "27BB": lambda p: {"thermal_energy_raw": p.hex().upper()} if p else {},
    "27B5": lambda p: {"thermal_distance_raw": p.hex().upper()} if p else {},
    "2709": lambda p: {"compressor_temp_raw": p.hex().upper()} if p else {},
    # Module 40, reachable only at priority 0x18 and unreachable for a day
    # because this recorder sent one priority to every module.  All nine LYRIQ
    # candidates answer there.  Every one is kept RAW: 416C read 2589 then 2593
    # a minute apart, 416D and 416E returned identical values, and the vehicle
    # was parked and unplugged, which is the state that says least about an
    # EVSE current.  Nine payloads and nine open questions -- capturing them
    # across charging, driving and a cold morning is what will settle them.
    "4149": lambda p: {"evse_current_raw": p.hex().upper()} if p else {},
    "416C": lambda p: {"group_v1_raw": p.hex().upper()} if p else {},
    "416D": lambda p: {"group_v2_raw": p.hex().upper()} if p else {},
    "416E": lambda p: {"group_v3_raw": p.hex().upper()} if p else {},
    "434F": lambda p: {"hv_temp_raw": p.hex().upper()} if p else {},
    "4127": lambda p: {"batt_temp_a_raw": p.hex().upper()} if p else {},
    "4124": lambda p: {"batt_temp_b_raw": p.hex().upper()} if p else {},
    "40E5": lambda p: {"coolant_1_raw": p.hex().upper()} if p else {},
    "40E6": lambda p: {"coolant_2_raw": p.hex().upper()} if p else {},
    # Traction pack voltage and current.  Both come from unmerged BEV3 sources,
    # and both were confirmed on this vehicle during an AC charge: 388.60 V and
    # -20.95 A, whose product (8.14 kW) agreed within 6% with the charge power
    # derived independently from the energy field's slope.  Negative current is
    # charging, which is the sign convention the source states and the vehicle
    # confirmed while plugged in.
    "2885": lambda p: {"pack_v": round(_u16(p, 0) / 100, 2)} if len(p) >= 2 else {},
    "2414": lambda p: {"pack_a": round(_s16(p, 0) / 20, 2)} if len(p) >= 2 else {},
    # Nominal pack voltage -- the rated figure, not a measurement, so a value
    # that never moves is the expected result rather than a dead field.
    #
    # Answered 0x5806 = 22534 on 2026-09-04, the first time it was ever sent:
    # allowlisted a day earlier and reachable from no profile at all until the
    # confidence table found it.  The source's /64 gives 352.09 V, which is
    # 3.6676 V across the 96 cells this pack was independently shown to have in
    # series -- the textbook nominal for an NMC cell.  That is a structural
    # corroboration of the divisor and not yet a proof of it: what makes this a
    # nominal rather than a coincidence is holding still while the pack does
    # not, and one reading cannot show that.  Which is why it is captured.
    "2429": lambda p: {"nominal_pack_v": round(_u16(p, 0) / 64, 2)} if len(p) >= 2 else {},
}


@dataclass(frozen=True)
class AddressGroup:
    """A module, and the identifiers to ask it for."""

    name: str
    ecu: str
    #: Commands that re-point the adapter at this module.  Flow control is
    #: re-asserted *after* the header every time: setting ATFCSM before
    #: ATFCSH leaves the adapter without a flow-control header to answer a
    #: first frame with, and a multi-frame reply then arrives truncated --
    #: which is exactly how the cell-voltage read silently lost its second
    #: frame on the first live run of this recorder.
    address: tuple[str, ...]
    dids: tuple[str, ...]
    #: The CAN priority this module answers service 22 at.  There is no
    #: universal one, which was established by asking every module at both:
    #:
    #:   17, 1D, 1E, CB   answer at 0x14 and 0x18
    #:   28 BSCM          answers at 0x14; at 0x18 it returns 7F 22 11,
    #:                    serviceNotSupported -- not "no such identifier" but
    #:                    "not this service at this priority"
    #:   40 BCM           answers at 0x18 only; at 0x14 it returns nothing at
    #:                    all, which is what made it look unreachable for a day
    #:
    #: So 28 and 40 are mutually exclusive under a single global priority, and
    #: the recorder used one for every group.  That is why module 40 could not
    #: be added until this field existed.
    priority: str = "ATCP14"


GROUPS: tuple[AddressGroup, ...] = (
    AddressGroup(
        name="battery",
        ecu="CB",
        address=("ATSHDACBF1", "ATCRA142AF1CB", "ATFCSH14DACBF1",
                 "ATFCSD300000", "ATFCSM1"),
        dids=("27C6", "27AF", "27C7", "27C0", "0046", "5401", "2AF5", "2B43",
              "2AF1", "27BF", "27BB", "27B5", "2709"),
    ),
    AddressGroup(
        name="chassis",
        ecu="28",
        address=("ATSHDA28F1", "ATCRA142AF128", "ATFCSH14DA28F1",
                 "ATFCSD300000", "ATFCSM1"),
        dids=("4A7A", "4A7C", "4C2D", "4C2F", "4C30"),
    ),
    AddressGroup(
        name="pack_power",
        ecu="17",
        address=("ATSHDA17F1", "ATCRA142AF117", "ATFCSH14DA17F1",
                 "ATFCSD300000", "ATFCSM1"),
        dids=("2885", "2414", "2429"),
    ),
    AddressGroup(
        name="drive_motor",
        ecu="1D",
        address=("ATSHDA1DF1", "ATCRA142AF11D", "ATFCSH14DA1DF1",
                 "ATFCSD300000", "ATFCSM1"),
        dids=("33E5",),
    ),
    AddressGroup(
        name="body",
        ecu="40",
        address=("ATSHDA40F1", "ATCRA18DAF140", "ATFCSH18DA40F1",
                 "ATFCSD300000", "ATFCSM1"),
        dids=("4149", "416C", "416D", "416E", "434F", "4127", "4124",
              "40E5", "40E6"),
        priority="ATCP18",
    ),
)

#: Standard OBD PIDs sampled alongside.  These go through the *ordinary* gate.
#: Asking for them needs the receive filter opened up again, because the
#: enhanced groups leave it pointed at one module.
#: Legislated service 01 PIDs, and the column each lands in.
#:
#: These are not sourced from anywhere external and are not candidates: the
#: vehicle's own support bitmap says it answers them.  A per-module census on
#: 2026-09-03 had module 17 advertise nine -- 01 0D 1C 1F 21 30 31 42 A6 -- and
#: this recorder was collecting two of them.  The rest were legislated, already
#: decodable, and going uncollected.
#:
#: `0D` and `A6` keep their hand-written decoders below because they predate
#: this list.  Everything else goes through `decode.decode_pid`, which already
#: carries the scaling and unit for each (decode.py:446-453) -- adding six more
#: hand-rolled branches would be re-deriving what the module already knows.
#:
#: `01` is deliberately absent: it is a monitor-status bitfield rather than a
#: scalar, so it has no single column to land in.
STANDARD_PIDS: tuple[tuple[str, str], ...] = (
    ("010D", "speed_kph"),
    ("01A6", "odometer_km"),
    ("011C", "obd_standard"),
    ("011F", "run_time_s"),
    ("0121", "dist_with_mil_km"),
    ("0130", "warmups_since_clear"),
    ("0131", "dist_since_clear_km"),
    ("0142", "module_voltage"),
)

#: Service 01 PID 01, the ninth thing module 17 advertises and the one the
#: census-driven list above could not absorb.
#:
#: It is not a measurement.  It is four bytes of packed flags -- malfunction
#: lamp, stored-fault count, and a readiness bit per emissions monitor -- so
#: there is no single value to put in a column and ``decode_pid`` correctly
#: reports it as undecoded.  ``decode.decode_monitor_status`` unpacks it
#: properly, and two of its fields belong in a drive row: a malfunction lamp
#: coming on *during* a drive, with the distance and speed either side of it,
#: is the sort of thing a recorder exists to catch and a later scan cannot
#: reconstruct.
#:
#: The readiness bits are deliberately not columns.  Eleven boolean columns
#: that change across months, recorded every eight seconds, is the wrong shape
#: for this file; ``hummer-obd-probe`` reports them when asked.
MONITOR_STATUS_PID: str = "0101"
MONITOR_STATUS_COLUMNS: tuple[str, ...] = ("mil_on", "dtc_count")

#: One-time setup.  Protocol, priority and flow control do not change between
#: groups, so they are paid for once per session rather than once per sample.
SESSION_INIT: tuple[str, ...] = (
    "ATZ", "ATE0", "ATL0", "ATS0", "ATH1", "ATAL",
    "ATSP7", "ATCP14", "ATFCSD300000", "ATFCSM1", "ATST96",
)

#: Standard OBD runs at priority 0x18, not the 0x14 the enhanced groups use.
#: Without restoring it, ``010D`` goes out addressed to whichever module the
#: last enhanced group left selected, and that module answers NO DATA -- which
#: is how the first live run recorded no speed at all despite the vehicle being
#: awake.
#:
#: These are addressed to module 0x17 specifically rather than broadcast to
#: ``DB33F1``, because on this vehicle the broadcast does not work.  Measured
#: across a whole raw transcript:
#:
#:     010D  545 answered, every one from module 17, plus 765 negative
#:           responses from module 28 (``7F 01 22``, conditionsNotCorrect)
#:     01A6  545 answered, every one from module 17, plus 766 the same from
#:           module 28 and 2 busy replies from module 45
#:
#: A functional broadcast is answered by whoever speaks first, and module 28
#: refuses service 01 faster than module 17 can answer it.  The adapter returns
#: that refusal and the real answer never arrives, so on 2026-09-03 speed and
#: odometer were present in 8 of 79 rows while every enhanced read was present
#: in all 79.  Nothing was wrong with the vehicle and nothing was wrong with
#: the decode: the question was being shouted at a room where the wrong module
#: answers first.
#:
#: ``ATCRA18DAF117`` then keeps the receive filter on module 17's own reply
#: address, so a module that was not asked cannot be mistaken for one that was.
#: Module 17 is already the pack_power group's module, so this address is not a
#: guess -- it is the one this node already reads pack voltage and current from.
STANDARD_ADDRESS: tuple[str, ...] = (
    "ATCP18",
    "ATSHDA17F1",
    "ATCRA18DAF117",
)

#: Puts the priority byte back for the next cycle's enhanced groups.
ENHANCED_PRIORITY = "ATCP14"

COLUMNS: tuple[str, ...] = (
    "utc", "elapsed_s", "volts",
    "speed_kph", "odometer_km",
    "soc_pct", "energy_kwh", "range_mi", "dist_since_chg_mi",
    "temp_f", "charger_5401_raw", "power_kw",
    "cell_avg_v", "cell_min_v", "cell_max_v", "cell_spread_mv", "cell_extra_raw",
    "array_2af1",
    "obd_standard", "run_time_s", "dist_with_mil_km",
    "warmups_since_clear", "dist_since_clear_km", "module_voltage",
    "regen_field_raw", "thermal_energy_raw", "thermal_distance_raw",
    "compressor_temp_raw",
    "evse_current_raw", "group_v1_raw", "group_v2_raw", "group_v3_raw",
    "hv_temp_raw", "batt_temp_a_raw", "batt_temp_b_raw",
    "coolant_1_raw", "coolant_2_raw",
    "pack_v", "pack_a", "nominal_pack_v", "hv_power_kw",
    "dmc2_v",
    "wheel_fl_kph", "wheel_fr_kph", "wheel_rl_kph", "wheel_rr_kph",
    "brake_kpa", "steering_deg", "lateral_g", "longitudinal_g",
    "array_2b43",
    "mil_on", "dtc_count",
)


#: Seconds of history the power slope is taken over.  Long enough that the
#: 0.01 kWh quantum of the energy field is small against it, short enough to
#: follow a real change in charge rate.
POWER_WINDOW_S: float = 60.0


#: Consecutive cycles that decode nothing before the recorder gives up and
#: exits.  Exiting is the recovery path rather than a failure of it: the unit
#: is ``Restart=always`` and ``hummer-rfcomm`` binds the device on open, so the
#: next process gets a freshly established link and a properly initialised
#: adapter.  Staying alive on a dead file descriptor is what loses a drive.
DEAD_CYCLES_BEFORE_EXIT: int = 3

#: Columns a row can carry without anything having been heard from the vehicle.
#: ``utc`` and ``elapsed_s`` are the row's own bookkeeping, and ``volts`` comes
#: from ``ATRV``, which the adapter answers by itself.  A cycle holding only
#: these has decoded nothing, however healthy the process looks.
_NON_VEHICLE_COLUMNS: Final[frozenset[str]] = frozenset({"utc", "elapsed_s", "volts"})


def _revive(transport: Transport, *, timeout: float, attempt: int) -> None:
    """Reopen the link and re-initialise the adapter, or raise.

    Reopening the RFCOMM device re-establishes the Bluetooth link, and that
    returns the ELM to its power-on defaults.  So the session header has to be
    sent again: reconnecting without it leaves an adapter that answers with
    echo on and no protocol selected, which reads as corrupt data rather than
    as a dead link -- worse than the failure being recovered from.

    ``reconnect`` lives on :class:`SerialTransport` rather than on the
    :class:`Transport` interface, so a transport that cannot reconnect says so
    instead of raising ``AttributeError`` from inside a recovery path.
    """
    reconnect = getattr(transport, "reconnect", None)
    if reconnect is None:
        raise TransportError("this transport cannot reconnect")
    reconnect(attempt)
    for command in SESSION_INIT:
        transport.send(validate_command(command), timeout=timeout)


def _power_over_window(rows: list, row: dict) -> Optional[float]:
    """Slope of ``energy_kwh`` from the oldest sample still inside the window.

    Returns ``None`` until there is enough history, which is honest: a power
    figure derived from one point does not exist, and emitting a placeholder
    zero would read as "not charging".
    """
    if "energy_kwh" not in row or "elapsed_s" not in row:
        return None
    cutoff = row["elapsed_s"] - POWER_WINDOW_S
    oldest = None
    for candidate in rows:
        if "energy_kwh" not in candidate or "elapsed_s" not in candidate:
            continue
        if candidate["elapsed_s"] >= cutoff:
            oldest = candidate
            break
    if oldest is None:
        # Nothing inside the window yet; fall back to the earliest sample so a
        # short session still reports something, once it has two points.
        for candidate in rows:
            if "energy_kwh" in candidate and "elapsed_s" in candidate:
                oldest = candidate
                break
    if oldest is None:
        return None
    hours = (row["elapsed_s"] - oldest["elapsed_s"]) / 3600.0
    if hours <= 0:
        return None
    return round((row["energy_kwh"] - oldest["energy_kwh"]) / hours, 2)


@dataclass
class Session:
    cycles: int = 0
    rows: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _payload(reply, request: str) -> Optional[bytes]:
    """The bytes after the echoed ``62 <did>``, or ``None`` if absent.

    Requiring the echo is what stops an unrelated frame that survived the
    receive filter from being recorded as a reading.
    """
    expect = bytes([0x62]) + bytes.fromhex(request[2:])
    for frame in reply.frames:
        idx = frame.find(expect)
        if idx != -1:
            return frame[idx + len(expect):]
    return None


def record(
    transport: Transport,
    *,
    interval_s: float = 5.0,
    duration_s: float = 0.0,
    max_cycles: int = 0,
    say: Callable[[str], None] = lambda m: None,
    timeout: float = 5.0,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    stop_when: Optional[Callable[[], bool]] = None,
    row_sink: Optional[Callable[[dict], None]] = None,
) -> Session:
    """Sample every group on a timer until a bound is reached.

    At least one of *duration_s* or *max_cycles* should be set; with neither,
    the caller is responsible for stopping this, which is why the CLI requires
    one of them rather than defaulting to "forever".
    """
    session = Session()
    for command in SESSION_INIT:
        transport.send(validate_command(command), timeout=timeout)

    started = clock()
    dead_cycles = 0
    while True:
        if max_cycles and session.cycles >= max_cycles:
            break
        if duration_s and (clock() - started) >= duration_s:
            break

        row: dict = {
            "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "elapsed_s": round(clock() - started, 3),
        }

        try:
            volts = parse_reply(transport.send("ATRV", timeout=timeout).data)
            row["volts"] = " ".join(volts.lines)
        except TransportError as exc:
            session.errors.append(f"ATRV: {exc}")

        for group in GROUPS:
            try:
                transport.send(validate_command(group.priority), timeout=timeout)
                for command in group.address:
                    transport.send(validate_command(command), timeout=timeout)
                for did in group.dids:
                    request = f"22{did}"
                    reply = parse_reply(
                        transport.send(
                            validate_enhanced_command(request), timeout=timeout
                        ).data
                    )
                    payload = _payload(reply, request)
                    if payload is None:
                        continue
                    decoder = DECODERS.get(did)
                    if decoder:
                        row.update(decoder(payload))
            except TransportError as exc:
                session.errors.append(f"{group.name}: {exc}")

        # Standard PIDs answer from several modules; the filter has to come off
        # or only the last-addressed one is heard.
        try:
            for command in STANDARD_ADDRESS:
                transport.send(validate_command(command), timeout=timeout)
            for command, column in STANDARD_PIDS:
                reply = parse_reply(
                    transport.send(validate_command(command), timeout=timeout).data
                )
                expect = bytes([0x41]) + bytes.fromhex(command[2:])
                for frame in reply.frames:
                    idx = frame.find(expect)
                    if idx == -1:
                        continue
                    data = frame[idx + len(expect):]
                    if column == "speed_kph" and data:
                        row[column] = data[0]
                    elif column == "odometer_km" and len(data) >= 4:
                        row[column] = round(
                            ((data[0] << 24) | (data[1] << 16)
                             | (data[2] << 8) | data[3]) / 10, 1
                        )
                    else:
                        # Everything the census added: decode.decode_pid holds
                        # the scaling and unit already, so this asks it rather
                        # than restating them here.
                        decoded = decode_pid(command[2:], reply)
                        if decoded.value is not None:
                            row[column] = decoded.value
                    break
            # PID 01 last, because it is the one that is not a scalar.  Only
            # module 17 can answer: STANDARD_ADDRESS points at it physically,
            # so this is that module's view of the lamp rather than a
            # vehicle-wide one -- which is the only view available from a
            # physically addressed request, and is worth saying.
            monitor = parse_reply(
                transport.send(
                    validate_command(MONITOR_STATUS_PID), timeout=timeout
                ).data
            )
            for status in decode_monitor_status(monitor):
                if status.status != "ok":
                    continue
                row["mil_on"] = 1 if status.mil_on else 0
                row["dtc_count"] = status.dtc_count
                break
        except TransportError as exc:
            session.errors.append(f"standard: {exc}")
        try:
            transport.send(validate_command(ENHANCED_PRIORITY), timeout=timeout)
        except TransportError as exc:
            session.errors.append(f"restore priority: {exc}")

        # Did anything at all answer?
        #
        # Every transport failure above is caught per group, so one quiet
        # module costs its own columns and nothing else -- which is right.  But
        # the same handling meant a link that had gone away entirely was also
        # swallowed, every cycle, for the rest of the session: the loop kept
        # writing rows carrying nothing but a timestamp, the service stayed
        # "active (running)", and the journal stayed quiet.  A drive recorded
        # after that point was lost while everything looked healthy.
        #
        # pyserial does not close the port on an I/O error, so the transport
        # never notices by itself and no amount of waiting brings it back.
        if any(key not in _NON_VEHICLE_COLUMNS for key in row):
            dead_cycles = 0
        else:
            dead_cycles += 1
            session.errors.append(f"cycle decoded nothing ({dead_cycles} consecutive)")
            say(f"  [{row['elapsed_s']:>8.1f}s] decoded nothing "
                f"({dead_cycles}/{DEAD_CYCLES_BEFORE_EXIT})")
            # A sleeping vehicle and a broken link both stop decoding.  Only
            # one of them is a fault, and the voltage tells them apart *here*,
            # where it is corroborated by silence, rather than on its own.
            if _asleep(transport, timeout):
                say("  nothing answered and the rail is low; vehicle asleep")
                break
            if dead_cycles >= DEAD_CYCLES_BEFORE_EXIT:
                raise TransportError(
                    f"{dead_cycles} consecutive cycles decoded nothing; exiting "
                    f"so the link is re-established"
                )
            try:
                _revive(transport, timeout=timeout, attempt=dead_cycles - 1)
                say("  link reopened and adapter re-initialised")
            except TransportError as exc:
                session.errors.append(f"reconnect: {exc}")
                say(f"  reconnect failed: {exc}")
            # No row is written.  A row of nothing but a timestamp is not a
            # sample, and writing one makes a dead link look like data.
            sleeper(interval_s)
            continue

        # Charge/discharge power, derived rather than read.
        #
        # 0x5401 is published as "charger DC power" but this vehicle answers it
        # with a single byte that is non-zero at idle (0x96) and did not scale
        # to the measured rate during an AC charge (0x93), so it is not used
        # for this.  The energy field is the better source: it moved through 80
        # distinct values across ten minutes of charging.
        #
        # But it is quantised to 0.01 kWh, and *that* is why the slope is taken
        # over a window rather than between consecutive samples.  At a ~7 s
        # cycle a single 0.01 kWh step is about 5 kW, so consecutive-sample
        # power alternated between 9.5 and 4.8 kW while the true rate was a
        # steady 7.8.  The reading was not wrong on average and was useless
        # instant to instant.  Over POWER_WINDOW_S the quantum is small against
        # the elapsed time and the number stops jittering.
        row_power = _power_over_window(session.rows, row)
        if row_power is not None:
            row["power_kw"] = row_power

        # Instantaneous HV power straight from the pack, which needs no window
        # because it is a direct product rather than a slope.  Sign follows the
        # current: negative is charging.  Keeping this alongside the slope-
        # derived figure is deliberate -- two independent routes to the same
        # quantity is what caught the 0x5401 mislabel, and disagreement between
        # them is a signal worth seeing rather than averaging away.
        if "pack_v" in row and "pack_a" in row:
            row["hv_power_kw"] = round(row["pack_v"] * row["pack_a"] / 1000.0, 2)

        session.rows.append(row)
        session.cycles += 1
        # Persisted before the next sample rather than at the end.  A
        # session ends when the vehicle powers down, which is also the
        # moment the node can lose power, so holding rows in memory until
        # then is how a drive gets lost.
        if row_sink is not None:
            row_sink(row)
        say(
            f"  [{row['elapsed_s']:>8.1f}s] soc={row.get('soc_pct')} "
            f"speed={row.get('speed_kph')} wheels="
            f"{row.get('wheel_fl_kph')}/{row.get('wheel_fr_kph')}/"
            f"{row.get('wheel_rl_kph')}/{row.get('wheel_rr_kph')} "
            f"packV={row.get('pack_v')} packA={row.get('pack_a')} "
            f"hvkW={row.get('hv_power_kw')} slopekW={row.get('power_kw')}"
        )

        if max_cycles and session.cycles >= max_cycles:
            break
        if duration_s and (clock() - started) >= duration_s:
            break
        # Checked after a row is written, so a session that ends because the
        # vehicle shut down still keeps everything it recorded.
        if stop_when is not None and stop_when():
            break
        sleeper(interval_s)

    return session


def write_csv(session: Session, path: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(COLUMNS), extrasaction="ignore",
            lineterminator="\n",   # a CRLF here once caused a false alarm
        )
        writer.writeheader()
        for row in session.rows:
            writer.writerow(row)


#: Connector volts at or above which the vehicle is treated as awake.
#:
#: This was 13.2, chosen against measured bands of 12.7-12.9 V asleep and
#: 13.7-13.9 V running.  Both figures were taken from a *parked* vehicle, and
#: driving turns out to sit between them.  The ATRV probes across the drive
#: that was lost on 2026-09-03, one every 300 s:
#:
#:     15:48:30  13.2 V   shutting down
#:     15:48:35  12.9 V   still falling
#:     15:48:39  12.7 V   off
#:     15:53:39  13.1 V   driving
#:     15:58:39  13.1 V   driving
#:     16:03:39  13.0 V   driving
#:     16:08:40  13.2 V   arrived, DC-DC boosting again
#:
#: A vehicle that has been awake a while charges its 12 V battery full, and the
#: DC-DC then holds a float around 13.0-13.1 V -- *below* the old threshold.
#:
#: **The next paragraph was wrong, and it is left standing because how it was
#: wrong is the useful part.** It read: "12.9 V was never a steady state; it was
#: one sample on the way down. So the bands do not overlap after all: asleep
#: tops out at 12.9 and driving bottoms out at 13.0, and 12.95 is the only value
#: that separates them."
#:
#: On 2026-09-04 the vehicle sat **parked and awake for over twenty minutes at a
#: steady 12.9 V**: answering service 22 from five modules, pack at 379 V,
#: drawing about 4 kW of accessory load, 146 recorded rows every one of which
#: reads `12.9V`.  12.9 is a steady state, and it is an *awake* one.
#:
#: The error was in the sampling, not the arithmetic.  Three states had been
#: observed -- asleep, driving, shutting down -- and the conclusion was drawn as
#: though those were all of them.  Parked-and-awake is a fourth, it floats lower
#: than driving because nothing is moving, and no measurement had covered it.
#: The threshold was then set 0.05 V above a state nobody had watched.
#:
#: It cost a real session: the recorder restarted at 12.9 V, read that as asleep
#: and went to its 300-second watch on a vehicle that was awake in front of it.
#:
#: And the bands do not merely touch at 12.9 -- **they overlap there.**  The
#: journal has both, hours apart:
#:
#:     16:10:48  12.9 V   nothing answered      -> genuinely asleep
#:     00:41-01:03  12.9 V   five modules answering, 146 rows -> genuinely awake
#:
#: So no threshold can classify 12.9 V correctly, and looking for one is the
#: mistake that has now been made twice.  The recorder already has the right
#: instrument and it is not the voltmeter: `record()` ends a session when
#: **nothing answers** for three consecutive cycles, which is a measurement of
#: the thing actually being asked about.  The 16:10:48 line above is that check
#: firing, not this threshold.
#:
#: The threshold's job is therefore narrower than it looks.  It decides when to
#: *try*, and answers decide whether the vehicle is really there.  Given that,
#: it belongs below the ambiguous region rather than inside it, and the
#: asymmetry settles where: **a false wake costs about three dead cycles and a
#: handful of unanswered requests; a false sleep costs an entire drive.**  12.8
#: sits above the only unambiguous sleeping reading (12.7) and below the
#: ambiguous one, so every ambiguous case is resolved by asking rather than by
#: guessing.
WAKE_VOLTS: float = 12.8


def _volts(transport: Transport, timeout: float) -> Optional[float]:
    """Connector voltage, or ``None`` if the adapter did not answer.

    ``ATRV`` is adapter-only: it reaches no vehicle module and puts nothing on
    the CAN bus, which is what makes it safe to poll while the vehicle sleeps.
    """
    try:
        reply = parse_reply(transport.send("ATRV", timeout=timeout).data)
    except TransportError:
        return None
    for line in reply.lines:
        text = line.strip().upper().rstrip("V")
        try:
            return float(text)
        except ValueError:
            continue
    return None


#: How many times an unanswered ``ATRV`` is retried promptly before the watch
#: drops back to the slow asleep cadence.  A moving vehicle glitches the RFCOMM
#: link; treating the first silence as a sleeping vehicle costs a whole
#: ``asleep_interval_s`` of a drive that is still happening.
UNANSWERED_RETRIES: int = 3

#: Gap between those prompt retries.
UNANSWERED_INTERVAL_S: float = 5.0


def _asleep(transport: Transport, timeout: float) -> bool:
    """True only when the adapter *answered* and the answer is below the band.

    An unanswered ``ATRV`` is **not** evidence of sleep.  Reading it as zero --
    which is what ``(_volts(...) or 0) < WAKE_VOLTS`` did -- meant a single
    transient Bluetooth timeout ended a session that was recording a drive,
    because zero is below every threshold.  A vehicle shutting down reports a
    real voltage; a link that is genuinely gone is what the read errors inside
    :func:`record` are for.  So silence keeps the session, and only a measured
    voltage can end it.
    """
    volts = _volts(transport, timeout)
    return volts is not None and volts < WAKE_VOLTS


def run_auto(
    transport: Transport,
    *,
    output_dir: str,
    interval_s: float = 5.0,
    asleep_interval_s: float = 300.0,
    max_session_s: float = 7200.0,
    say: Callable[[str], None] = lambda m: None,
    timeout: float = 5.0,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    stop: Callable[[], bool] = lambda: False,
) -> None:
    """Record whenever the vehicle is awake; stay silent while it sleeps.

    The sleeping branch sends **only** ``ATRV``.  That is the whole reason this
    can run unattended against a parked vehicle: the overnight measurements that
    cleared the power gate were taken with exactly that command set and showed
    ``ATCS T:00 R:00`` on every one of 158 samples, so a service that polls
    voltage and nothing else is doing what has already been proven harmless.

    A session ends when the vehicle goes back to sleep, so each wake period
    produces its own file rather than one unbounded CSV.
    """
    awake = False
    unanswered = 0
    while not stop():
        volts = _volts(transport, timeout)
        if volts is None:
            # Only ATRV is ever sent on this path, so retrying promptly puts
            # nothing extra on the CAN bus -- it just avoids spending five
            # minutes asleep because one Bluetooth read timed out mid-drive.
            unanswered += 1
            if unanswered <= UNANSWERED_RETRIES:
                say(f"adapter did not answer ATRV ({unanswered}); retrying")
                sleeper(UNANSWERED_INTERVAL_S)
            else:
                # Past the prompt retries the link is not glitching, it is
                # gone -- and this loop could not previously get it back.
                # `record` revives a link that dies mid-session, but nothing
                # revived one that died while the vehicle was parked, so the
                # watch sat on a dead file descriptor indefinitely.  Observed
                # on 2026-09-03: the vehicle slept, the OBD port lost power,
                # the adapter dropped its Bluetooth link, and the recorder
                # then reported "adapter still silent" every five minutes
                # against an rfcomm channel showing `closed`.  It would never
                # have recorded again without a restart.
                #
                # Reopening is what fixes it: hummer-rfcomm binds the device
                # connect-on-open, so closing and reopening re-establishes the
                # Bluetooth link once the adapter has power again.  This costs
                # one reconnect per slow cycle and still sends only ATRV, so a
                # genuinely sleeping vehicle sees no extra traffic.
                say("adapter still silent; reopening the link")
                try:
                    _revive(transport, timeout=timeout, attempt=unanswered)
                    say("link reopened")
                    unanswered = 0
                except TransportError as exc:
                    say(f"reopen failed: {exc}")
                sleeper(asleep_interval_s)
            continue
        unanswered = 0

        if volts < WAKE_VOLTS:
            if awake:
                say(f"vehicle asleep ({volts} V); session ended")
                awake = False
            sleeper(asleep_interval_s)
            continue

        if not awake:
            awake = True
            say(f"vehicle awake ({volts} V); starting a session")

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = f"{output_dir.rstrip('/')}/drive-{stamp}.csv"
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=list(COLUMNS), extrasaction="ignore",
                lineterminator="\n",
            )
            writer.writeheader()
            handle.flush()

            def sink(row: dict) -> None:
                writer.writerow(row)
                handle.flush()
                os.fsync(handle.fileno())

            session = record(
                transport,
                interval_s=interval_s,
                duration_s=max_session_s,
                say=say,
                timeout=timeout,
                sleeper=sleeper,
                clock=clock,
                # Deliberately NOT `or _asleep(...)`.  On 2026-09-03 this
                # truck sat awake and idle for 23 minutes at 13.9 V, which
                # topped up its 12 V battery; the DC-DC then dropped to float
                # and the whole 12.6-mile commute was driven at 12.9-13.1 V --
                # under WAKE_VOLTS.  The recorder called that "asleep", ended
                # the session, and slept 300 s at a time through the entire
                # drive.  The odometer moved 20.3 km with nothing recording.
                #
                # Lowering the threshold cannot fix it: the measured asleep
                # band is 12.7-12.9 V, so the two bands genuinely overlap.
                # Voltage cannot answer "is this vehicle awake".  Whether its
                # modules are answering can, and that is now what ends a
                # session -- with voltage kept only as corroboration once the
                # answers have already stopped.
                stop_when=stop,
                row_sink=sink,
            )
        say(f"{session.cycles} cycles -> {path}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hummer-obd-drive",
        description=(
            "Record a bounded drive or charge session: all approved enhanced "
            "identifiers plus speed and odometer, decoded. Transmits only with "
            "--confirm."
        ),
    )
    parser.add_argument("--device", default="/dev/rfcomm0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--interval-s", type=float, default=5.0)
    parser.add_argument("--duration-s", type=float, default=0.0)
    parser.add_argument("--max-cycles", type=int, default=0)
    parser.add_argument("--output", default="evidence/drive.csv")
    parser.add_argument("--raw-log", default="logs/drive-raw.jsonl")
    parser.add_argument("--label", default="", help="what this session is (e.g. 'highway drive')")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument(
        "--auto",
        action="store_true",
        help=(
            "run continuously: record while the vehicle is awake, poll only "
            "ATRV while it sleeps. This is the service mode."
        ),
    )
    parser.add_argument("--output-dir", default="evidence/sessions")
    parser.add_argument("--asleep-interval-s", type=float, default=300.0)
    parser.add_argument("--max-session-s", type=float, default=7200.0)
    args = parser.parse_args(argv)

    if not args.auto and not args.duration_s and not args.max_cycles:
        print(
            "refusing to start without a bound: pass --duration-s or --max-cycles",
            file=sys.stderr,
        )
        return 2

    def say(message: str) -> None:
        print(message, flush=True)

    if not args.confirm:
        say("DRY RUN - nothing transmitted, no serial device opened.")
        say(f"would sample every {args.interval_s}s, bound "
            f"{args.duration_s or '-'}s / {args.max_cycles or '-'} cycles")
        for command in SESSION_INIT:
            validate_command(command)
        for group in GROUPS:
            for command in group.address:
                validate_command(command)
            for did in group.dids:
                validate_enhanced_command(f"22{did}")
            say(f"  {group.name:<12} ecu {group.ecu}  {', '.join(group.dids)}")
        for command, column in STANDARD_PIDS:
            validate_command(command)
        say(f"  {'standard':<12}          {', '.join(c for c, _ in STANDARD_PIDS)}")
        say("\nevery command above validated. re-run with --confirm to record.")
        return 0

    say(f"opening {args.device} ...")
    try:
        with RawLog(
            args.raw_log, "drive-session",
            meta={"role": "drive_session", "label": args.label},
        ) as rawlog:
            with SerialTransport(
                args.device, rawlog, baudrate=args.baud,
                validator=validate_supervised_command,
            ) as transport:
                if args.auto:
                    os.makedirs(args.output_dir, exist_ok=True)
                    say(f"auto mode: sessions -> {args.output_dir}/")
                    run_auto(
                        transport,
                        output_dir=args.output_dir,
                        interval_s=args.interval_s,
                        asleep_interval_s=args.asleep_interval_s,
                        max_session_s=args.max_session_s,
                        say=say,
                    )
                    return 0
                session = record(
                    transport,
                    interval_s=args.interval_s,
                    duration_s=args.duration_s,
                    max_cycles=args.max_cycles,
                    say=say,
                )
    except TransportError as exc:
        print(f"transport failed: {exc}", file=sys.stderr)
        return 3

    write_csv(session, args.output)
    say(f"\n{session.cycles} cycles written to {args.output}")
    if session.errors:
        say(f"{len(session.errors)} transport errors (first 5):")
        for err in session.errors[:5]:
            say(f"  {err}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
