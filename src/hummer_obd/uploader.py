"""Optional, disabled-by-default uploader.

The Pi is the system of record: samples live in SQLite and stay there.  This
module exists so that turning on upload later is a reviewed configuration
change rather than a rewrite, and so the "off" state is explicit and tested.

Rules:

* it refuses to do anything unless ``[upload] enabled = true`` *and* an
  endpoint is set,
* it refuses a plaintext endpoint unless ``require_https`` has been turned off
  deliberately for a local test,
* when a token file is configured it refuses to send without a token — an
  unreadable or emptied token file stops the upload, it never downgrades it to
  an anonymous POST,
* it never deletes local data — it only stamps ``uploaded_at``, so the local
  buffer remains the full history,
* a failed batch leaves the rows unstamped, so nothing is lost,
* it does not follow redirects: a 3xx would carry the bearer token to a
  different (possibly plaintext) host and turn the POST into a GET, so the
  final 200 would stamp rows for a batch nobody received,
* it uploads decoded samples only; raw transcripts never leave the Pi, and a
  VIN-shaped string anywhere in a batch aborts that batch.

Those refusals duplicate checks that ``UploadConfig.validate()`` already makes
at load time.  That is deliberate.  A ``Config`` can also be built in code,
tests and future callers do exactly that, and this module is the last thing
between the vehicle's data and the network — like ``safety.py``, it assumes
nothing upstream ran.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .config import Config, load_config, redacted_endpoint
from .decode import mask_vin
from .storage import Storage

__all__ = [
    "Uploader",
    "UploadDisabled",
    "UploadError",
    "UploadResult",
    "audit_payload",
    "UPLOADABLE_TABLES",
    "FORBIDDEN_PAYLOAD_KEYS",
    "main",
]

#: The only tables this module may read.  ``vehicle_info`` (VIN-derived rows)
#: and the raw JSONL transcripts are absent on purpose: there is no code path
#: here that opens them, and this tuple is the statement of that intent.
UPLOADABLE_TABLES = ("samples",)

#: Keys that must never appear in a payload at any depth.  ``raw_hex`` is
#: absent because it is a decoded sample field stored per row and is genuinely
#: useful downstream; the audit below still reads what its bytes spell.
FORBIDDEN_PAYLOAD_KEYS = frozenset({"raw_log", "raw_log_path", "transcript", "vin"})

# A VIN is 17 characters drawn from the alphabet minus I, O and Q.  The
# lookarounds anchor the match to a whole token, so a long unbroken hex dump is
# not mistaken for one.
_VIN_SHAPED = re.compile(r"(?<![A-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Z0-9])")


class UploadDisabled(RuntimeError):
    """Raised when an upload is attempted while upload is switched off."""


class UploadError(RuntimeError):
    """Raised when a batch is unsafe to send, or the endpoint rejects it."""


@dataclass
class UploadResult:
    sent: int = 0
    batches: int = 0
    remaining: int = 0
    #: True when the batch was built and audited but deliberately not sent.
    dry_run: bool = False
    #: The audited payload, returned only for a dry run so an operator can read
    #: exactly what would have left the Pi.
    payload: Optional[dict] = None


def _spelled_out(raw_hex: str) -> str:
    """Render frame hex as the text those bytes carry.

    ``raw_hex`` is a byte encoding, not prose, so scanning its literal digits
    tells you nothing.  A VIN can only ride inside a frame as ASCII (a service
    09 PID 02 reply), which means the scan has to look at the decoded bytes.
    Unprintable bytes become spaces, which also breaks up runs that are not
    text in the first place.  A string that is not valid hex is not frame data
    at all, so it is handed back unchanged and scanned as-is.

    This is **best-effort defence in depth, not a guarantee**.  A real
    multi-frame ISO-TP reply interleaves consecutive-frame sequence bytes
    (``0x21``, ``0x22``, ...) which are themselves printable, so they split the
    17-character run and a VIN spread across frames will not match.  Compacting
    those out before scanning would trade this gap for false positives that
    refuse ordinary batches, which is the worse failure direction.  The real
    control is structural: ``UPLOADABLE_TABLES`` is ``samples`` only, and
    service 09 VIN data is written to ``vehicle_info``, which nothing here
    reads.
    """
    try:
        data = bytes.fromhex(raw_hex.replace(" ", ""))
    except ValueError:
        return raw_hex
    return "".join(chr(b) if 32 <= b < 127 else " " for b in data)


def _refuse_if_vin_shaped(text: str, where: str) -> None:
    match = _VIN_SHAPED.search(text.upper())
    if match:
        raise UploadError(
            f"refusing to send: {where} holds a VIN-shaped string "
            f"({mask_vin(match.group(0))}); an unmasked VIN never leaves the Pi"
        )


def audit_payload(node: Any, where: str = "payload") -> None:
    """Walk a payload and refuse anything that must not leave the Pi.

    This runs on the assembled batch rather than on each row, because the
    interesting failure is a future contributor adding a field, not a decoder
    misbehaving on one sample.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{where}.{key}"
            if str(key).lower() in FORBIDDEN_PAYLOAD_KEYS:
                raise UploadError(
                    f"refusing to send: {child} is not an uploadable field; raw "
                    f"transcripts and VINs stay on the Pi (uploadable tables: "
                    f"{', '.join(UPLOADABLE_TABLES)})"
                )
            if key == "raw_hex" and isinstance(value, str):
                _refuse_if_vin_shaped(_spelled_out(value), child)
                continue
            audit_payload(value, child)
    elif isinstance(node, (list, tuple)):
        for index, item in enumerate(node):
            audit_payload(item, f"{where}[{index}]")
    elif isinstance(node, str):
        _refuse_if_vin_shaped(node, where)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow a redirect instead of chasing it.

    ``urlopen`` follows a 3xx silently, and that is wrong here twice over.  It
    copies every header except the content ones onto the new request, so the
    ``Authorization`` bearer token is handed to whatever host the ``Location``
    names — a third party, or a plaintext one, which walks straight past the
    ``require_https`` refusal above.  And it rewrites the POST as a GET, so the
    batch is dropped on the floor while the final 200 reads as a receipt and
    stamps the rows as uploaded.  Returning ``None`` here leaves the 3xx to be
    raised as an ``HTTPError``, which ``run_once`` turns into a refusal with
    the rows still pending.  A moved endpoint is a configuration change for a
    human to make, not something to follow unattended.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


