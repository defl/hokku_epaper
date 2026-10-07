"""Per-screen user configuration stored alongside telemetry in serve_scheduler.json."""

from __future__ import annotations

from dataclasses import dataclass

from hokku.webserver.labels import LabelError, parse_labels
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

    ``labels``: when non-empty, only images carrying at least one of these
    labels are eligible. Empty (the default, and what every pre-existing
    screen loads as) means no label filter — the whole library.
    """

    orientation: Orientation = Orientation.LANDSCAPE
    filter_by_orientation: bool = False
    server_url_override: str = ""
    labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        assert self.orientation in (Orientation.LANDSCAPE, Orientation.PORTRAIT), (
            f"ScreenConfig.orientation must be LANDSCAPE or PORTRAIT, got {self.orientation!r}"
        )

    def to_dict(self) -> dict:
        return {
            "orientation": self.orientation,
            "filter_by_orientation": self.filter_by_orientation,
            "server_url_override": self.server_url_override,
            "labels": list(self.labels),
        }

    @classmethod
    def from_dict(cls, d: dict) -> ScreenConfig:
        return cls(
            orientation=Orientation(d["orientation"]),
            filter_by_orientation=bool(d.get("filter_by_orientation", False)),
            server_url_override=str(d.get("server_url_override", "")),
            labels=cls._labels_from_dict(d),
        )

    @staticmethod
    def _labels_from_dict(d: dict) -> tuple[str, ...]:
        raw = d.get("labels")
        if raw is None:
            return ()
        try:
            return parse_labels(raw)
        except LabelError:
            # A corrupt filter is the smaller loss: the screen falls back to the
            # whole library rather than the record being skipped.
            return ()
