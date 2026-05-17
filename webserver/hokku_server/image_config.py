"""ImageConfig dataclass and its JSON helper."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any, Literal

from hokku_server.dither_config import DitherConfig
from hokku_server.orientation import Orientation  # noqa: F401 (re-exported)


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
    color_enhance: float
    use_adaptive_saturate: bool
    saturate_max_enhance: float
    saturate_low_chroma_thresh: float
    saturate_high_chroma_thresh: float
    scale_chroma: bool
    adaptive_vivid: bool
    vivid_chroma_low: float
    vivid_chroma_high: float
    prepare_midtone: float
    clahe_clip_limit: float
    clahe_keepout_feather: float  # sigma = min(canvas_w, canvas_h) * this; 0 = hard edge
    prepare_usm_radius: float
    prepare_usm_amount: int
    dither_noise: float

    def cache_slug(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:14]


def _bw_safe_image_config(cfg: "ImageConfig") -> "ImageConfig":
    """Return *cfg* with saturation boosters disabled (safe for B&W images)."""
    return replace(
        cfg,
        color_enhance=1.05,
        use_adaptive_saturate=False,
        adaptive_vivid=False,
        scale_chroma=False,
    )


def _image_config_from_dict(blob: Any, *, field_path: str = "image_config") -> ImageConfig:
    """Build an ImageConfig from a nested JSON object (or default if absent).

    All fields are required. If blob is None or any field is missing the
    default preset is returned, resetting the config rather than patching it.

    Args:
        blob:       The dict (or None) to parse.
        field_path: Used in error messages to identify which config field is bad.
    """
    from hokku_server.presets import FALLBACK_PRESET, PRESET_IMAGE_CONFIGS  # avoid circular at import time

    if blob is None:
        return PRESET_IMAGE_CONFIGS[FALLBACK_PRESET]
    if not isinstance(blob, dict):
        raise ValueError(f"config['{field_path}'] must be an object")

    dither_blob = blob.get("dither")
    if not isinstance(dither_blob, dict):
        return PRESET_IMAGE_CONFIGS[FALLBACK_PRESET]
    dither_kwargs = {f.name: dither_blob[f.name] for f in fields(DitherConfig) if f.name in dither_blob}
    missing_dither = {f.name for f in fields(DitherConfig)} - dither_kwargs.keys()
    if missing_dither:
        return PRESET_IMAGE_CONFIGS[FALLBACK_PRESET]
    dither = DitherConfig(**dither_kwargs)

    image_kwargs: dict[str, Any] = {"dither": dither}
    for f in fields(ImageConfig):
        if f.name == "dither":
            continue
        if f.name not in blob:
            return PRESET_IMAGE_CONFIGS[FALLBACK_PRESET]
        image_kwargs[f.name] = blob[f.name]

    return ImageConfig(**image_kwargs)
