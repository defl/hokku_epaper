"""ImageConfig dataclass and its JSON helper."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields, replace
from typing import Any

from hokku_server.dither_config import DitherConfig


@dataclass(frozen=True)
class ImageConfig:
    """How to convert a source image to palette indices.

    Orientation is *not* stored here — it lives on the per-screen
    ScreenConfig and is passed explicitly to the render functions.
    """

    dither: DitherConfig
    prepare_autocontrast_cutoff: float
    prepare_gamma: float
    prepare_brightness: float
    prepare_contrast: float
    color_enhance: float
    adaptive_saturate_space: str  # "off" | "cielab" | "oklab"
    saturate_max_enhance: float
    saturate_low_chroma_thresh: float  # CIELAB chroma units
    saturate_high_chroma_thresh: float  # CIELAB chroma units
    saturate_low_chroma_thresh_oklab: float  # OKLAB chroma units (panel inks 0.09–0.18)
    saturate_high_chroma_thresh_oklab: float
    scale_chroma: bool
    adaptive_vivid: bool
    vivid_chroma_low: float  # CIELAB chroma units
    vivid_chroma_high: float  # CIELAB chroma units
    vivid_chroma_low_oklab: float  # OKLAB chroma units
    vivid_chroma_high_oklab: float
    drc_l_space: str  # "cielab" | "oklab" — space for L compression
    drc_chroma_space: str  # "cielab" | "oklab" — space for chroma scaling
    prepare_midtone: float
    clahe_clip_limit: float
    clahe_keepout_feather: float  # sigma = min(canvas_w, canvas_h) * this; 0 = hard edge
    prepare_usm_radius: float
    prepare_usm_amount: int
    dither_noise: float

    def cache_slug(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:14]


def _bw_safe_image_config(cfg: ImageConfig) -> ImageConfig:
    """Return *cfg* with saturation boosters disabled (safe for B&W images)."""
    return replace(
        cfg,
        color_enhance=1.05,
        adaptive_saturate_space="off",
        adaptive_vivid=False,
        scale_chroma=False,
    )


# Defaults for fields added after the original config schema. Used to migrate
# config blobs from older versions that don't carry the new field names.
_LENIENT_DEFAULTS: dict[str, Any] = {
    "saturate_low_chroma_thresh_oklab": 0.025,
    "saturate_high_chroma_thresh_oklab": 0.075,
    "vivid_chroma_low_oklab": 0.025,
    "vivid_chroma_high_oklab": 0.075,
    "drc_l_space": "cielab",
    "drc_chroma_space": "cielab",
}


def _image_config_from_dict(blob: Any, *, field_path: str = "image_config") -> ImageConfig:
    """Build an ImageConfig from a nested JSON object (or default if absent).

    All fields are required. If blob is None or any field is missing the
    default preset is returned, resetting the config rather than patching it.

    Args:
        blob:       The dict (or None) to parse.
        field_path: Used in error messages to identify which config field is bad.
    """
    from hokku_server.presets import (  # noqa: PLC0415 — deferred to break circular import
        FALLBACK_PRESET,
        PRESET_IMAGE_CONFIGS,
    )

    if blob is None:
        return PRESET_IMAGE_CONFIGS[FALLBACK_PRESET]
    if not isinstance(blob, dict):
        raise ValueError(f"config['{field_path}'] must be an object")

    # Migrate old fields that have been replaced or renamed before the strict
    # presence check kicks in.  This lets older config.json files load without
    # losing the user's tuning.
    blob = dict(blob)  # shallow copy — don't mutate the caller's dict
    if "adaptive_saturate_space" not in blob and "use_adaptive_saturate" in blob:
        blob["adaptive_saturate_space"] = "cielab" if blob["use_adaptive_saturate"] else "off"
    blob.pop("use_adaptive_saturate", None)  # tolerate either presence; ignore now
    for name, default in _LENIENT_DEFAULTS.items():
        blob.setdefault(name, default)

    dither_blob = blob.get("dither")
    if not isinstance(dither_blob, dict):
        return PRESET_IMAGE_CONFIGS[FALLBACK_PRESET]
    dither_kwargs = {
        f.name: dither_blob[f.name] for f in fields(DitherConfig) if f.name in dither_blob
    }
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
