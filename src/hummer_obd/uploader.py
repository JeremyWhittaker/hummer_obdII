"""Optional, disabled-by-default uploader.

The Pi is the system of record: samples live in SQLite and stay there.  This
module exists so that turning on upload later is a configuration change rather
than a rewrite, and so the "off" state is explicit and tested.

Rules:

* it refuses to do anything unless ``[upload] enabled = true`` *and* an
  endpoint is set,
* it never deletes local data — it only stamps ``uploaded_at``, so the local
  buffer remains the full history,
* a failed batch leaves the rows unstamped, so nothing is lost,
* it uploads decoded samples only; raw transcripts never leave the Pi.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

from .config import Config
from .storage import Storage

__all__ = ["Uploader", "UploadDisabled", "UploadError"]


class UploadDisabled(RuntimeError):
    """Raised when an upload is attempted while upload is switched off."""


class UploadError(RuntimeError):
    """Raised when the endpoint rejects or fails a batch."""


@dataclass
class UploadResult:
    sent: int = 0
    batches: int = 0
    remaining: int = 0


def _post(endpoint: str, payload: dict, timeout: float = 30.0) -> int:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return int(response.status)


class Uploader:
    def __init__(self, cfg: Config, storage: Storage, *, sender: Optional[Callable] = None) -> None:
        self.cfg = cfg
        self.storage = storage
        self._send = sender or _post

    def enabled(self) -> bool:
        return bool(self.cfg.upload.enabled and self.cfg.upload.endpoint)

    def run_once(self) -> UploadResult:
        """Upload at most one batch.  Raises if upload is not switched on."""
        if not self.enabled():
            raise UploadDisabled(
                "upload is disabled: set [upload] enabled = true and an endpoint "
                "in the configuration to turn it on"
            )
        rows = self.storage.pending_samples(limit=self.cfg.upload.batch_size)
        if not rows:
            return UploadResult(sent=0, batches=0, remaining=0)
        payload = {
            "schema": "hummer-obd/sample-batch/1",
            "samples": [
                {
                    "ts": row["ts"],
                    "pid": row["pid"],
                    "name": row["name"],
                    "value": row["value"],
                    "unit": row["unit"],
                    "status": row["status"],
                    "raw_hex": row["raw_hex"],
                }
                for row in rows
            ],
        }
        try:
            status = self._send(self.cfg.upload.endpoint, payload)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise UploadError(f"upload failed, rows left in the local buffer: {exc}") from exc
        if not 200 <= int(status) < 300:
            raise UploadError(f"endpoint returned HTTP {status}; rows left in the local buffer")
        self.storage.mark_uploaded([row["id"] for row in rows])
        return UploadResult(sent=len(rows), batches=1, remaining=self.storage.pending_count())
