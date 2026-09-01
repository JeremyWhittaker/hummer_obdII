"""Append-only, byte-exact serial transcript logging.

Everything that goes to or comes from the adapter is written here *before* it
is parsed.  The log is the primary evidence artefact: parsing bugs must never
be able to destroy what the vehicle actually said.

Each record is one JSON object on one line with:

``seq``       monotonically increasing record number within the session
``ts``        wall-clock ISO-8601 timestamp (UTC)
``mono``      monotonic seconds since session start (ordering that survives NTP steps)
``dir``       ``tx`` or ``rx``
``b64``       the exact bytes, base64 encoded (lossless, canonical)
``hex``       the exact bytes as lowercase hex (lossless, human greppable)
``len``       byte count
``display``   a lossy, human-readable rendering; never parsed, never trusted
``note``      optional operator annotation

The file is opened in append mode, one ``write()`` per record, and flushed (and
optionally fsynced) immediately, so a power loss truncates at most the record
being written and never rewrites earlier bytes.
"""

from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

__all__ = ["RawLog", "render_display", "iter_records"]

_PRINTABLE = {0x09: "\\t", 0x0A: "\\n", 0x0D: "\\r"}


def render_display(data: bytes) -> str:
    """Return a lossy but readable rendering of *data*.

    This field exists purely so a human can skim a transcript.  It is never
    used to reconstruct bytes; ``b64``/``hex`` are the authoritative fields.
    """
    out = []
    for byte in data:
        if byte in _PRINTABLE:
            out.append(_PRINTABLE[byte])
        elif 0x20 <= byte <= 0x7E:
            out.append(chr(byte))
        else:
            out.append(f"\\x{byte:02x}")
    return "".join(out)


class RawLog:
    """Append-only raw transcript writer."""

    def __init__(
        self,
        path: os.PathLike[str] | str,
        session_id: str,
        *,
        fsync: bool = True,
        meta: Optional[dict] = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id
        self._fsync = fsync
        self._seq = 0
        self._t0 = time.monotonic()
        # Line buffered append; never truncate, never seek.
        self._fh = open(self.path, "a", encoding="utf-8", newline="\n")
        self.write_event("session_start", {"session_id": session_id, "meta": meta or {}})

    # -- writing ---------------------------------------------------------
    def _emit(self, record: dict) -> dict:
        self._seq += 1
        record["seq"] = self._seq
        record["ts"] = datetime.now(timezone.utc).isoformat()
        record["mono"] = round(time.monotonic() - self._t0, 6)
        record["session_id"] = self.session_id
        self._fh.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
        self._fh.flush()
        if self._fsync:
            os.fsync(self._fh.fileno())
        return record

    def log_bytes(self, direction: str, data: bytes, note: str = "") -> dict:
        """Record *data* exactly as it appeared on the wire."""
        if direction not in ("tx", "rx"):
            raise ValueError(f"direction must be 'tx' or 'rx', got {direction!r}")
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("raw log only accepts bytes")
        data = bytes(data)
        return self._emit(
            {
                "kind": "io",
                "dir": direction,
                "len": len(data),
                "b64": base64.b64encode(data).decode("ascii"),
                "hex": data.hex(),
                "display": render_display(data),
                "note": note,
            }
        )

    def log_tx(self, data: bytes, note: str = "") -> dict:
        return self.log_bytes("tx", data, note)

    def log_rx(self, data: bytes, note: str = "") -> dict:
        return self.log_bytes("rx", data, note)

    def write_event(self, event: str, payload: Optional[dict] = None) -> dict:
        """Record a non-wire event (session start, reconnect, operator note)."""
        return self._emit({"kind": "event", "event": event, "payload": payload or {}})

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        if not self._fh.closed:
            try:
                self.write_event("session_end")
            finally:
                self._fh.close()

    def __enter__(self) -> "RawLog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def iter_records(path: os.PathLike[str] | str):
    """Yield parsed records from a raw log, skipping nothing silently.

    A truncated final line (power loss during a write) is yielded as an
    ``{"kind": "corrupt"}`` record rather than being dropped.
    """
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                yield {"kind": "corrupt", "lineno": lineno, "raw": line}


def decode_record(record: dict) -> bytes:
    """Return the exact bytes for an ``io`` record, verifying hex == base64."""
    if record.get("kind") != "io":
        raise ValueError("not an io record")
    from_b64 = base64.b64decode(record["b64"])
    from_hex = bytes.fromhex(record["hex"])
    if from_b64 != from_hex:
        raise ValueError(f"raw log record {record.get('seq')} is internally inconsistent")
    return from_b64
