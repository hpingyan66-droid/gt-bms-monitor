"""Bundled distribution capabilities, never selected by an HTTP request.

These editions limit the shipped application. They are not account roles or a
tamper-proof licensing boundary against someone who can modify the source.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


class EditionError(ValueError):
    """A missing or invalid package edition must prevent application startup."""


_CAPABILITIES = {
    "full_access": dict(can_operate=True, can_manage_network=True, can_export=True,
                        can_view_gps=True, can_maintain=True),
    "corporate": dict(can_operate=True, can_manage_network=False, can_export=True,
                      can_view_gps=True, can_maintain=False),
    "individual": dict(can_operate=True, can_manage_network=False, can_export=True,
                       can_view_gps=True, can_maintain=False),
}
_LABELS = {"full_access": "フルアクセス版", "corporate": "法人利用版", "individual": "個人利用版"}


@dataclass(frozen=True)
class Edition:
    id: str

    def __post_init__(self):
        if type(self.id) is not str or self.id not in _CAPABILITIES:
            raise EditionError("Invalid application edition")

    @property
    def label(self) -> str:
        return _LABELS[self.id]

    @property
    def can_operate(self) -> bool:
        return _CAPABILITIES[self.id]["can_operate"]

    @property
    def can_manage_network(self) -> bool:
        return _CAPABILITIES[self.id]["can_manage_network"]

    @property
    def can_export(self) -> bool:
        return _CAPABILITIES[self.id]["can_export"]

    @property
    def can_view_gps(self) -> bool:
        return _CAPABILITIES[self.id]["can_view_gps"]

    @property
    def can_maintain(self) -> bool:
        return _CAPABILITIES[self.id]["can_maintain"]

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "can_operate": self.can_operate,
                "can_manage_network": self.can_manage_network, "can_export": self.can_export,
                "can_view_gps": self.can_view_gps, "can_maintain": self.can_maintain}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EditionError("Invalid application edition")
        result[key] = value
    return result


def load_edition(path: Path | None = None) -> Edition:
    """Load the package manifest; ``path`` is an offline build/test hook only."""
    source = Path(path) if path is not None else Path(__file__).with_name("edition.json")
    try:
        data = json.loads(source.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        if (not isinstance(data, dict) or set(data) != {"schema_version", "id"}
                or type(data["schema_version"]) is not int or data["schema_version"] != 1):
            raise EditionError("Invalid application edition")
        return Edition(data["id"])
    except (OSError, UnicodeError, ValueError, TypeError):
        # Do not leak arbitrary manifest contents or local paths in diagnostics.
        raise EditionError("Application edition is missing or invalid; reinstall a verified package") from None
