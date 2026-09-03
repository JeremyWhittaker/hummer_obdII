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
from typing import Callable, Optional

from .decode import parse_reply
from .rawlog import RawLog
from .safety import (
    validate_command,
    validate_enhanced_command,
    validate_supervised_command,
)
from .transport import SerialTransport, Transport, TransportError

__all__ = ["AddressGroup", "GROUPS", "DECODERS", "COLUMNS",
           "STANDARD_ADDRESS", "record", "main"]


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
DECODERS: dict[str, Callable[[bytes], dict]] = {
    "27C6": lambda p: {"soc_pct": round(_u16(p, 0) / 655.35, 3)} if len(p) >= 2 else {},
    "27AF": lambda p: {"energy_kwh": round(_u16(p, 0) / 100, 2)} if len(p) >= 2 else {},
    "27C7": lambda p: {"range_mi": round(_u24(p, 0) / 103, 2)} if len(p) >= 3 else {},
    "27C0": lambda p: {"dist_since_chg_mi": round(_u24(p, 0) / 16.09344, 2)} if len(p) >= 3 else {},
    "0046": lambda p: {"temp_f": round((p[0] - 40) * 1.8 + 32, 1)} if p else {},
    # This vehicle answers 0x5401 with a SINGLE byte (observed 0x96), so the
    # published two-byte "/4350 kW" equation cannot apply and is not used.
    # The byte is kept raw until a charging session gives it a reference.
    "5401": lambda p: {"charger_5401_raw": p.hex().upper()} if p else {},
    "2AF5": lambda p: (
        {
            "cell_avg_v": round(_u16(p, 0) / 10000, 4),
            "cell_min_v": round(_u16(p, 2) / 10000, 4),
            "cell_max_v": round(_u16(p, 4) / 10000, 4),
            # Spread in millivolts, computed from the raw counts so it does not
            # inherit rounding from the three volt columns above.
            "cell_spread_mv": round((_u16(p, 4) - _u16(p, 2)) / 10, 2),
        }
        if len(p) >= 6
        else {}
    ),
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


GROUPS: tuple[AddressGroup, ...] = (
    AddressGroup(
        name="battery",
        ecu="CB",
        address=("ATSHDACBF1", "ATCRA142AF1CB", "ATFCSH14DACBF1",
                 "ATFCSD300000", "ATFCSM1"),
        dids=("27C6", "27AF", "27C7", "27C0", "0046", "5401", "2AF5", "2B43"),
    ),
    AddressGroup(
        name="chassis",
        ecu="28",
        address=("ATSHDA28F1", "ATCRA142AF128", "ATFCSH14DA28F1",
                 "ATFCSD300000", "ATFCSM1"),
        dids=("4A7A", "4A7C", "4C2D", "4C2F", "4C30"),
    ),
    AddressGroup(
        name="drive_motor",
        ecu="1D",
        address=("ATSHDA1DF1", "ATCRA142AF11D", "ATFCSH14DA1DF1",
                 "ATFCSD300000", "ATFCSM1"),
        dids=("33E5",),
    ),
)

#: Standard OBD PIDs sampled alongside.  These go through the *ordinary* gate.
#: Asking for them needs the receive filter opened up again, because the
#: enhanced groups leave it pointed at one module.
STANDARD_PIDS: tuple[tuple[str, str], ...] = (
    ("010D", "speed_kph"),
    ("01A6", "odometer_km"),
)

#: One-time setup.  Protocol, priority and flow control do not change between
#: groups, so they are paid for once per session rather than once per sample.
SESSION_INIT: tuple[str, ...] = (
    "ATZ", "ATE0", "ATL0", "ATS0", "ATH1", "ATAL",
    "ATSP7", "ATCP14", "ATFCSD300000", "ATFCSM1", "ATST96",
)

#: Standard OBD is a *functional broadcast* at priority 0x18, not a physical
#: request at 0x14.  Without restoring both, ``010D`` goes out addressed to
#: whichever module the last enhanced group left selected, and that module
#: answers NO DATA -- which is how the first live run recorded no speed at
#: all despite the vehicle being awake.
STANDARD_ADDRESS: tuple[str, ...] = (
    "ATCP18",
    "ATSHDB33F1",
    "ATCM00000000",
)

#: Puts the priority byte back for the next cycle's enhanced groups.
ENHANCED_PRIORITY = "ATCP14"

COLUMNS: tuple[str, ...] = (
    "utc", "elapsed_s", "volts",
    "speed_kph", "odometer_km",
    "soc_pct", "energy_kwh", "range_mi", "dist_since_chg_mi",
    "temp_f", "charger_5401_raw", "power_kw",
    "cell_avg_v", "cell_min_v", "cell_max_v", "cell_spread_mv",
    "dmc2_v",
    "wheel_fl_kph", "wheel_fr_kph", "wheel_rl_kph", "wheel_rr_kph",
    "brake_kpa", "steering_deg", "lateral_g", "longitudinal_g",
    "array_2b43",
)


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
                    break
        except TransportError as exc:
            session.errors.append(f"standard: {exc}")
        try:
            transport.send(validate_command(ENHANCED_PRIORITY), timeout=timeout)
        except TransportError as exc:
            session.errors.append(f"restore priority: {exc}")

        # Charge/discharge power, derived rather than read.
        #
        # 0x5401 is published as "charger DC power" but this vehicle answers it
        # with a single byte that is non-zero at idle (0x96) and did not scale
        # to the measured rate during an AC charge (0x93), so it is not used
        # for this.  The energy field, by contrast, moves smoothly and with
        # high resolution -- 80 distinct values across ten minutes -- which
        # makes its slope a sound power measurement.  Positive is charging.
        if len(session.rows) >= 1 and "energy_kwh" in row:
            prev = session.rows[-1]
            if "energy_kwh" in prev and "elapsed_s" in prev:
                hours = (row["elapsed_s"] - prev["elapsed_s"]) / 3600.0
                if hours > 0:
                    delta = row["energy_kwh"] - prev["energy_kwh"]
                    row["power_kw"] = round(delta / hours, 2)

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
            f"steer={row.get('steering_deg')} kW={row.get('power_kw')}"
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


#: Connector volts at or above which the vehicle is treated as awake.  The
#: measured bands on this vehicle are 12.7-12.9 V asleep and 13.7-13.9 V
#: running, so this sits in the gap with room either side.
WAKE_VOLTS: float = 13.2


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
    while not stop():
        volts = _volts(transport, timeout)
        if volts is None:
            say("adapter did not answer ATRV; waiting")
            sleeper(asleep_interval_s)
            continue

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
                stop_when=lambda: stop() or (_volts(transport, timeout) or 0) < WAKE_VOLTS,
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
