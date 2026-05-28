"""Per-screen user configuration stored alongside telemetry in serve_scheduler.json."""

from __future__ import annotations

from dataclasses import dataclass

from hokku_server.orientation import Orientation


@dataclass(frozen=True)
class ScreenConfig:
    """Persistent, user-configurable settings for one connected screen.

    ``orientation`` defaults to LANDSCAPE — this is the single canonical
    default in the codebase for an unconfigured screen. No other module
    carries an orientation default; everything else reads what the
    screen actually has.

    ``filter_by_orientation``: when True, only images whose native
    orientation matches the screen's orientation are eligible for
    serving. Square (NEUTRAL) images are always eligible regardless.
    """

    orientation: Orientation = Orientation.LANDSCAPE
    filter_by_orientation: bool = False

    def __post_init__(self) -> None:
        assert self.orientation in (Orientation.LANDSCAPE, Orientation.PORTRAIT), (
            f"ScreenConfig.orientation must be LANDSCAPE or PORTRAIT, got {self.orientation!r}"
        )

    def to_dict(self) -> dict:
        return {
            "orientation": self.orientation,
            "filter_by_orientation": self.filter_by_orientation,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ScreenConfig:
        return cls(
            orientation=Orientation(d["orientation"]),
            filter_by_orientation=bool(d.get("filter_by_orientation", False)),
        )
