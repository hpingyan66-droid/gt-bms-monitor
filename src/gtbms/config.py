from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal
from urllib.parse import urlparse


@dataclass
class Config:
    mode: Literal["mock", "ble"] = "mock"
    battery_id: str = "gt50"
    ble_address: str = ""
    expected_name: str = "SP17S005P17S40A"
    expected_cells: int = 16
    pair: bool = False
    poll_seconds: float = 2
    request_timeout_seconds: float = 8
    current_direction: str = "unconfirmed"
    nominal_voltage_v: float = 59.2
    configured_capacity_ah: float = 50
    measured_usable_wh: float | None = None
    reserve_fraction: float = 0.15
    max_gap_seconds: float = 10
    stale_seconds: float = 10
    retention_days: int = 0
    backup_hours: float = 24
    backup_keep: int = 14
    port: int = 8765
    autostart_monitor: bool = True
    gps_enabled: bool = False
    sync_enabled: bool = False
    sync_url: str = ""
    sync_include_gps: bool = False
    mock_disconnect_every: int = 0
    data_dir: str = "data"
    backup_dir: str = "backups"

    def validate(self) -> Config:
        for name in ("pair", "autostart_monitor", "gps_enabled", "sync_enabled", "sync_include_gps"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a JSON boolean")
        for name in ("mode", "battery_id", "ble_address", "expected_name", "current_direction",
                     "sync_url", "data_dir", "backup_dir"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be a string")
        for name in ("expected_cells", "port", "retention_days", "backup_keep", "mock_disconnect_every"):
            if type(getattr(self, name)) is not int:
                raise ValueError(f"{name} must be an integer")
        if self.mode not in ("mock", "ble"):
            raise ValueError("mode must be mock or ble")
        if self.current_direction not in ("unconfirmed", "charge_positive", "discharge_positive"):
            raise ValueError("invalid current_direction")
        if not self.battery_id.strip() or len(self.battery_id) > 80:
            raise ValueError("invalid battery_id")
        if not self.data_dir.strip() or not self.backup_dir.strip():
            raise ValueError("data_dir and backup_dir must not be empty")
        for name in ("poll_seconds", "request_timeout_seconds", "nominal_voltage_v",
                     "configured_capacity_ah", "max_gap_seconds", "stale_seconds", "backup_hours"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"invalid {name}")
        if not 1 <= self.poll_seconds <= 10:
            raise ValueError("poll_seconds must be 1..10")
        if not 1 <= self.expected_cells <= 32 or not 1024 <= self.port <= 65535:
            raise ValueError("invalid cells or port")
        if (type(self.reserve_fraction) not in (int, float) or
                not math.isfinite(self.reserve_fraction) or not 0 <= self.reserve_fraction < 1):
            raise ValueError("invalid reserve_fraction")
        if self.measured_usable_wh is not None and (type(self.measured_usable_wh) not in (int, float) or
                not math.isfinite(self.measured_usable_wh) or self.measured_usable_wh <= 0):
            raise ValueError("invalid measured_usable_wh")
        if self.retention_days < 0 or self.backup_keep < 1 or self.mock_disconnect_every < 0:
            raise ValueError("invalid retention/backup/mock setting")
        self.max_gap_seconds = max(self.max_gap_seconds, self.poll_seconds*3)
        self.stale_seconds = max(self.stale_seconds, self.poll_seconds*3)
        if self.sync_enabled:
            url = urlparse(self.sync_url)
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
                raise ValueError("sync requires an HTTPS URL without embedded credentials")
        return self

    @property
    def key(self) -> str:
        # Mock measurements can never train or overwrite the real battery history.
        return f"{self.battery_id}:{self.mode}"

    @classmethod
    def load(cls, path: Path) -> Config:
        if not path.exists():
            raise ValueError("config.local.json is missing; run scripts/Setup.ps1")
        return cls(**json.loads(path.read_text(encoding="utf-8-sig"))).validate()

    def save(self, path: Path) -> None:
        # Keep the temporary file on the same volume for atomic replacement.
        # Unique names prevent concurrent callers from replacing each other's data.
        tmp = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                    prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
                tmp = Path(handle.name)
                json.dump(asdict(self), handle, indent=2, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(path)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
