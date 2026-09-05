"""Per-screen user configuration stored alongside telemetry in serve_scheduler.json."""

from __future__ import annotations

from dataclasses import dataclass

from hokku.webserver.collection_store import ALL_COLLECTION_ID
from hokku.webserver.orientation import Orientation


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
    server_url_override: str = ""
    active_collection_id: str = ALL_COLLECTION_ID

    def __post_init__(self) -> None:
        assert self.orientation in (Orientation.LANDSCAPE, Orientation.PORTRAIT), (
            f"ScreenConfig.orientation must be LANDSCAPE or PORTRAIT, got {self.orientation!r}"
        )

    def to_dict(self) -> dict:
        return {
            "orientation": self.orientation,
            "filter_by_orientation": self.filter_by_orientation,
            "server_url_override": self.server_url_override,
            "active_collection_id": self.active_collection_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ScreenConfig:
        raw_collection_id = d.get("active_collection_id", ALL_COLLECTION_ID)
        return cls(
            orientation=Orientation(d["orientation"]),
            filter_by_orientation=bool(d.get("filter_by_orientation", False)),
            server_url_override=str(d.get("server_url_override", "")),
            active_collection_id=(
                raw_collection_id
                if isinstance(raw_collection_id, str) and raw_collection_id
                else ALL_COLLECTION_ID
            ),
        )
