"""Long-running read-only collector.

Not enabled by default and deliberately dull:

* it polls a configured list of service 01 PIDs,
* it re-reads DTCs (services 03/07/0A) on a slow timer,
* every byte still lands in the append-only raw log first,
* decoded values go to SQLite, which is the local buffer — nothing is
  uploaded unless upload is explicitly enabled *and* an endpoint is set,
* when the vehicle stops answering (asleep), it backs off instead of
  hammering the bus, and reconnects with capped exponential backoff when the
  RFCOMM link drops.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, load_config
from .rawlog import RawLog
from .safety import UnsafeCommandError, validate_command
from .session import AdapterSession
from .storage import Storage
from .transport import SerialTransport, TransportError

__all__ = ["Collector", "main"]


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class Collector:
    def __init__(self, cfg: Config, *, once: bool = False, logger=print) -> None:
        self.cfg = cfg
        self.once = once
        self.log = logger
        self.running = True
        self.session_uid = f"collect-{_stamp()}"
        # Validate the configured PID list before opening anything at all: a
        # typo in the config must fail loudly, not reach the bus.  Entries are
        # full service 01 requests ("010C"), never bare PIDs, so a stray "04"
        # in the list is an error rather than a silently reinterpreted PID.
        self.commands = [self._as_service01(p) for p in cfg.collector.pids]
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

    # -- main loop -------------------------------------------------------
    def run(self) -> int:
        raw_path = Path(self.cfg.path(self.cfg.collector.raw_log_dir)) / f"{self.session_uid}.jsonl"
        db_path = self.cfg.path(self.cfg.collector.database)
        errors = 0
        with RawLog(raw_path, self.session_uid, meta={"role": "collector"}) as rawlog, \
                Storage(db_path) as store:
            sid = store.start_session(self.session_uid, raw_log_path=str(raw_path), notes="collector")
            transport = SerialTransport(
                self.cfg.adapter.device,
                rawlog,
                baudrate=self.cfg.adapter.baudrate,
                read_timeout_s=self.cfg.adapter.read_timeout_s,
                command_timeout_s=self.cfg.adapter.command_timeout_s,
                reconnect_initial_s=self.cfg.adapter.reconnect_initial_s,
                reconnect_max_s=self.cfg.adapter.reconnect_max_s,
            )
            session = AdapterSession(transport, logger=self.log)
            next_dtc = 0.0
            attempt = 0
            while self.running:
                try:
                    if not transport.is_open:
                        transport.open()
                        fp = session.initialize()
                        session.negotiate_protocol()
                        store.update_session(sid, adapter_id=fp.adapter_id, protocol=fp.protocol)
                        store.add_event("connected", fp.protocol, session_id=sid)
                        attempt = 0
                    cycle_had_data = False
                    for command in self.commands:
                        if not self.running:
                            break
                        pid = command[2:4]
                        value, reply = session.read_pid(pid)
                        store.add_sample(sid, value)
                        cycle_had_data = cycle_had_data or value.status == "ok"
                    now = time.monotonic()
                    if self.cfg.collector.dtc_interval_s and now >= next_dtc:
                        for mode in ("03", "07", "0A"):
                            codes, reply = session.read_dtcs(mode)
                            store.add_dtc_read(sid, mode, codes, " ".join(f.hex() for f in reply.frames))
                        next_dtc = now + self.cfg.collector.dtc_interval_s
                    errors = 0
                    if self.once:
                        break
                    if cycle_had_data:
                        time.sleep(self.cfg.collector.poll_interval_s)
                    else:
                        # The vehicle is most likely asleep.  Back off; never
                        # escalate to more aggressive probing.
                        store.add_event("idle_backoff", "no data this cycle", session_id=sid)
                        time.sleep(self.cfg.collector.idle_backoff_s)
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
            transport.close()
            store.end_session(sid)
        return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only OBD collector")
    parser.add_argument("--config", help="path to hummer.toml")
    parser.add_argument("--root", default=".", help="project root for relative paths")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument("--force", action="store_true",
                        help="run even when collector.enabled is false in the config")
    args = parser.parse_args(argv)
    cfg = load_config(args.config, root=args.root) if args.config else load_config(root=args.root)
    if not cfg.collector.enabled and not args.force:
        print("collector.enabled is false in the configuration; refusing to start "
              "(use --force for a supervised manual run)", file=sys.stderr)
        return 1
    collector = Collector(cfg, once=args.once)
    signal.signal(signal.SIGTERM, collector.stop)
    signal.signal(signal.SIGINT, collector.stop)
    return collector.run()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
