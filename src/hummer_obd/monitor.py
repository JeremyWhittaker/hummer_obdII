"""Listen to the diagnostic connector without transmitting to the vehicle.

Every other tool here asks a question and reads the answer. This one asks
nothing. It puts the adapter into receive-only CAN monitoring and records
whatever arrives, which is a different kind of question: not *what will a module
tell me if I ask*, but *is anything being said at all*.

`docs/PASSIVE_CAN_VALIDATION.md` did the safety analysis before any of this
existed and set the terms it had to meet. Two are worth restating because they
shape the code:

**Silence has to be real.** On CAN, a listening node normally asserts a dominant
bit in the acknowledgement slot of every frame it receives. That is a
transmission -- short, unaddressed, invisible in any frame log -- and it still
means the adapter is driving the bus. ``STCMM0`` is the vendor-documented mode
where it does not, and it is the difference between "we only read" and "we do
not transmit". This project already makes the stronger promise for the 12 V
watch, so it keeps it here.

**A monitor stream is not a request/response exchange.**
:meth:`SerialTransport.send` reads until the adapter emits ``>``, which a
monitor stream never does; it would block for the full timeout and return
truncated bytes flagged as a timeout, which is wrong and quietly wrong. It also
writes to the raw log only after its loop finishes, breaking the rule that raw
bytes reach the transcript before anything parses them. So the streaming path is
separate, and the gate split makes the mistake unmakeable: this transport is
built with the *setup* validator, which refuses the stream command outright.

What this tool cannot tell you is whether the vehicle's internal networks are
busy. The connector sits on the gateway's diagnostic side, and the link is ASCII
over Bluetooth at 115200 baud -- a few hundred frames per second against a bus
that carries thousands. **Frame counts here are not a measurement of bus load.**
A quiet capture says the gateway forwards little to this connector. It says
nothing about what is happening behind it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Final, Optional

from .safety import (
    MONITOR_CAN_MODE,
    MONITOR_STREAM_COMMAND,
    UnsafeCommandError,
    validate_monitor_setup_command,
    validate_monitor_stream_command,
)
from .transport import PROMPT, SerialTransport, TransportError

__all__ = [
    "MONITOR_COMMANDS",
    "MAX_CAPTURE_SECONDS",
    "MAX_CAPTURE_BYTES",
    "STOP_CHARACTER",
    "CaptureResult",
    "MonitorTransport",
    "assert_no_vehicle_traffic",
    "main",
]

#: Hard ceilings on the bounds themselves.  A bound a caller can set to
#: infinity is not a bound, and this runs against a vehicle.
MAX_CAPTURE_SECONDS: Final[float] = 60.0
MAX_CAPTURE_BYTES: Final[int] = 4_000_000

#: How monitoring is stopped: any character over the UART to the adapter.  A
#: module constant rather than a flag -- an operator-supplied "character" could
#: be a multi-byte string, which is a command the gate never saw.
STOP_CHARACTER: Final[bytes] = b"\r"

#: Adapter commands that put traffic on the bus by themselves.  All three
#: select protocol *auto*, and auto-detection discovers a protocol **by
#: transmitting** -- it sends until something answers.  A tool promising that
#: nothing reaches the vehicle cannot auto-detect its way onto the bus, so the
#: protocol is pinned instead.  (Note ``STP0``, digit zero, is protocol auto;
#: ``STPO``, letter O, is protocol *open* and is a different command.)
_MAY_INITIATE_BUS_TRAFFIC: Final[frozenset[str]] = frozenset(
    {"ATSP0", "ATTP0", "STP0"}
)


def assert_no_vehicle_traffic(commands: tuple[str, ...]) -> tuple[str, ...]:
    """Every command must be adapter configuration that cannot reach the bus.

    ``voltage.py`` does the same thing for the 12 V watch, and this is stricter
    in one way: passing the gate and starting with ``AT``/``ST`` is necessary
    but not sufficient, because a handful of adapter commands transmit in order
    to do their job.  Run at import, so a well-meaning edit fails before the
    module can be loaded rather than in front of a vehicle.
    """
    checked: list[str] = []
    for command in commands:
        safe = validate_monitor_setup_command(command)
        if not (safe.startswith("AT") or safe.startswith("ST")):
            raise UnsafeCommandError(
                f"refused {safe}: the monitor path sends adapter commands only"
            )
        if safe in _MAY_INITIATE_BUS_TRAFFIC:
            raise UnsafeCommandError(
                f"refused {safe}: it reaches the vehicle to do its work, and "
                f"this path must not"
            )
        checked.append(safe)
    return tuple(checked)


#: The complete list of what is transmitted before monitoring starts.  ``ATSP7``
#: pins the protocol this vehicle uses (ISO 15765-4, 29-bit, 500 kbit/s) rather
#: than letting the adapter search for it.  ``ATCAF0`` turns off CAN auto
#: formatting so frames arrive as sent.  ``ATCS`` reads the CAN error counters,
#: which are checked again afterwards.
MONITOR_COMMANDS: Final[tuple[str, ...]] = assert_no_vehicle_traffic((
    "ATZ", "ATE0", "ATL0", "ATS1", "ATH1", "ATAL",
    "ATSP7",
    "ATCAF0",
    "ATCS",
    MONITOR_CAN_MODE,
))


@dataclass(frozen=True)
class CaptureResult:
    """What one capture did, including the ways it can be unsatisfying."""

    command: str
    bytes_captured: int
    records_written: int
    elapsed_s: float
    stop_reason: str
    stop_acknowledged: bool
    hit_byte_bound: bool


class MonitorTransport(SerialTransport):
    """A serial transport that can also stream, for the capture tool only.

    Deliberately a subclass in its own module rather than a method on
    :class:`SerialTransport`.  ``collector.py`` constructs a ``SerialTransport``;
    giving *that* class a ``capture`` method would mean every object the
    unattended collector holds could start a stream, and "unreachable from the
    collector" would drop from a structural property to a convention.  As a
    subclass it stays assertable::

        assert not hasattr(SerialTransport, "capture")
    """

    def __init__(self, *args, clock: Callable[[], float] = time.monotonic,
                 **kwargs) -> None:
        # The setup validator, so send() cannot start monitoring: the stream
        # command is refused by the very gate this transport was built with.
        kwargs.setdefault("validator", validate_monitor_setup_command)
        super().__init__(*args, **kwargs)
        self._clock = clock

    def capture(
        self,
        command: str,
        *,
        max_seconds: float,
        max_bytes: int,
        chunk_bytes: int = 4096,
        flush_interval_s: float = 0.25,
        stop_drain_s: float = 2.0,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> CaptureResult:
        """Stream whatever arrives until a bound is reached, then stop.

        Bounded by wall clock *and* byte count, because either alone can run
        away: a silent bus never fills a byte budget, and a busy one fills it
        long before a clock expires.
        """
        safe = validate_monitor_stream_command(command)
        if not 0 < max_seconds <= MAX_CAPTURE_SECONDS:
            raise ValueError(
                f"max_seconds must be in (0, {MAX_CAPTURE_SECONDS}]")
        if not 0 < max_bytes <= MAX_CAPTURE_BYTES:
            raise ValueError(f"max_bytes must be in (0, {MAX_CAPTURE_BYTES}]")
        if not self.is_open:
            raise TransportError("transport is not open")

        # Deliberately NOT reset_input_buffer(): send() clears the port before
        # every command, which is right for request/response and would silently
        # discard stream bytes here.  Anything already waiting is recorded
        # instead of dropped.
        waiting = getattr(self._serial, "in_waiting", 0) or 0
        if waiting:
            self.rawlog.log_rx(self._serial.read(waiting),
                               note="pre-capture residue")

        self.rawlog.write_event("capture_start", {
            "command": safe,
            "max_seconds": max_seconds,
            "max_bytes": max_bytes,
            "stop_char_hex": STOP_CHARACTER.hex(),
        })

        payload = (safe + "\r").encode("ascii")
        # Logged before it is written, as send() does: a transmitted byte that
        # failed to be logged is worse than a logged byte that failed to send.
        self.rawlog.log_tx(payload, note=f"start monitoring ({safe})")
        try:
            self._serial.write(payload)
            self._serial.flush()
        except Exception as exc:  # pragma: no cover - exercised via fakes
            self.rawlog.write_event("write_failed", {"error": str(exc)})
            raise TransportError(f"write failed: {exc}") from exc

        # The adapter sometimes echoes the stream command back despite ATE0 --
        # observed once in four captures on 2026-09-04, where it made a capture
        # that received nothing from the vehicle report "5 bytes". Bytes this
        # tool transmitted are not bytes the vehicle sent, and a capture whose
        # entire content is its own command must read as empty. Recorded under
        # its own note so it is preserved rather than silently dropped.
        echo_expected = payload
        started = self._clock()
        deadline = started + max_seconds
        next_flush = started + flush_interval_s
        pending = bytearray()
        total = 0
        records = 0
        stop_reason = "duration"

        def flush(note: str) -> None:
            nonlocal pending, records
            if pending:
                self.rawlog.log_rx(bytes(pending), note=note)
                records += 1
                pending = bytearray()

        try:
            while True:
                if should_stop():
                    stop_reason = "stopped"
                    break
                if self._clock() >= deadline:
                    stop_reason = "duration"
                    break
                # Each read is banked into `pending` before the next one is
                # attempted.  Building one `chunk` across both reads and
                # appending afterwards loses the first read's bytes when the
                # second raises -- they reached the program and never reached
                # the transcript, which is the one thing the raw log exists to
                # prevent.
                chunk = self._serial.read(1)
                if chunk:
                    pending += chunk
                    total += len(chunk)
                    if echo_expected and bytes(pending) == echo_expected[:len(pending)]:
                        # Still matching our own command; not vehicle data.
                        if bytes(pending) == echo_expected:
                            self.rawlog.log_rx(bytes(pending),
                                               note="stream command echo (not vehicle data)")
                            total -= len(pending)
                            pending = bytearray()
                            echo_expected = b""
                        continue
                    echo_expected = b""
                available = getattr(self._serial, "in_waiting", 0) or 0
                if available:
                    more = self._serial.read(min(available, chunk_bytes))
                    pending += more
                    total += len(more)
                if total >= max_bytes:
                    stop_reason = "byte_limit"
                    break
                now = self._clock()
                if len(pending) >= chunk_bytes or now >= next_flush:
                    # A deadline, not "elapsed >= interval": the latter is true
                    # on every iteration once the first interval passes, which
                    # writes one raw-log record per byte.
                    next_flush = now + flush_interval_s
                    flush(f"capture {safe}")
        except Exception as exc:
            # The bytes that did arrive are evidence and must reach the
            # transcript before the failure propagates.
            flush("partial before read error")
            self.rawlog.write_event("capture_read_failed", {"error": str(exc)})
            raise TransportError(f"read failed: {exc}") from exc
        finally:
            flush(f"capture {safe}")

        elapsed = self._clock() - started
        acknowledged = self._stop(stop_drain_s)
        self.rawlog.write_event("capture_end", {
            "bytes": total, "records": records, "elapsed_s": round(elapsed, 3),
            "stop_reason": stop_reason, "stop_acknowledged": acknowledged,
        })
        return CaptureResult(
            command=safe, bytes_captured=total, records_written=records,
            elapsed_s=elapsed, stop_reason=stop_reason,
            stop_acknowledged=acknowledged,
            hit_byte_bound=stop_reason == "byte_limit",
        )

    def _stop(self, drain_s: float) -> bool:
        """Send the stop character and drain to the prompt."""
        self.rawlog.log_tx(
            STOP_CHARACTER,
            note="monitor stop character (UART to the adapter, not onto CAN)",
        )
        try:
            self._serial.write(STOP_CHARACTER)
            self._serial.flush()
        except Exception as exc:  # pragma: no cover
            self.rawlog.write_event("stop_write_failed", {"error": str(exc)})
            return False
        tail = bytearray()
        deadline = self._clock() + drain_s
        while self._clock() < deadline:
            chunk = self._serial.read(1)
            if chunk:
                tail += chunk
                if PROMPT in chunk:
                    break
        if tail:
            self.rawlog.log_rx(bytes(tail), note="post-stop drain")
        return PROMPT in bytes(tail)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record the diagnostic connector without transmitting to "
                    "the vehicle. Receive-only: the adapter does not even "
                    "acknowledge the frames it hears."
    )
    parser.add_argument("--device", default="/dev/rfcomm0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--seconds", type=float, default=30.0,
                        help=f"capture length, at most {MAX_CAPTURE_SECONDS}")
    parser.add_argument("--max-bytes", type=int, default=500_000)
    parser.add_argument("--label", default="",
                        help="what the vehicle was doing (one event per capture)")
    parser.add_argument("--output", help="write the capture summary JSON here")
    parser.add_argument("--confirm", action="store_true",
                        help="required: this opens the serial device")
    args = parser.parse_args(argv)

    if not args.confirm:
        print("DRY RUN - nothing is transmitted and no serial device is opened.")
        print("would send, in order:")
        for command in MONITOR_COMMANDS:
            print(f"    {command}")
        print(f"    {MONITOR_STREAM_COMMAND}      (start monitoring)")
        print(f"    {STOP_CHARACTER!r}     (stop)")
        print("    ATCS       (counters again)")
        print(f"\nthen record for up to {args.seconds}s or {args.max_bytes} bytes.")
        print("Nothing above reaches the CAN bus. Re-run with --confirm.")
        return 0

    # Imported here so a dry run needs no serial library at all.
    from .rawlog import RawLog

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_path = f"logs/raw/monitor-{stamp}.jsonl"
    counters: dict[str, str] = {}
    result: Optional[CaptureResult] = None

    # fsync=False deliberately: rawlog fsyncs every record by default, and at
    # frame rates that would dominate the runtime -- the measurement would be
    # measuring its own logging.  Records are flushed to the OS on every write
    # either way, so only a machine-level crash loses anything, and a capture
    # is repeatable.
    with RawLog(raw_path, session_id=f"monitor-{stamp}", fsync=False,
                meta={"label": args.label}) as raw:
        # The claim, written into the evidence it is checked against.
        raw.write_event("transmit_manifest", {
            "setup": list(MONITOR_COMMANDS),
            "stream": MONITOR_STREAM_COMMAND,
            "stop_char_hex": STOP_CHARACTER.hex(),
        })
        transport = MonitorTransport(
            args.device, raw, baudrate=args.baud,
            # A monitor bound is only as sharp as this: the loop checks the
            # clock once per read, so a 2 s read timeout turns a 30 s capture
            # into a 32 s one.
            read_timeout_s=0.2,
        )
        try:
            transport.open()
            for command in MONITOR_COMMANDS:
                reply = transport.send(command, timeout=5.0)
                if command == "ATCS":
                    counters["before"] = reply.data.decode("ascii", "replace").strip()
            result = transport.capture(
                MONITOR_STREAM_COMMAND,
                max_seconds=min(args.seconds, MAX_CAPTURE_SECONDS),
                max_bytes=min(args.max_bytes, MAX_CAPTURE_BYTES),
            )
            after = transport.send("ATCS", timeout=5.0)
            counters["after"] = after.data.decode("ascii", "replace").strip()
        except UnsafeCommandError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
        except TransportError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 3
        finally:
            transport.close()

    print(f"\ncaptured {result.bytes_captured} bytes in "
          f"{result.elapsed_s:.1f}s ({result.stop_reason})")
    print(f"  records written : {result.records_written}")
    print(f"  stop acknowledged: {result.stop_acknowledged}")
    print(f"  CAN counters before: {counters.get('before', '?')}")
    print(f"  CAN counters after : {counters.get('after', '?')}")
    print(f"  transcript: {raw_path}")
    if result.bytes_captured == 0:
        print("\nNothing arrived. That is a result, not a failure: the gateway")
        print("forwards little or nothing unsolicited to this connector. It says")
        print("nothing about whether the vehicle's internal networks are busy.")
    else:
        print("\nFrame counts here are NOT a measurement of bus load: ASCII over")
        print("Bluetooth at 115200 caps at a few hundred frames per second")
        print("against a bus carrying thousands. This capture is lossy by")
        print("construction, and the loss is not recorded anywhere.")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump({
                "started_utc": stamp, "label": args.label,
                "transcript": raw_path, "can_counters": counters,
                "bytes": result.bytes_captured,
                "records": result.records_written,
                "elapsed_s": round(result.elapsed_s, 3),
                "stop_reason": result.stop_reason,
                "stop_acknowledged": result.stop_acknowledged,
            }, handle, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
