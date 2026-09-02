"""Long-running read-only collector.

Not enabled by default and deliberately dull:

* it polls a configured list of service 01 PIDs,
* it re-reads DTCs (services 03/07/0A) on a slow timer,
* every byte still lands in the append-only raw log first,
* decoded values go to SQLite, which is the local buffer — nothing is
  uploaded unless upload is explicitly enabled *and* an endpoint is set,
* when the vehicle stops answering (asleep), it backs off instead of
  hammering the bus, and reconnects with capped exponential backoff when the
  RFCOMM link drops,
* a run can be bounded by cycle count or wall-clock time, so the first
  continuous trial on a real vehicle stops itself instead of depending on
  someone remembering to press Ctrl-C.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, CollectorConfig, load_config
from .rawlog import RawLog
from .safety import UnsafeCommandError, validate_command
from .session import AdapterSession
from .storage import Storage
from .transport import SerialTransport, TransportError

__all__ = ["Collector", "RunLimits", "main"]

#: Longest single ``time.sleep`` the collector will take.  Waits are chopped
#: into slices this size so a SIGTERM and a trial deadline are both noticed
#: within a second, however long the configured backoff is.
_SLEEP_SLICE_S = 1.0


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@dataclass
class RunLimits:
    """Bounds on one collector run.

    ``0`` means "no limit" for both counters, which is how the always-on
    service runs.  A supervised trial sets one or both so the run ends by
    itself: that is the whole point of the step between a single ``--once``
    cycle and a service that never stops.
    """

    poll_interval_s: float = 2.0
    max_cycles: int = 0
    duration_s: float = 0.0

    @classmethod
    def from_config(
        cls,
        collector: CollectorConfig,
        *,
        max_cycles: int | None = None,
        duration_s: float | None = None,
        poll_interval_s: float | None = None,
    ) -> "RunLimits":
        """Take the configured limits and apply this run's overrides.

        Only the overrides are checked here.  The configured values were
        already validated by :meth:`CollectorConfig.validate` when the file was
        loaded, so an override is the only unchecked input at this point.
        """
        limits = cls(
            poll_interval_s=float(collector.poll_interval_s),
            max_cycles=int(collector.max_cycles),
            duration_s=float(collector.duration_s),
        )
        if poll_interval_s is not None:
            if float(poll_interval_s) <= 0:
                raise ValueError("poll_interval_s must be greater than zero")
            limits.poll_interval_s = float(poll_interval_s)
        if max_cycles is not None:
            if int(max_cycles) < 1:
                # 0 means "no limit" in the config file, but on the command
                # line it is the one input where a typo would *remove* a bound
                # that the config had set.  Everything else here fails closed;
                # so does this.  Omit the flag to use the configured value.
                raise ValueError(
                    "--max-cycles must be 1 or more; omit it to use the "
                    "configured collector.max_cycles"
                )
            limits.max_cycles = int(max_cycles)
        if duration_s is not None:
            if float(duration_s) < 0:
                raise ValueError("duration_s must not be negative")
            limits.duration_s = float(duration_s)
        return limits

    def describe(self) -> str:
        """One line naming the interval and the limits actually in force."""
        cycles = f"max {self.max_cycles} cycles" if self.max_cycles else "unlimited cycles"
        duration = f"max {self.duration_s:g}s" if self.duration_s else "no time limit"
        return f"interval {self.poll_interval_s:g}s, {cycles}, {duration}"


class Collector:
    def __init__(
        self,
        cfg: Config,
        *,
        once: bool = False,
        logger=print,
        max_cycles: int | None = None,
        duration_s: float | None = None,
        poll_interval_s: float | None = None,
    ) -> None:
        self.cfg = cfg
        self.once = once
        self.log = logger
        self.running = True
        self.session_uid = f"collect-{_stamp()}"
        #: Completed passes over the PID list, and why the run ended.
        self.cycles = 0
        self.stop_reason = ""
        #: Set for the duration of :meth:`run`; ``None`` means no time limit.
        self._deadline: float | None = None
        # Validate the configured PID list before opening anything at all: a
        # typo in the config must fail loudly, not reach the bus.  Entries are
        # full service 01 requests ("010C"), never bare PIDs, so a stray "04"
        # in the list is an error rather than a silently reinterpreted PID.
        self.commands = [self._as_service01(p) for p in cfg.collector.pids]
        self.limits = RunLimits.from_config(
            cfg.collector,
            max_cycles=max_cycles,
            duration_s=duration_s,
            poll_interval_s=poll_interval_s,
        )
        cfg.upload.validate()

    @staticmethod
    def _as_service01(entry: str) -> str:
        """Validate one ``collector.pids`` entry as a service 01 request."""
        candidate = "".join(str(entry).split()).upper()
        if not candidate.startswith("01"):
            raise UnsafeCommandError(
                f"collector.pids entry {entry!r} must be a full service 01 request "
                f'such as "010C", not a bare PID or another service'
            )
        return validate_command(candidate)

    def stop(self, *_args) -> None:
        self.running = False

    # -- run limits ------------------------------------------------------
    def _limit_reached(self, deadline: float | None) -> str:
        """Name the limit that has been hit, or return "" to keep polling."""
        if self.limits.max_cycles and self.cycles >= self.limits.max_cycles:
            return "max_cycles reached"
        if deadline is not None and time.monotonic() >= deadline:
            return "duration reached"
        return ""

    def _sleep(self, seconds: float, deadline: float | None) -> None:
        """Wait between cycles, waking early for a stop request or the deadline.

        One ``time.sleep(idle_backoff_s)`` would let a ten minute trial
        overshoot by a whole backoff interval and would make Ctrl-C feel dead
        for just as long, so the wait is sliced and both exit conditions are
        re-checked every slice.
        """
        end = time.monotonic() + max(0.0, seconds)
        while self.running:
            now = time.monotonic()
            remaining = end - now
            if deadline is not None:
                remaining = min(remaining, deadline - now)
            if remaining <= 0:
                return
            time.sleep(min(_SLEEP_SLICE_S, remaining))

    def _wait(self, seconds: float) -> None:
        """Sliced wait bound to this run's deadline; handed to the transport."""
        self._sleep(seconds, self._deadline)

    def _summary(self) -> str:
        if self.stop_reason:
            return self.stop_reason
        if self.once:
            return "single cycle complete"
        return "stopped on request"

    # -- main loop -------------------------------------------------------
    def run(self) -> int:
        raw_path = Path(self.cfg.path(self.cfg.collector.raw_log_dir)) / f"{self.session_uid}.jsonl"
        db_path = self.cfg.path(self.cfg.collector.database)
        errors = 0
        self.cycles = 0
        self.stop_reason = ""
        # --once is one cycle by definition, so the trial limits do not apply
        # to it: they exist for the step after it.
        bounded = self.limits.duration_s and not self.once
        deadline = time.monotonic() + self.limits.duration_s if bounded else None
        self._deadline = deadline
        with RawLog(raw_path, self.session_uid, meta={"role": "collector"}) as rawlog, \
                Storage(db_path) as store:
            sid = store.start_session(self.session_uid, raw_log_path=str(raw_path), notes="collector")
            self.log(f"{self.session_uid} starting: "
                     f"{'single cycle' if self.once else self.limits.describe()}")
            transport = SerialTransport(
                self.cfg.adapter.device,
                rawlog,
                baudrate=self.cfg.adapter.baudrate,
                read_timeout_s=self.cfg.adapter.read_timeout_s,
                command_timeout_s=self.cfg.adapter.command_timeout_s,
                reconnect_initial_s=self.cfg.adapter.reconnect_initial_s,
                reconnect_max_s=self.cfg.adapter.reconnect_max_s,
                # The reconnect backoff is the longest sleep in the loop.  Route
                # it through the same sliced wait as the others so a bounded
                # trial and a SIGTERM are honoured during a link flap too.
                sleeper=self._wait,
            )
            session = AdapterSession(transport, logger=self.log)
            next_dtc = 0.0
            attempt = 0
            while self.running:
                # The wait between cycles can be cut short by the deadline, so
                # re-check it here rather than only after a completed cycle.
                self.stop_reason = self._limit_reached(deadline)
                if self.stop_reason:
                    break
                try:
                    if not transport.is_open:
                        transport.open()
                        fp = session.initialize()
                        session.negotiate_protocol()
                        store.update_session(sid, adapter_id=fp.adapter_id, protocol=fp.protocol)
                        store.add_event("connected", fp.protocol, session_id=sid)
                        attempt = 0
                    cycle_had_data = False
                    completed = True
                    for command in self.commands:
                        if not self.running:
                            completed = False
                            break
                        pid = command[2:4]
                        value, reply = session.read_pid(pid)
                        store.add_sample(sid, value)
                        cycle_had_data = cycle_had_data or value.status == "ok"
                    if completed:
                        # A cycle is one finished pass over the PID list.  A
                        # pass that got nothing still counts, otherwise a
                        # sleeping vehicle would make a bounded trial run for
                        # ever on idle backoff alone.
                        self.cycles += 1
                    now = time.monotonic()
                    if self.cfg.collector.dtc_interval_s and now >= next_dtc:
                        for mode in ("03", "07", "0A"):
                            codes, reply = session.read_dtcs(mode)
                            store.add_dtc_read(sid, mode, codes, " ".join(f.hex() for f in reply.frames))
                        next_dtc = now + self.cfg.collector.dtc_interval_s
                    errors = 0
                    if self.once:
                        break
                    self.stop_reason = self._limit_reached(deadline)
                    if self.stop_reason:
                        break
                    if cycle_had_data:
                        self._sleep(self.limits.poll_interval_s, deadline)
                    else:
                        # The vehicle is most likely asleep.  Back off; never
                        # escalate to more aggressive probing.
                        store.add_event("idle_backoff", "no data this cycle", session_id=sid)
                        self._sleep(self.cfg.collector.idle_backoff_s, deadline)
                except TransportError as exc:
                    errors += 1
                    attempt += 1
                    store.add_event("transport_error", str(exc), session_id=sid)
                    self.log(f"transport error ({errors}): {exc}")
                    if errors >= self.cfg.collector.max_consecutive_errors:
                        store.add_event("giving_up", "too many consecutive errors", session_id=sid)
                        store.end_session(sid)
                        return 3
                    try:
                        transport.reconnect(attempt)
                    except TransportError as reconnect_exc:
                        self.log(f"reconnect failed: {reconnect_exc}")
                except KeyboardInterrupt:
                    self.running = False
            if self.stop_reason:
                store.add_event("stopped", self.stop_reason, session_id=sid)
            transport.close()
            store.end_session(sid)
            self.log(f"{self.session_uid} finished: {self._summary()} after {self.cycles} cycles")
        return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only OBD collector")
    parser.add_argument("--config", help="path to hummer.toml")
    parser.add_argument("--root", default=".", help="project root for relative paths")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument("--max-cycles", type=int, metavar="N",
                        help="stop after N completed poll cycles, 0 for no limit "
                             "(default: collector.max_cycles)")
    parser.add_argument("--duration-s", type=float, metavar="S",
                        help="stop after S seconds of wall-clock time, 0 for no limit "
                             "(default: collector.duration_s)")
    parser.add_argument("--poll-interval-s", type=float, metavar="S",
                        help="override collector.poll_interval_s for this run, so a trial "
                             "can poll conservatively without editing the deployed config")
    parser.add_argument("--force", action="store_true",
                        help="run even when collector.enabled is false in the config; this is "
                             "the expected way to run a bounded trial (--max-cycles/--duration-s) "
                             "before the service is switched on")
    args = parser.parse_args(argv)
    # Reject the limits here, before the config is read or the serial device is
    # touched: a mistyped trial bound must never reach the vehicle.
    if args.poll_interval_s is not None and args.poll_interval_s <= 0:
        parser.error("--poll-interval-s must be greater than zero")
    if args.max_cycles is not None and args.max_cycles < 0:
        parser.error("--max-cycles must not be negative")
    if args.duration_s is not None and args.duration_s < 0:
        parser.error("--duration-s must not be negative")
    cfg = load_config(args.config, root=args.root) if args.config else load_config(root=args.root)
    if not cfg.collector.enabled and not args.force:
        print("collector.enabled is false in the configuration; refusing to start "
              "(use --force for a supervised manual run)", file=sys.stderr)
        return 1
    collector = Collector(
        cfg,
        once=args.once,
        max_cycles=args.max_cycles,
        duration_s=args.duration_s,
        poll_interval_s=args.poll_interval_s,
    )
    signal.signal(signal.SIGTERM, collector.stop)
    signal.signal(signal.SIGINT, collector.stop)
    return collector.run()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
