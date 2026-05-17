"""ScreenImageConfig — the complete spec for rendering one image onto the panel."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from hokku_server.bounding_box import BoundingBox
from hokku_server.image_config import ImageConfig, _image_config_from_dict
from hokku_server.orientation import Orientation


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
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()[:14]


def _screen_image_config_from_dict(d: dict) -> ScreenImageConfig:
    """Round-trip helper: dict → ScreenImageConfig."""
    image_config = _image_config_from_dict(d.get("image_config"), field_path="image_config")
    orientation = Orientation(d["orientation"])
    crop_to_fill_threshold = float(d.get("crop_to_fill_threshold", 0.0))
    raw = d.get("clahe_keepout_bboxes")
    if raw is not None:
        try:
            keepout = tuple(BoundingBox(x=b['x'], y=b['y'], w=b['w'], h=b['h']) for b in raw)
        except (ValueError, KeyError, TypeError):
            keepout = None
    else:
        keepout = None
    return ScreenImageConfig(
        image_config=image_config,
        orientation=orientation,
        crop_to_fill_threshold=crop_to_fill_threshold,
        clahe_keepout_bboxes=keepout,
    )