#: Built once: the default opener follows redirects, this one does not.
_OPENER = urllib.request.build_opener(_NoRedirect)


def _post(
    endpoint: str,
    payload: dict,
    timeout: float = 30.0,
    headers: Optional[dict[str, str]] = None,
) -> int:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=dict(headers or {"Content-Type": "application/json"}),
        method="POST",
    )
    with _OPENER.open(request, timeout=timeout) as response:  # noqa: S310
        return int(response.status)


class Uploader:
    def __init__(self, cfg: Config, storage: Storage, *, sender: Optional[Callable] = None) -> None:
        self.cfg = cfg
        self.storage = storage
        self._send = sender or _post

    def enabled(self) -> bool:
        return bool(self.cfg.upload.enabled and self.cfg.upload.endpoint)

    # -- refusals --------------------------------------------------------
    def _require_enabled(self) -> None:
        upload = self.cfg.upload
        if not self.enabled():
            raise UploadDisabled(
                "upload is disabled: set [upload] enabled = true and an endpoint "
                "in the configuration to turn it on"
            )
        if upload.allow_raw_logs:
            raise UploadDisabled(
                "upload.allow_raw_logs is true, and there is no code path that "
                "uploads raw transcripts; set it back to false"
            )
        if upload.require_https and not upload.endpoint.lower().startswith("https://"):
            raise UploadDisabled(
                f"upload.endpoint {redacted_endpoint(upload.endpoint)} is not https, and vehicle "
                "telemetry is private data; set upload.require_https = false only "
                "to aim the Pi at a local test endpoint"
            )

    def _headers(self) -> dict[str, str]:
        """Build the request headers, reading the bearer token at send time.

        Reading late rather than in ``__init__`` means a rotated token is picked
        up by the next batch instead of at the next restart.  The token exists
        only inside the returned dict: it is never kept on the instance, never
        logged, and never named in an exception, because exception text ends up
        in journald and in operator screenshots.
        """
        headers = {"Content-Type": "application/json"}
        if not self.cfg.upload.token_file:
            return headers
        path = self.cfg.path(self.cfg.upload.token_file)
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise UploadDisabled(
                f"upload.token_file {path} could not be read ({exc.strerror}); "
                "refusing to fall back to an unauthenticated POST"
            ) from exc
        if not token:
            raise UploadDisabled(
                f"upload.token_file {path} is empty; refusing to fall back to an "
                "unauthenticated POST"
            )
        headers["Authorization"] = f"Bearer {token}"
        return headers

    # -- payload ---------------------------------------------------------
    def _build_payload(self, rows) -> dict:
        """Assemble one batch from ``samples`` and audit it before it is sent."""
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
        audit_payload(payload)
        return payload

    def preview(self) -> dict:
        """Return the exact payload a real run would POST, without sending it.

        Every refusal above still applies, the token file included, so a dry run
        also proves the credential is in place.  Nothing is sent and no row is
        stamped, and the token is not part of the payload.
        """
        self._require_enabled()
        self._headers()
        rows = self.storage.pending_samples(limit=self.cfg.upload.batch_size)
        return self._build_payload(rows)

    # -- main entry ------------------------------------------------------
    def run_once(self, *, dry_run: bool = False) -> UploadResult:
        """Upload at most one batch.  Raises if upload is not switched on."""
        self._require_enabled()
        headers = self._headers()
        rows = self.storage.pending_samples(limit=self.cfg.upload.batch_size)
        payload = self._build_payload(rows)
        if dry_run:
            return UploadResult(
                sent=0,
                batches=0,
                remaining=self.storage.pending_count(),
                dry_run=True,
                payload=payload,
            )
        if not rows:
            return UploadResult(sent=0, batches=0, remaining=0)
        try:
            status = self._send(
                self.cfg.upload.endpoint,
                payload,
                timeout=self.cfg.upload.timeout_s,
                headers=headers,
            )
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise UploadError(f"upload failed, rows left in the local buffer: {exc}") from exc
        # Rows are stamped only after a confirmed 2xx.  A redirect is not a
        # receipt either: the endpoint moved, and re-sending is cheap while a
        # false "uploaded" stamp is unrecoverable.
        if not 200 <= int(status) < 300:
            raise UploadError(f"endpoint returned HTTP {status}; rows left in the local buffer")
        self.storage.mark_uploaded([row["id"] for row in rows])
        return UploadResult(sent=len(rows), batches=1, remaining=self.storage.pending_count())


