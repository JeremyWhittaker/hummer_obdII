"""Parked, person-started service-22 discovery. See docs/DEEP_SCAN.md.

This module is never imported by a collector. A hit is an unidentified payload,
not a decoded signal. No result changes the recorder's allowlist or decoders.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .decode import decode_dtcs, negative_response_name, parse_reply
from .drive import GROUPS, AddressGroup
from .enhanced import candidate_scalings, candidate_triples
from .rawlog import RawLog
from .safety import UnsafeCommandError, validate_command, validate_scan_command
from .transport import SerialTransport, TransportError

MODULES = ("17", "1D", "1E", "40", "28", "CB", "45", "CD")
GUARD_READS = frozenset({"010D", "03", "07", "0A"})
INIT = ("ATZ", "ATE0", "ATL0", "ATS0", "ATH1", "ATAL", "ATSP7", "ATST96")
NOTES = {0x22, 0x33, 0x34, 0x7E, 0x7F}
IDENTITY = ("ELM327", "OBDLINK", "STN")
assert INIT[0] == "ATZ", "startup synchronization assumes the reset comes first"


class ScanAborted(RuntimeError):
    """No further discovery traffic is permitted during this run."""


@dataclass(frozen=True)
class ScanConfig:
    module: str = "17"
    priority: str = "14"
    start: int = 0x2400
    end: int = 0x24FF
    device: str = "/dev/rfcomm0"
    delay_ms: float = 75.0
    timeout: float = 3.0
    chunk_size: int = 16
    output_dir: Path = Path("evidence/scans")
    # The first exchange after the tty opens has measured 4.2-4.4 s on this
    # link (DEEP_SCAN.md section 8), so the reset gets its own budget.
    startup_timeout: float = 10.0
    # Receive-only quiet window after the reset reply; a delayed second
    # identity banner arrived 0.83-0.91 s after the first.
    settle_s: float = 1.5

    def __post_init__(self):
        if self.module not in MODULES or self.priority not in {"14", "18"}:
            raise ValueError("select a census module and priority 14 or 18")
        if (type(self.start) is not int or type(self.end) is not int
                or not 0 <= self.start <= self.end <= 0xFFFF):
            raise ValueError("identifier range must satisfy 0000 <= start <= end <= FFFF")
        if not math.isfinite(self.delay_ms) or not 50 <= self.delay_ms <= 2000:
            raise ValueError("delay must be finite and between 50 and 2000 ms")
        if not math.isfinite(self.timeout) or not 0.1 <= self.timeout <= 10:
            raise ValueError("timeout must be finite and between 0.1 and 10 seconds")
        if type(self.chunk_size) is not int or not 1 <= self.chunk_size <= 32:
            raise ValueError("DTC chunk size must be between 1 and 32 identifiers")
        if not math.isfinite(self.startup_timeout) or not 0.1 <= self.startup_timeout <= 30:
            raise ValueError("startup timeout must be finite and between 0.1 and 30 seconds")
        if not math.isfinite(self.settle_s) or not 0.05 <= self.settle_s <= 5:
            raise ValueError("settle window must be finite and between 0.05 and 5 seconds")


def address_group(module: str, priority: str) -> AddressGroup:
    """Reuse the recorder's exact groups; extend its proven addressing template.

    Priority 14 replies use 142AF1xx, NOT 14DAF1xx. Priority 18 uses 18DAF1xx.
    Header precedes flow-control header/data/mode in every group.
    """
    if module not in MODULES or priority not in {"14", "18"}:
        raise ValueError("unknown module or priority")
    for group in GROUPS:
        if group.ecu == module and group.priority == f"ATCP{priority}":
            return replace(group, dids=())
    reply = f"142AF1{module}" if priority == "14" else f"18DAF1{module}"
    return AddressGroup(
        name=f"scan_{module}", ecu=module, dids=(), priority=f"ATCP{priority}",
        address=(f"ATSHDA{module}F1", f"ATCRA{reply}",
                 f"ATFCSH{priority}DA{module}F1", "ATFCSD300000", "ATFCSM1"),
    )


class ScanTransport(SerialTransport):
    """One open port, two non-overlapping gates, serialized even across threads.

    Normal send() always uses validate_scan_command. Only send_guard() can use
    the ordinary gate, and only for the four exact mandatory safety reads.
    The temporary gate change is private, locked, and restored on every exit.
    Neither existing validator nor the default SerialTransport is widened.
    """

    def __init__(self, device, rawlog, **kwargs):
        super().__init__(device, rawlog, validator=validate_scan_command, **kwargs)
        self._send_lock = threading.RLock()

    def open(self):
        super().open()
        try:
            # Prevent two scanners sharing a tty even with different output
            # directories. The existing recorder must ALSO be stopped manually;
            # flock cannot exclude a program that does not take this lock.
            fcntl.flock(self._serial.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, AttributeError) as exc:
            self.close()
            raise TransportError("cannot exclusively lock the scan port") from exc

    def send(self, command, timeout=None):
        with self._send_lock:
            self._refuse_unsolicited(command)
            return super().send(command, timeout)

    def send_guard(self, command, timeout=None):
        safe = validate_command(command)
        if safe not in GUARD_READS:
            raise UnsafeCommandError("scan guard permits only 010D, 03, 07, 0A")
        with self._send_lock:
            self._refuse_unsolicited(safe)
            previous = self._validator
            self._validator = validate_command
            try:
                return super().send(safe, timeout)
            finally:
                self._validator = previous

    def _refuse_unsolicited(self, command):
        """Log bytes nobody asked for, then refuse to send after them.

        SerialTransport.send() clears its input buffer before writing, which
        is right for the recorder but would let a late reply vanish unlogged
        here and the next reply be read as the answer to the wrong command.
        Only the reset may follow them: ATZ is what resynchronizes.
        """
        waiting = getattr(self._serial, "in_waiting", 0) or 0
        if not waiting:
            return
        stray = self._serial.read(waiting)
        self.rawlog.log_rx(stray, note=f"unsolicited bytes before {command}")
        if str(command).strip().upper() != "ATZ":
            raise ScanAborted("unsolicited adapter output before a command; "
                              "replies would be misattributed")

    def observe(self, quiet_s, max_s, note):
        """Receive, never transmit, until the line is quiet for *quiet_s*.

        Every byte is logged before it is interpreted, as send() does.
        Returns the bytes and whether the line went quiet within *max_s*.
        """
        with self._send_lock:
            if not self.is_open:
                raise TransportError("transport is not open")
            buffer = bytearray()
            started = last = time.monotonic()
            quiet = False
            while True:
                now = time.monotonic()
                if now - last >= quiet_s:
                    quiet = True
                    break
                if now - started >= max_s:
                    break
                try:
                    chunk = self._serial.read(1)
                    waiting = getattr(self._serial, "in_waiting", 0) or 0
                    if chunk and waiting:
                        chunk += self._serial.read(waiting)
                except Exception as exc:
                    self.rawlog.log_rx(bytes(buffer), note=f"{note}: partial before read error")
                    raise TransportError(f"read failed during {note}: {exc}") from exc
                if chunk:
                    buffer.extend(chunk)
                    last = time.monotonic()
            self.rawlog.log_rx(bytes(buffer), note=note if quiet else f"{note} (still receiving)")
            return bytes(buffer), quiet


def _reset_banner(data: bytes) -> bool:
    """Exactly one adapter identity line, optionally after the ATZ echo, then one prompt."""
    if data.count(b">") != 1 or not data.rstrip().endswith(b">"):
        return False
    lines = [line for line in parse_reply(data).lines if line.upper() != "ATZ"]
    return len(lines) == 1 and lines[0].upper().startswith(IDENTITY)


def _messages(response, header: str):
    """Require complete, attributable classical CAN, then use the shared parser.

    parse_reply deliberately tolerates unrelated text and incomplete streams
    for offline reporting. A safety guard cannot: validate every wire line and
    ISO-TP sequence before accepting the parser's reassembled payloads.
    """
    if response.timed_out:
        raise ScanAborted("adapter timeout; reply was not completed")
    try:
        raw = response.data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ScanAborted("non-ASCII adapter reply") from exc
    if raw.count(">") != 1 or not raw.rstrip().endswith(">"):
        raise ScanAborted("missing or unexpected adapter prompt")
    lines = [line.strip() for line in raw.replace(">", "").replace("\r", "\n").splitlines()
             if line.strip()]
    remaining = 0
    sequence = 1
    for line in lines:
        compact = "".join(line.split()).upper()
        if not re.fullmatch(r"[0-9A-F]+", compact) or len(compact) % 2:
            raise ScanAborted("unexpected adapter text (error, silence, or malformed reply)")
        if not compact.startswith(header) or len(compact) < 12:
            raise ScanAborted("reply from wrong module/address or without a CAN header")
        body = bytes.fromhex(compact[8:])
        if not 2 <= len(body) <= 8:
            raise ScanAborted("invalid classical CAN frame length")
        kind = body[0] >> 4
        if kind == 0:
            if remaining or not 1 <= body[0] <= 7 or len(body) < body[0] + 1:
                raise ScanAborted("truncated or interleaved ISO-TP single frame")
        elif kind == 1:
            total = ((body[0] & 15) << 8) | body[1]
            if remaining or total < 8 or len(body) != 8:
                raise ScanAborted("invalid ISO-TP first frame")
            remaining = total - (len(body) - 2)
            sequence = 1
        elif kind == 2:
            if remaining <= 0 or body[0] != 0x20 | sequence:
                raise ScanAborted("orphan or out-of-order ISO-TP consecutive frame")
            if remaining > 7 and len(body) != 8:
                raise ScanAborted("short intermediate ISO-TP frame")
            remaining = max(0, remaining - (len(body) - 1))
            sequence = (sequence + 1) & 15
        else:
            raise ScanAborted("unexpected ISO-TP frame type")
    reply = parse_reply(response.data)
    if remaining or reply.incomplete or not reply.frames:
        raise ScanAborted("silent module or incomplete diagnostic reply")
    messages = []
    for frame, source in zip(reply.frames, reply.frame_headers):
        if source != header:
            raise ScanAborted("parsed reply has no matching module attribution")
        messages.append(frame[1:1 + frame[0]] if frame and frame[0] < 0x10 else frame)
    if not messages or len(messages) != len(reply.frames):
        raise ScanAborted("missing response attribution")
    return reply, messages


def _classify(response, did: int, header: str) -> dict:
    _, messages = _messages(response, header)
    pending = 0
    while messages and messages[0] == b"\x7f\x22\x78":
        pending += 1
        messages = messages[1:]
    if pending > 3 or len(messages) != 1:
        raise ScanAborted("pending storm or no single terminal response; no retry")
    message = messages[0]
    prefix = b"\x62" + did.to_bytes(2, "big")
    if message.startswith(prefix) and len(message) > 3:
        payload = message[3:]
        return {"did": f"{did:04X}", "status": "hit", "validated": False,
                "payload_hex": payload.hex().upper(), "payload_len": len(payload),
                "candidate_scalings": candidate_scalings(payload),
                "candidate_triples": candidate_triples(payload), "pending_count": pending}
    if len(message) == 3 and message[:2] == b"\x7f\x22":
        code = message[2]
        if code in NOTES | {0x11, 0x31}:
            status = "unsupported" if code == 0x11 else "absent" if code == 0x31 else "restricted"
            return {"did": f"{did:04X}", "status": status, "nrc": f"{code:02X}",
                    "name": negative_response_name(code), "validated": False,
                    "pending_count": pending}
        raise ScanAborted(f"unexpected NRC {code:02X} ({negative_response_name(code)}); no retry")
    raise ScanAborted("wrong service, wrong DID echo, or empty positive payload")


class _Runner:
    def __init__(self, transport, config, rawlog, sleeper):
        self.transport = transport
        self.config = config
        self.rawlog = rawlog
        self.sleeper = sleeper
        self.sent = False

    def send(self, command, *, guard=False, timeout=None):
        if self.sent:
            self.sleeper(self.config.delay_ms / 1000)
        self.sent = True
        method = self.transport.send_guard if guard else self.transport.send
        return method(command, timeout=timeout or self.config.timeout)

    def startup(self):
        """Reset the adapter and let the link settle before trusting replies.

        Measured on this link: the first exchange after the tty opens took
        4.2-4.4 s and returned an identity banner without the command's echo,
        and a second banner followed ~0.9 s later -- which, read as the reply
        to ATE0, aborted the run. So the reset gets its own budget and the
        line is then observed, never written, until quiet. Silence or exactly
        one more identity banner is accepted; anything else stops the run.
        Every later reply keeps its strict check.
        """
        response = self.send("ATZ", timeout=self.config.startup_timeout)
        if response.timed_out:
            raise ScanAborted("adapter setup timed out")
        if not _reset_banner(response.data):
            raise ScanAborted("adapter rejected setup command ATZ")
        extra, quiet = self.transport.observe(
            self.config.settle_s, self.config.startup_timeout, "post-reset settle")
        if not quiet:
            raise ScanAborted("adapter output did not settle after reset")
        if extra and not _reset_banner(extra):
            raise ScanAborted("unexpected adapter output after reset")
        for command in INIT[1:]:
            self.adapter(command)

    def adapter(self, command):
        response = self.send(command)
        if response.timed_out:
            raise ScanAborted("adapter setup timed out")
        reply = parse_reply(response.data)
        lines = [line for line in reply.lines if line.upper() != command]
        if lines != ["OK"] or not response.data.rstrip().endswith(b">"):
            raise ScanAborted(f"adapter rejected setup command {command}")

    def address(self, group):
        for command in (group.priority, *group.address):
            self.adapter(command)

    def speed(self):
        self.address(address_group("17", "18"))
        response = self.send("010D", guard=True)
        _, messages = _messages(response, "18DAF117")
        if len(messages) != 1 or len(messages[0]) != 3 or messages[0][:2] != b"\x41\x0d":
            raise ScanAborted("speed unavailable or malformed; parked state not established")
        speed = messages[0][2]
        self.rawlog.write_event("speed_check", {"speed_kph": speed})
        if speed != 0:
            raise ScanAborted(f"vehicle moving ({speed} km/h); parked-only scan stopped")

    def dtcs(self, stage):
        self.address(address_group("45", "18"))
        for mode in ("03", "07", "0A"):
            response = self.send(mode, guard=True)
            reply, messages = _messages(response, "18DAF145")
            if (len(messages) != 1 or len(messages[0]) < 2
                    or messages[0][0] != int(mode, 16) + 0x40):
                raise ScanAborted(f"DTC service {mode} unavailable or malformed")
            payload = messages[0]
            count = payload[1]
            if len(payload) < 2 + 2 * count:
                raise ScanAborted(f"DTC service {mode} truncated; cannot prove clean")
            codes = decode_dtcs(mode, reply)
            self.rawlog.write_event("dtc_check", {"stage": stage, "mode": mode,
                                                "count": count, "codes": codes})
            if count or any(payload[2:]) or codes:
                raise ScanAborted(f"DTC detected by service {mode}: {', '.join(codes) or 'nonzero count/data'}")


def _directory(config: ScanConfig) -> Path:
    """Evidence never goes outside cwd/evidence/scans, even via symlinks."""
    path = Path(os.path.abspath(config.output_dir))
    root = Path.cwd() / "evidence" / "scans"
    if not path.is_relative_to(root):
        raise ValueError("output directory must be under evidence/scans/ in the current checkout")
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("symlinks are not allowed in scan output paths")
    return path


def _paths(config):
    directory = _directory(config)
    stem = f"module-{config.module}-priority-{config.priority}-{config.start:04X}-{config.end:04X}"
    return directory, directory / f"{stem}.state.json", directory / f"{stem}.raw.jsonl"


def _identity(config):
    return {"schema": 1, "module": config.module, "priority": config.priority,
            "start": config.start, "end": config.end}


def _load_state(config, path, resume):
    if path.is_symlink():
        raise ValueError("state may not be a symlink")
    if not path.exists():
        if resume:
            raise ValueError("no matching resume state for this module, priority and range")
        return {**_identity(config), "next_did": config.start, "last_did": None,
                "status": "new", "hits": 0, "completed_reads": 0}
    if not resume:
        raise ValueError("state already exists; inspect it and use --resume")
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or any(state.get(k) != v for k, v in _identity(config).items()):
        raise ValueError("resume state identity mismatch")
    next_did = state.get("next_did")
    if type(next_did) is not int or not config.start <= next_did <= config.end + 1:
        raise ValueError("invalid resume cursor")
    last = None if next_did == config.start else next_did - 1
    if state.get("last_did") != last:
        raise ValueError("inconsistent resume cursor")
    if state.get("status") not in {"new", "running", "aborted", "complete", "unsupported"}:
        raise ValueError("invalid resume status")
    for key in ("hits", "completed_reads"):
        if type(state.get(key)) is not int or state[key] < 0:
            raise ValueError("invalid resume counters")
    if state["completed_reads"] != next_did - config.start or state["hits"] > state["completed_reads"]:
        raise ValueError("inconsistent resume counters")
    if state["status"] == "complete" and next_did != config.end + 1:
        raise ValueError("incomplete range marked complete")
    stopped = state.get("service_unsupported_at")
    if stopped is not None and (type(stopped) is not int or stopped != last):
        raise ValueError("invalid service-not-supported checkpoint")
    if state["status"] == "unsupported" and stopped is None:
        raise ValueError("unsupported state has no module-stop checkpoint")
    return state


def _save_state(path, state):
    # Same-directory atomic replacement + file/directory fsync. Never touch
    # the append-only raw transcript. An ambiguous crash may repeat ONE DID.
    fd, temporary = tempfile.mkstemp(prefix=".scan-state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _output_lock(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
    fd = os.open(directory / ".scan.lock", flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError as exc:
        raise ValueError("another scan owns this output directory") from exc
    finally:
        os.close(fd)


def run_scan(config: ScanConfig, *, resume=False, say=print,
             sleeper: Callable[[float], None] = time.sleep) -> dict:
    """Transmit an explicitly requested bounded scan; CLI opt-in is in main().

    Safety reads are mandatory on every new invocation, including resume.
    A terminal, already-bracketed range opens no port on resume. After unsafe
    aborts no more traffic is sent, including a postflight: that omission is
    recorded rather than reporting a fictitious clean final DTC check.
    """
    directory, state_path, raw_path = _paths(config)
    with _output_lock(directory):
        state = _load_state(config, state_path, resume)
        if raw_path.is_symlink():
            raise ValueError("raw log may not be a symlink")
        if resume and not raw_path.is_file():
            raise ValueError("resume raw transcript is missing")
        if not resume and raw_path.exists():
            raise ValueError("raw transcript already exists without matching new state")
        if state["status"] in {"complete", "unsupported"}:
            say(f"Already {state['status']}; no device opened.")
            return state
        _save_state(state_path, state)
        # RawLog uses append mode; create privately without following symlinks.
        fd = os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with RawLog(raw_path, f"scan-{uuid4().hex}", meta=_identity(config)) as log:
            try:
                with ScanTransport(config.device, log, read_timeout_s=min(0.1, config.timeout),
                                   command_timeout_s=config.timeout) as transport:
                    runner = _Runner(transport, config, log, sleeper)
                    runner.startup()
                    runner.speed()
                    runner.dtcs("before")
                    group = address_group(config.module, config.priority)
                    header = next(c[5:] for c in group.address if c.startswith("ATCRA"))
                    state["status"] = "running"
                    state.pop("reason", None)
                    state.pop("postflight", None)
                    # If NRC11 was durable but its postflight failed, a resume
                    # may re-check guards, never probe more of that module.
                    end = config.end + 1 if state.get("service_unsupported_at") is None else state["next_did"]
                    for did in range(state["next_did"], end):
                        runner.speed()
                        runner.address(group)
                        record = _classify(runner.send(f"22{did:04X}"), did, header)
                        # This fsync MUST precede the resume cursor. Raw bytes
                        # are already durable via SerialTransport.send().
                        log.write_event("did_result", record)
                        state["last_did"] = did
                        state["next_did"] = did + 1
                        state["completed_reads"] += 1
                        state["hits"] += record["status"] == "hit"
                        if record["status"] == "unsupported":
                            state["service_unsupported_at"] = did
                        _save_state(state_path, state)
                        say(f"{config.module}/{config.priority} {did:04X}: {record['status']}"
                            + (" (unvalidated)" if record["status"] == "hit" else ""))
                        if record["status"] == "unsupported":
                            runner.speed()
                            runner.dtcs("after")
                            state["status"] = "unsupported"
                            break
                        if state["completed_reads"] % config.chunk_size == 0:
                            runner.speed()
                            runner.dtcs("chunk")
                    else:
                        runner.speed()
                        runner.dtcs("after")
                        state["status"] = "complete" if state.get("service_unsupported_at") is None else "unsupported"
                    log.write_event("scan_finished", state)
                    _save_state(state_path, state)
                    return state
            except (ScanAborted, TransportError, OSError, KeyboardInterrupt) as exc:
                state["status"] = "aborted"
                state["reason"] = "operator interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc)
                state["postflight"] = "not completed; no further traffic after unsafe stop"
                log.write_event("scan_aborted", state)
                _save_state(state_path, state)
                raise ScanAborted(state["reason"]) from exc


def _hex_arg(text):
    if not re.fullmatch(r"(?:0[xX])?[0-9a-fA-F]{1,4}", text):
        raise argparse.ArgumentTypeError("expected a hexadecimal identifier (0000..FFFF)")
    return int(text, 16)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", type=str.upper, choices=MODULES, default="17")
    parser.add_argument("--priority", type=lambda x: x.upper().removeprefix("0X"), choices=("14", "18"))
    parser.add_argument("--start", type=_hex_arg, default=0x2400)
    parser.add_argument("--end", type=_hex_arg, default=0x24FF)
    parser.add_argument("--device", default="/dev/rfcomm0")
    parser.add_argument("--delay-ms", type=float, default=75)
    parser.add_argument("--timeout", type=float, default=3)
    parser.add_argument("--chunk-size", type=int, default=16)
    parser.add_argument("--startup-timeout", type=float, default=10,
                        help="budget for the adapter reset reply (0.1-30 s)")
    parser.add_argument("--settle", type=float, default=1.5,
                        help="receive-only quiet window after the reset (0.05-5 s)")
    parser.add_argument("--output-dir", type=Path, default=Path("evidence/scans"))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--confirm", action="store_true", help="transmit; parked, plugged in, attended only")
    args = parser.parse_args(argv)
    try:
        config = ScanConfig(module=args.module,
                            priority=args.priority or ("18" if args.module in {"40", "45"} else "14"),
                            start=args.start, end=args.end, device=args.device,
                            delay_ms=args.delay_ms, timeout=args.timeout,
                            chunk_size=args.chunk_size, output_dir=args.output_dir,
                            startup_timeout=args.startup_timeout, settle_s=args.settle)
        directory, state_path, _ = _paths(config)
        if not args.confirm:
            state = _load_state(config, state_path, True) if args.resume else None
            group = address_group(config.module, config.priority)
            print("DRY RUN: no device opened, no files written, nothing transmitted.")
            print(f"Module {config.module}, priority {config.priority}, range {config.start:04X}-{config.end:04X}")
            print("Addressing: " + " ".join((group.priority, *group.address)))
            print(f"Reads: 22XXXX only; {config.end - config.start + 1} identifiers; delay {config.delay_ms:g} ms")
            print("Guards: 010D from 17/18 before every DID; 03/07/0A from 45/18 before/chunk/after.")
            print(f"Startup: ATZ budget {config.startup_timeout:g} s, then {config.settle_s:g} s "
                  "receive-only settle; one late identity banner tolerated, nothing else.")
            print(f"DTC chunk: {config.chunk_size}; private output: {directory}")
            if state:
                print(f"Resume cursor: {state['next_did']:04X}; status: {state['status']}")
            print("Stop the recorder first. Keep parked, plugged in and attended. Add --confirm to transmit.")
            return 0
        state = run_scan(config, resume=args.resume)
        print(f"Scan {state['status']}: {state['completed_reads']} reads, {state['hits']} unvalidated hits.")
        return 0
    except (ValueError, UnsafeCommandError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except (ScanAborted, TransportError, OSError) as exc:
        print(f"STOPPED: {exc}. Recorder restart is the operator's responsibility.", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
