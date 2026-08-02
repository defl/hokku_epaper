"""ImageRecord dataclass and serialisation helpers."""

from __future__ import annotations

import enum
import logging
from dataclasses import asdict, dataclass, field

from hokku.webserver.image_config import (
    ImageConfig,
    ImageConfigError,
    image_config_from_dict_strict,
    parse_crop_to_fill_threshold,
)
from hokku.webserver.orientation import Orientation

logger = logging.getLogger(__name__)

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

    # ── per-picture overrides ────────────────────────────────────────────────
    # User-authored, and the only fields here that are not derived from the
    # source file: everything else can be recomputed, these cannot. Set = this
    # picture ignores the corresponding automatic choice; None = automatic.
    # The two are independent — overriding the pipeline says nothing about the
    # crop, and vice versa.
    #
    # Both feed ScreenImageConfig.cache_slug(), so setting one changes the slug
    # and the picture re-renders on its own; nothing else has to invalidate it.
    #
    # Additive and optional, so _DB_VERSION does not move: a v4 row without them
    # loads fine. An older binary reading a newer file ignores what it doesn't
    # know, so downgrading drops overrides silently — the pictures revert to
    # automatic rather than breaking.
    image_config: ImageConfig | None = None  # None = the classifier picks
    crop_to_fill_threshold: float | None = None  # None = AppConfig's global value

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

    @staticmethod
    def _overrides_from_dict(d: dict) -> tuple[ImageConfig | None, float | None]:
        """Parse the two override fields, dropping either one if it is corrupt.

        Errors are swallowed on purpose. _load_db() skips any record whose
        from_dict() raises, so letting a malformed override propagate would
        cost the whole image — its dimensions, its render slugs, its status —
        and force a needless re-render. Losing just the override is the
        smaller, more recoverable failure, and it is logged.
        """
        image_config: ImageConfig | None = None
        raw_cfg = d.get("image_config")
        if raw_cfg is not None:
            try:
                image_config = image_config_from_dict_strict(
                    raw_cfg, field_path="image_config override"
                )
            except ImageConfigError as e:
                logger.warning("Dropping malformed dither override for %r: %s", d.get("name"), e)

        crop: float | None = None
        raw_crop = d.get("crop_to_fill_threshold")
        if raw_crop is not None:
            try:
                crop = parse_crop_to_fill_threshold(raw_crop)
            except ImageConfigError as e:
                logger.warning("Dropping malformed crop override for %r: %s", d.get("name"), e)

        return image_config, crop

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
        image_config, crop_to_fill_threshold = cls._overrides_from_dict(d)
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
            image_config=image_config,
            crop_to_fill_threshold=crop_to_fill_threshold,
        )


@dataclass(frozen=True)
class ConversionProgress:
    current_name: str | None  # being converted right now (None if idle)
    done: int  # completed this sync cycle
    total: int  # scheduled this sync cycle
