"""Configuration loading.

TOML via the standard library (Python 3.11+), so the Pi needs no extra
dependency.  Unknown keys are reported rather than ignored, and the upload
section is disabled by default and refuses to enable itself without an
explicit endpoint.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

__all__ = ["Config", "AdapterConfig", "CollectorConfig", "UploadConfig", "DisplayConfig", "load_config"]


@dataclass
class AdapterConfig:
    device: str = "/dev/rfcomm0"
    baudrate: int = 115200
    read_timeout_s: float = 2.0
    command_timeout_s: float = 5.0
    #: Seconds to wait between reconnect attempts (exponential, capped).
    reconnect_initial_s: float = 2.0
    reconnect_max_s: float = 120.0
    #: Adapter address, recorded for evidence only; never used to auto-pair.
    bluetooth_address: str = ""


@dataclass
class CollectorConfig:
    enabled: bool = False
    poll_interval_s: float = 2.0
    #: PIDs polled each cycle.  Every entry is validated by the safety gate.
    #: The PIDs this vehicle advertises and answers: speed, run time, control
    #: module voltage.  See config/hummer.example.toml for the evidence.
    pids: list[str] = field(default_factory=lambda: ["010D", "011F", "0142"])
    #: How often to re-read DTCs (service 03/07/0A), in seconds.  0 disables.
    dtc_interval_s: float = 900.0
    database: str = "data/hummer_obd.sqlite3"
    raw_log_dir: str = "logs/raw"
    #: Stop polling and idle when the vehicle stops answering (asleep).
    idle_backoff_s: float = 60.0
    max_consecutive_errors: int = 20


@dataclass
class UploadConfig:
    #: Disabled by default.  Nothing leaves the Pi unless this is set to true
    #: *and* an endpoint is configured.
    enabled: bool = False
    endpoint: str = ""
    batch_size: int = 200
    interval_s: float = 300.0

    def validate(self) -> None:
        if self.enabled and not self.endpoint:
            raise ValueError("upload.enabled is true but upload.endpoint is empty")


@dataclass
class DisplayConfig:
    enabled: bool = True
    #: Seconds between rendered updates.
    refresh_interval_s: float = 300.0
    #: Panel is put to sleep between updates to limit wear.
    sleep_between_updates: bool = True
    #: Render to this PNG instead of the panel (development/testing).
    simulate_path: str = ""
    rotate: int = 0


@dataclass
class Config:
    adapter: AdapterConfig = field(default_factory=AdapterConfig)
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    upload: UploadConfig = field(default_factory=UploadConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    root: Path = field(default_factory=lambda: Path("."))

    def path(self, relative: str) -> Path:
        """Resolve a config-relative path against the project root."""
        p = Path(relative)
        return p if p.is_absolute() else (self.root / p)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if k != "root"}
        d["root"] = str(self.root)
        return d


_SECTIONS = {
    "adapter": AdapterConfig,
    "collector": CollectorConfig,
    "upload": UploadConfig,
    "display": DisplayConfig,
}


def _build(section: str, data: dict[str, Any]):
    cls = _SECTIONS[section]
    known = {f for f in cls.__dataclass_fields__}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown keys in [{section}]: {sorted(unknown)}")
    return cls(**data)


def load_config(path: Optional[str | Path] = None, root: Optional[str | Path] = None) -> Config:
    """Load configuration from *path*, falling back to built-in defaults."""
    cfg = Config()
    if root is not None:
        cfg.root = Path(root)
    if path is None:
        cfg.upload.validate()
        return cfg
    path = Path(path)
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    unknown = set(data) - set(_SECTIONS)
    if unknown:
        raise ValueError(f"unknown configuration sections: {sorted(unknown)}")
    for section in _SECTIONS:
        if section in data:
            setattr(cfg, section, _build(section, data[section]))
    if root is None:
        cfg.root = path.resolve().parent.parent
    cfg.upload.validate()
    return cfg
