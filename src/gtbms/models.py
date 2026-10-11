from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime | None = None) -> str:
    return (value or utcnow()).astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return result.astimezone(timezone.utc)


@dataclass
class Sample:
    battery_id: str
    source: str
    received_at: str
    voltage_v: float
    raw_current_a: float
    soc_pct: float
    remaining_ah: float
    configured_ah: float
    cycles: int
    cell_voltages_v: list[float]
    temperatures_c: list[float]
    charge_mos: bool
    discharge_mos: bool
    protection_code: int
    balancing_mask: int
    bms_version: int
    current_direction: str = "unconfirmed"
    bms_measured_at: str | None = None
    connection_id: str = ""
    monotonic_s: float = 0
    quality_flags: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: str(uuid4()))

    @property
    def current_a(self) -> float | None:
        if self.current_direction == "charge_positive":
            return -self.raw_current_a
        if self.current_direction == "discharge_positive":
            return self.raw_current_a
        return None

    @property
    def power_w(self) -> float | None:
        return None if self.current_a is None else self.voltage_v * self.current_a

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.update(current_a=self.current_a, power_w=self.power_w,
                    raw_power_w=self.voltage_v * self.raw_current_a,
                    cell_min_v=min(self.cell_voltages_v, default=None),
                    cell_max_v=max(self.cell_voltages_v, default=None),
                    cell_delta_v=(max(self.cell_voltages_v)-min(self.cell_voltages_v)) if self.cell_voltages_v else None,
                    state="unconfirmed" if self.current_a is None else "discharging" if self.current_a > .1 else "charging" if self.current_a < -.1 else "idle",
                    # Standard JBD NTC indices do not identify sensor placement.
                    cell_temperature_c=None, bms_temperature_c=None, mos_temperature_c=None,
                    alarms=protection_labels(self.protection_code))
        return data


PROTECTIONS = ["セル過電圧", "セル低電圧", "パック過電圧", "パック低電圧",
               "充電高温", "充電低温", "放電高温", "放電低温", "充電過電流",
               "放電過電流", "短絡", "フロントエンド異常", "MOSロック"]


def protection_labels(code: int) -> list[str]:
    labels = [label for i, label in enumerate(PROTECTIONS) if code & (1 << i)]
    if code & ~((1 << len(PROTECTIONS)) - 1):
        labels.append("未定義保護ビット")
    return labels
