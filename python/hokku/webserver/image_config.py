"""ImageConfig dataclass and its JSON helper."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, fields, replace
from typing import Any, Literal

from hokku.webserver.dither_config import DitherConfig

logger = logging.getLogger(__name__)

AdaptiveSaturateSpace = Literal["off", "cielab", "oklab"]
DrcSpace = Literal["cielab", "oklab"]


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
    adaptive_saturate_space: AdaptiveSaturateSpace
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
    drc_l_space: DrcSpace  # space for L compression
    drc_chroma_space: DrcSpace  # space for chroma scaling
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


# NOTE: there used to be a _LENIENT_DEFAULTS table here, listing the fields a
# stored config was allowed to omit. It existed because the parser reset a
# pipeline wholesale when any field was missing, so every field added to
# ImageConfig had to be hand-registered in a second place or it would silently
# wipe people's tuning on upgrade. That is what happened: clahe_keepout_feather
# was added without being listed, and from then on the shipped
# config.json.example (and any config predating that field) reset all three
# pipelines to the fallback preset. Merging onto the defaults makes the table
# unnecessary — a missing field is simply a field that keeps its default.


def _image_config_from_dict(
    blob: Any,
    *,
    field_path: str = "image_config",
    default: ImageConfig | None = None,
) -> ImageConfig:
    """Build an ImageConfig by merging a stored JSON object onto the defaults.

    Fields present in *blob* win; fields absent keep their value from
    *default*. A config that predates a field therefore keeps every setting it
    does carry, instead of being discarded.

    This used to reset the whole pipeline to the fallback preset if a single
    field was missing, which silently wiped tuning on upgrade — the entire
    shipped example config was being reset that way. See the note where
    _LENIENT_DEFAULTS used to live.

    Args:
        blob:       The dict (or None) to parse.
        field_path: Used in log/error messages to identify which config field is bad.
        default:    Base to merge onto. Defaults to the fallback preset; callers
                    should pass the default for *their* pipeline so a sparse
                    B&W or face blob falls back to B&W or face values rather
                    than to the generic default pipeline.
    """
    from hokku.webserver.presets import (  # noqa: PLC0415 — deferred to break circular import
        FALLBACK_PRESET,
        PRESET_IMAGE_CONFIGS,
    )

    base = default if default is not None else PRESET_IMAGE_CONFIGS[FALLBACK_PRESET]

    if blob is None:
        return base
    if not isinstance(blob, dict):
        raise ValueError(f"config['{field_path}'] must be an object")

    # Renames still need handling explicitly — a renamed field is not the same
    # thing as a missing one, and dropping it would lose a real setting.
    blob = dict(blob)  # shallow copy — don't mutate the caller's dict
    if "adaptive_saturate_space" not in blob and "use_adaptive_saturate" in blob:
        blob["adaptive_saturate_space"] = "cielab" if blob["use_adaptive_saturate"] else "off"
    blob.pop("use_adaptive_saturate", None)  # tolerate either presence; ignore now

    dither_blob = blob.get("dither")
    if dither_blob is not None and not isinstance(dither_blob, dict):
        raise ValueError(f"config['{field_path}']['dither'] must be an object")
    dither_blob = dither_blob or {}
    dither_kwargs = {
        f.name: (dither_blob[f.name] if f.name in dither_blob else getattr(base.dither, f.name))
        for f in fields(DitherConfig)
    }
    dither = DitherConfig(**dither_kwargs)

    image_kwargs: dict[str, Any] = {"dither": dither}
    filled_from_default: list[str] = []
    for f in fields(ImageConfig):
        if f.name == "dither":
            continue
        if f.name in blob:
            image_kwargs[f.name] = blob[f.name]
        else:
            image_kwargs[f.name] = getattr(base, f.name)
            filled_from_default.append(f.name)

    if filled_from_default:
        # Say so. The old reset was completely silent, which is why a shipped
        # example config could be discarded on every single load unnoticed.
        logger.info(
            "config['%s']: %d field(s) absent, kept defaults: %s",
            field_path,
            len(filled_from_default),
            ", ".join(sorted(filled_from_default)),
        )

    return ImageConfig(**image_kwargs)