def main(argv=None) -> int:
    """Inspect, or run, one upload batch.  Dry run unless ``--send`` is given."""
    parser = argparse.ArgumentParser(
        description="Opt-in telemetry upload; prints the payload instead of sending it "
                    "unless --send is given"
    )
    parser.add_argument("--config", help="path to hummer.toml")
    parser.add_argument("--root", default=".", help="project root for relative paths")
    parser.add_argument("--send", action="store_true",
                        help="actually POST one batch and stamp the rows it confirms")
    args = parser.parse_args(argv)
    cfg = load_config(args.config, root=args.root) if args.config else load_config(root=args.root)
    db_path = cfg.path(cfg.collector.database)
    if not db_path.exists():
        # Opening it would create an empty one, which quietly hides the real
        # problem: the wrong --root, or a collector that has never run.
        print(f"no local buffer at {db_path}; nothing to upload", file=sys.stderr)
        return 1
    try:
        with Storage(db_path) as store:
            uploader = Uploader(cfg, store)
            if not args.send:
                result = uploader.run_once(dry_run=True)
                print(json.dumps(result.payload, indent=2, sort_keys=True))
                return 0
            result = uploader.run_once()
            print(f"uploaded {result.sent} sample(s); {result.remaining} still pending")
            return 0
    except UploadDisabled as exc:
        print(f"upload not attempted: {exc}", file=sys.stderr)
        return 1
    except UploadError as exc:
        print(f"upload refused or failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
