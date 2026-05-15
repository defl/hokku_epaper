"""ScreenImageConfig — the complete spec for rendering one image onto the panel."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from hokku_server.image_config import ImageConfig, Orientation, _image_config_from_dict


@dataclass(frozen=True)
class ScreenImageConfig:
    """The complete spec for rendering one image onto the panel:
    which dithering pipeline, in which orientation, and how aggressively
    to crop out letterbox bands.

    All three fields together uniquely determine the panel binary output,
    so this struct is used as the cache key for panel .bin files.
    """

    image_config: ImageConfig
    orientation: Orientation
    #: Maximum zoom ratio (e.g. 0.02 = 2%) applied to eliminate letterbox
    #: bands.  0.0 = always letterbox (default, safe).
    crop_to_fill_threshold: float = 0.0

    def cache_slug(self) -> str:
        payload = {
            "image_config": self.image_config.cache_slug(),
            "orientation": self.orientation,
            "crop_to_fill_threshold": self.crop_to_fill_threshold,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()[:14]


def _screen_image_config_from_dict(d: dict) -> ScreenImageConfig:
    """Round-trip helper: dict → ScreenImageConfig."""
    image_config = _image_config_from_dict(d.get("image_config"), field_path="image_config")
    orientation = d["orientation"]
    crop_to_fill_threshold = float(d.get("crop_to_fill_threshold", 0.0))
    return ScreenImageConfig(
        image_config=image_config,
        orientation=orientation,
        crop_to_fill_threshold=crop_to_fill_threshold,
    )
