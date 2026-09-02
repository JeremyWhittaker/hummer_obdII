"""Serial transport to the OBDLink adapter over an RFCOMM tty.

Responsibilities:

* refuse to transmit anything the safety gate has not approved,
* write every transmitted and received byte to the append-only raw log
  *before* any parsing happens,
* read until the adapter's ``>`` prompt with a bounded timeout,
* reconnect with capped exponential backoff when the link drops (the vehicle
  sleeping, the adapter powering down, or Bluetooth dropping out).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from .rawlog import RawLog
from .safety import describe_command, validate_command

__all__ = ["Transport", "TransportError", "SerialTransport", "PROMPT"]

PROMPT = b">"


class TransportError(RuntimeError):
    """Raised when the adapter link fails in a way the caller must handle."""


@dataclass
class Response:
    command: str
    data: bytes
    elapsed_s: float
    timed_out: bool = False


class Transport:
    """Interface implemented by the real serial transport and by fakes."""

    def open(self) -> None: ...
    def close(self) -> None: ...
    def send(self, command: str, timeout: Optional[float] = None) -> Response: ...


class SerialTransport(Transport):
    def __init__(
        self,
        device: str,
        rawlog: RawLog,
        *,
        baudrate: int = 115200,
        read_timeout_s: float = 2.0,
        command_timeout_s: float = 5.0,
        reconnect_initial_s: float = 2.0,
        reconnect_max_s: float = 120.0,
        serial_module=None,
        sleeper: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.device = device
        self.rawlog = rawlog
        self.baudrate = baudrate
        self.read_timeout_s = read_timeout_s
        self.command_timeout_s = command_timeout_s
        self.reconnect_initial_s = reconnect_initial_s
        self.reconnect_max_s = reconnect_max_s
        self._serial = None
        self._backoff = reconnect_initial_s
        #: How the reconnect backoff waits.  A long-running caller injects its
        #: own waiter so a 120 s backoff cannot outlive a bounded run or
        #: swallow a SIGTERM; ``None`` means plain ``time.sleep``, which is
        #: right for a one-shot.  Resolved at call time, not here, so the
        #: default really is "whatever time.sleep is now".
        self._sleeper = sleeper
        if serial_module is None:  # imported lazily so tests need no pyserial
            import serial as serial_module  # type: ignore
        self._serial_module = serial_module

    # -- link management -------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._serial is not None and getattr(self._serial, "is_open", False)

    def open(self) -> None:
        if self.is_open:
            return
        try:
            self._serial = self._serial_module.Serial(
                self.device,
                baudrate=self.baudrate,
                timeout=self.read_timeout_s,
                write_timeout=self.command_timeout_s,
            )
        except Exception as exc:  # pyserial raises several exception types
            self.rawlog.write_event("open_failed", {"device": self.device, "error": str(exc)})
            raise TransportError(f"cannot open {self.device}: {exc}") from exc
        self._backoff = self.reconnect_initial_s
        self.rawlog.write_event("open", {"device": self.device, "baudrate": self.baudrate})

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None
                self.rawlog.write_event("close", {"device": self.device})

    def reconnect(self, attempt: int = 0) -> None:
        """Close and reopen the link, waiting out the current backoff first.

        The wait goes through :attr:`_sleeper` rather than ``time.sleep``.  It
        is the third and longest sleep in a collector cycle -- up to
        ``reconnect_max_s`` -- so leaving it uninterruptible would let a
        time-boxed run overshoot by two minutes and would make a stop request
        look ignored for just as long.
        """
        self.close()
        delay = min(self._backoff, self.reconnect_max_s)
        self.rawlog.write_event("reconnect_wait", {"attempt": attempt, "delay_s": delay})
        (self._sleeper or time.sleep)(delay)
        self._backoff = min(self._backoff * 2, self.reconnect_max_s)
        self.open()

    # -- I/O -------------------------------------------------------------
    def send(self, command: str, timeout: Optional[float] = None) -> Response:
        """Validate, transmit and read one command.  Raw bytes are logged."""
        safe = validate_command(command)  # raises UnsafeCommandError
        if not self.is_open:
            raise TransportError("transport is not open")
        payload = (safe + "\r").encode("ascii")
        deadline = time.monotonic() + (timeout or self.command_timeout_s)

        try:
            self._serial.reset_input_buffer()
        except Exception:
            pass
        self.rawlog.log_tx(payload, note=describe_command(safe))
        started = time.monotonic()
        try:
            self._serial.write(payload)
            self._serial.flush()
        except Exception as exc:
            self.rawlog.write_event("write_failed", {"command": safe, "error": str(exc)})
            raise TransportError(f"write failed for {safe}: {exc}") from exc

        buffer = bytearray()
        timed_out = False
        while True:
            try:
                # Read one byte (blocking up to the read timeout), then drain
                # whatever else has already arrived.  Asking for a fixed block
                # size instead would stall for the whole read timeout on every
                # short reply, which is most of them.
                chunk = self._serial.read(1)
                waiting = getattr(self._serial, "in_waiting", 0) or 0
                if chunk and waiting:
                    chunk += self._serial.read(waiting)
            except Exception as exc:
                self.rawlog.log_rx(bytes(buffer), note="partial before read error")
                self.rawlog.write_event("read_failed", {"command": safe, "error": str(exc)})
                raise TransportError(f"read failed for {safe}: {exc}") from exc
            if chunk:
                buffer.extend(chunk)
                if PROMPT in chunk:
                    break
            if time.monotonic() > deadline:
                timed_out = True
                break
        elapsed = time.monotonic() - started
        self.rawlog.log_rx(
            bytes(buffer),
            note=f"reply to {safe}" + (" (timeout)" if timed_out else ""),
        )
        return Response(command=safe, data=bytes(buffer), elapsed_s=elapsed, timed_out=timed_out)

    # -- context manager -------------------------------------------------
    def __enter__(self) -> "SerialTransport":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
