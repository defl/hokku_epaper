"""ScreenImageConfig — the complete spec for rendering one image onto the panel."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from hokku.webserver.bounding_box import BoundingBox
from hokku.webserver.image_config import ImageConfig, image_config_from_dict_strict
from hokku.webserver.orientation import Orientation


@dataclass(frozen=True)
class ScreenImageConfig:
    """The complete spec for rendering one image onto the panel:
    which dithering pipeline, in which orientation, how aggressively to crop
    out letterbox bands, and where faces are (if detected).

    All fields together uniquely determine the panel binary output, so this
    struct is used as the cache key for panel .bin files.
    """

    image_config: ImageConfig
    orientation: Orientation
    crop_to_fill_threshold: float = 0.0
    #: Face bounding boxes or None.
    #: Passed to the renderer to scope CLAHE away from the face regions.
    clahe_keepout_bboxes: tuple[BoundingBox, ...] | None = None
    #: Face bounding boxes to center the cover-crop window on, or None.
    #: "Face-aware cropping" — biases which side of the image gets cropped
    #: away so faces stay centered instead of the plain image center.
    face_crop_bboxes: tuple[BoundingBox, ...] | None = None
    #: Target screen model. Part of the cache key so different-geometry models
    #: (e.g. Bigme F7 192 KB vs Huessen 960 KB) never share a panel .bin file.
    screen_model: str = "huessen_epf1301"

    def cache_slug(self) -> str:
        # Convert BoundingBox objects to dicts for JSON serialization
        bbox_serializable = None
        if self.clahe_keepout_bboxes:
            bbox_serializable = [asdict(b) for b in self.clahe_keepout_bboxes]

        payload = {
            "image_config": self.image_config.cache_slug(),
            "orientation": self.orientation,
            "crop_to_fill_threshold": self.crop_to_fill_threshold,
            "clahe_keepout_bboxes": bbox_serializable,
        }
        # Only non-default models contribute to the slug, so existing Huessen
        # cache files keep their slugs unchanged across the v3→v4 upgrade
        # (no re-render storm) while other models get distinct slugs/files.
        if self.screen_model != "huessen_epf1301":
            payload["screen_model"] = self.screen_model
        # Face-aware cropping is a new, off-by-default feature — only fold it
        # into the slug when actually in use, so upgrading doesn't invalidate
        # every existing cached render.
        if self.face_crop_bboxes:
            payload["face_crop_bboxes"] = [asdict(b) for b in self.face_crop_bboxes]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:14]


def _screen_image_config_from_dict(d: dict) -> ScreenImageConfig:
    """Round-trip helper: dict → ScreenImageConfig."""
    image_config = image_config_from_dict_strict(d.get("image_config"), field_path="image_config")
    orientation = Orientation(d["orientation"])
    crop_to_fill_threshold = float(d.get("crop_to_fill_threshold", 0.0))
    raw = d.get("clahe_keepout_bboxes")
    if raw is not None:
        try:
            keepout = tuple(BoundingBox(x=b["x"], y=b["y"], w=b["w"], h=b["h"]) for b in raw)
        except (ValueError, KeyError, TypeError):
            keepout = None
    else:
        keepout = None
    raw_crop = d.get("face_crop_bboxes")
    if raw_crop is not None:
        try:
            face_crop = tuple(BoundingBox(x=b["x"], y=b["y"], w=b["w"], h=b["h"]) for b in raw_crop)
        except (ValueError, KeyError, TypeError):
            face_crop = None
    else:
        face_crop = None
    return ScreenImageConfig(
        image_config=image_config,
        orientation=orientation,
        crop_to_fill_threshold=crop_to_fill_threshold,
        clahe_keepout_bboxes=keepout,
        face_crop_bboxes=face_crop,
        screen_model=str(d.get("screen_model", "huessen_epf1301")),
    )
