"""Per-screen user configuration stored alongside telemetry in serve_scheduler.json."""
from __future__ import annotations

from dataclasses import dataclass

from hokku_server.orientation import Orientation


@dataclass(frozen=True)
class ScreenConfig:
    """Persistent, user-configurable settings for one connected screen.

    ``orientation_override``: when set, this screen always receives images
    rendered in the specified orientation regardless of the global server
    setting.  ``None`` means "follow the global default".
    """

    orientation_override: Orientation | None = None

    def to_dict(self) -> dict:
        return {"orientation_override": self.orientation_override}

    @classmethod
    def from_dict(cls, d: dict) -> ScreenConfig:
        raw = d.get("orientation_override")
        return cls(orientation_override=Orientation(raw) if raw else None)
