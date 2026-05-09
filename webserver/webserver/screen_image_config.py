"""ScreenImageConfig — the complete spec for rendering one image onto the panel."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from webserver.image_config import ImageConfig, Orientation


@dataclass(frozen=True)
class ScreenImageConfig:
    """The complete spec for rendering one image onto the panel:
    which dithering pipeline, in which orientation.

    Both ``ImageConfig`` and orientation together uniquely determine the panel
    binary output, so this pair is used as the cache key for panel .bin files.
    """

    image_config: ImageConfig
    orientation: Orientation

    def cache_slug(self) -> str:
        payload = {
            "image_config": self.image_config.cache_slug(),
            "orientation": self.orientation,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()[:14]


def _screen_image_config_from_dict(d: dict) -> ScreenImageConfig:
    """Round-trip helper: dict → ScreenImageConfig."""
    from webserver.image_config import _image_config_from_dict
    image_config = _image_config_from_dict(d.get("image_config"), field_path="image_config")
    orientation = d["orientation"]
    return ScreenImageConfig(image_config=image_config, orientation=orientation)
