"""ImageConfig dataclass and its JSON helper."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from typing import Any, Literal

from hokku_server.dither_config import DitherConfig


Orientation = Literal["landscape", "portrait"]


@dataclass(frozen=True)
class ImageConfig:
    """How to convert a source image to palette indices.

    Orientation is *not* stored here — it lives on AppConfig and is passed
    explicitly to the render functions.
    """

    dither: DitherConfig
    prepare_autocontrast_cutoff: float
    prepare_gamma: float
    prepare_brightness: float
    prepare_contrast: float
    prepare_sharpness: float
    color_enhance: float
    use_adaptive_saturate: bool
    saturate_max_enhance: float
    saturate_low_chroma_thresh: float
    saturate_high_chroma_thresh: float
    scale_chroma: bool
    adaptive_vivid: bool
    vivid_chroma_low: float
    vivid_chroma_high: float

    def cache_slug(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:14]


def _image_config_from_dict(blob: Any, *, field_path: str = "image_config") -> ImageConfig:
    """Build an ImageConfig from a nested JSON object (or default if absent).

    Args:
        blob:       The dict (or None) to parse.
        field_path: Used in error messages to identify which config field is bad.
    """
    from hokku_server.presets import DEFAULT_PRESET, PRESET_IMAGE_CONFIGS  # avoid circular at import time

    if blob is None:
        return PRESET_IMAGE_CONFIGS[DEFAULT_PRESET]
    if not isinstance(blob, dict):
        raise ValueError(f"config['{field_path}'] must be an object")

    dither_blob = blob.get("dither")
    if not isinstance(dither_blob, dict):
        raise ValueError(f"config['{field_path}']['dither'] must be an object")
    dither_kwargs = {f.name: dither_blob[f.name] for f in fields(DitherConfig) if f.name in dither_blob}
    missing = {f.name for f in fields(DitherConfig)} - dither_kwargs.keys()
    if missing:
        raise ValueError(f"config['{field_path}']['dither'] missing fields: {sorted(missing)}")
    dither = DitherConfig(**dither_kwargs)

    image_kwargs: dict[str, Any] = {"dither": dither}
    for f in fields(ImageConfig):
        if f.name == "dither":
            continue
        if f.name not in blob:
            raise ValueError(f"config['{field_path}'] missing field: {f.name}")
        image_kwargs[f.name] = blob[f.name]
    return ImageConfig(**image_kwargs)
