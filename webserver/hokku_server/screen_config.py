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

    ``filter_by_orientation``: when True, only images whose native orientation
    matches the screen's effective orientation are eligible for serving.
    Square images (NEUTRAL) are always eligible regardless of this flag.
    """

    orientation_override: Orientation | None = None
    filter_by_orientation: bool = False

    def __post_init__(self) -> None:
        assert self.orientation_override != Orientation.NEUTRAL, (
            "orientation_override cannot be NEUTRAL; use None to follow the global default"
        )

    def to_dict(self) -> dict:
        return {
            "orientation_override": self.orientation_override,
            "filter_by_orientation": self.filter_by_orientation,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ScreenConfig:
        raw = d.get("orientation_override")
        return cls(
            orientation_override=Orientation(raw) if raw else None,
            filter_by_orientation=bool(d.get("filter_by_orientation", False)),
        )
