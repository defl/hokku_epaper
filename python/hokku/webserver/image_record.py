"""ImageRecord dataclass and serialisation helpers."""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field

from hokku.webserver.orientation import Orientation

# Default model used to migrate pre-v4 records (which only had a single
# implicit screen model) into the model-keyed ``slugs`` dict.
_LEGACY_MODEL = "huessen_epf1301"


class ConvertStatus(str, enum.Enum):
    OK = "ok"
    FAILED = "failed"
    PENDING = "pending"


@dataclass(frozen=True)
class ImageRecord:
    name: str  # outside-world identifier
    name_hash: str  # sha1(name) — on-disk identifier
    original_sha1: str  # sha1 of file contents
    original_size_bytes: int
    original_mtime: float
    added_at: float
    convert_status: ConvertStatus
    convert_error: str | None
    # Cached ScreenImageConfig slug per (model, orientation), keyed by
    # ``"{model}.{orientation.value}"`` — e.g. "huessen_epf1301.landscape".
    slugs: dict[str, str] = field(default_factory=dict)
    last_conversion_seconds: float | None = None  # wall-clock time of last successful render
    image_width: int | None = None  # pixel dimensions of the source image
    image_height: int | None = None

    def slug_for(self, model: str, orientation: Orientation) -> str | None:
        """Return the cached slug for (model, orientation), or None if not rendered."""
        assert orientation != Orientation.NEUTRAL, "slug_for() requires LANDSCAPE or PORTRAIT"
        return self.slugs.get(f"{model}.{orientation.value}")

    @property
    def native_orientation(self) -> Orientation:
        """Returns LANDSCAPE, PORTRAIT, or NEUTRAL (square).

        Only valid for ok-status images — callers must ensure convert_status == ConvertStatus.OK.
        """
        assert self.convert_status == ConvertStatus.OK, (
            f"native_orientation called on non-ok image {self.name!r} "
            f"(status={self.convert_status})"
        )
        w, h = self.image_width, self.image_height
        assert w and h, f"ok-status image {self.name!r} has invalid dimensions ({w}, {h})"
        if w == h:
            return Orientation.NEUTRAL
        return Orientation.LANDSCAPE if w > h else Orientation.PORTRAIT

    def matches_orientation_filter(self, orientation: Orientation) -> bool:
        """True if this image is eligible when a screen filters by the given orientation."""
        assert orientation != Orientation.NEUTRAL, "pass LANDSCAPE or PORTRAIT to a filter"
        return self.native_orientation in (Orientation.NEUTRAL, orientation)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ImageRecord:
        raw_t = d.get("last_conversion_seconds")
        raw_w, raw_h = d.get("image_width"), d.get("image_height")
        # v4+ stores a model-keyed ``slugs`` dict.  Migrate pre-v4 records that
        # carried single landscape/portrait slug fields (implicitly Huessen).
        if "slugs" in d and isinstance(d["slugs"], dict):
            slugs = {str(k): str(v) for k, v in d["slugs"].items() if v}
        else:
            slugs = {}
            ls = d.get("landscape_image_config_slug")
            ps = d.get("portrait_image_config_slug")
            if ls:
                slugs[f"{_LEGACY_MODEL}.{Orientation.LANDSCAPE.value}"] = ls
            if ps:
                slugs[f"{_LEGACY_MODEL}.{Orientation.PORTRAIT.value}"] = ps
        return cls(
            name=d["name"],
            name_hash=d["name_hash"],
            original_sha1=d["original_sha1"],
            original_size_bytes=int(d["original_size_bytes"]),
            original_mtime=float(d["original_mtime"]),
            added_at=float(d["added_at"]),
            convert_status=ConvertStatus(d["convert_status"]),
            convert_error=d.get("convert_error"),
            slugs=slugs,
            last_conversion_seconds=float(raw_t) if raw_t is not None else None,
            image_width=int(raw_w) if raw_w is not None else None,
            image_height=int(raw_h) if raw_h is not None else None,
        )


@dataclass(frozen=True)
class ConversionProgress:
    current_name: str | None  # being converted right now (None if idle)
    done: int  # completed this sync cycle
    total: int  # scheduled this sync cycle
