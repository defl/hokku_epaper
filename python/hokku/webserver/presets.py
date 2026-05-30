"""Named ImageConfig presets — UI selects one, fields populate, user tweaks.

Three curated presets cover the common cases. Power users can reach any
algorithm + LUT combination via the Advanced panel's individual controls.
"""

from __future__ import annotations

from dataclasses import replace

from hokku.webserver.dither_config import AlgorithmName, DitherConfig
from hokku.webserver.image_config import ImageConfig


def _hue_aware(algorithm: AlgorithmName, serpentine: bool = False) -> ImageConfig:
    return ImageConfig(
        dither=DitherConfig(
            algorithm=algorithm,
            lut_name="hue_aware",
            serpentine=serpentine,
            hue_cutoff_deg=95.0,
            neutral_chroma=8.0,
        ),
        prepare_autocontrast_cutoff=0.5,
        prepare_gamma=0.88,
        prepare_brightness=1.0,
        prepare_contrast=1.1,
        color_enhance=1.25,
        adaptive_saturate_space="cielab",
        saturate_max_enhance=1.25,
        saturate_low_chroma_thresh=5.0,
        saturate_high_chroma_thresh=15.0,
        saturate_low_chroma_thresh_oklab=0.025,
        saturate_high_chroma_thresh_oklab=0.075,
        scale_chroma=False,
        adaptive_vivid=True,
        vivid_chroma_low=5.0,
        vivid_chroma_high=15.0,
        vivid_chroma_low_oklab=0.025,
        vivid_chroma_high_oklab=0.075,
        drc_l_space="cielab",
        drc_chroma_space="cielab",
        prepare_midtone=1.02,
        clahe_clip_limit=1.75,
        clahe_keepout_feather=0.015,
        prepare_usm_radius=1.0,
        prepare_usm_amount=120,
        dither_noise=2.0,
    )


def _bw(algorithm: AlgorithmName, serpentine: bool = False) -> ImageConfig:
    base = _hue_aware(algorithm, serpentine)
    bw_dither = replace(base.dither, lut_name="bw")
    return replace(
        base,
        dither=bw_dither,
        color_enhance=1.05,
        adaptive_saturate_space="off",
        adaptive_vivid=False,
    )


PRESET_IMAGE_CONFIGS: dict[str, ImageConfig] = {
    "floyd_steinberg_hue_aware": _hue_aware("floyd_steinberg", serpentine=True),
    "floyd_steinberg_bw": _bw("floyd_steinberg", serpentine=True),
    "atkinson_hue_aware": _hue_aware("atkinson"),
}

FALLBACK_PRESET = "floyd_steinberg_hue_aware"


# UI-only metadata. Kept out of the dataclass so cache_slug() stays stable
# across copy edits and so dataclass equality isn't perturbed by labels.
PRESET_META: dict[str, dict[str, str]] = {
    "floyd_steinberg_hue_aware": {
        "label": "Floyd-Steinberg (hue-aware)",
        "description": "Floyd-Steinberg with a hue-constrained palette LUT, adaptive saturation + adaptive vivid. Punchier reds/blues without bleeding into other inks.",
    },
    "floyd_steinberg_bw": {
        "label": "Floyd-Steinberg (neutral)",
        "description": "Floyd-Steinberg with colour boosting disabled. For B&W photos and near-monochrome images — avoids tinting near-neutral greys pink or yellow.",
    },
    "atkinson_hue_aware": {
        "label": "Atkinson (hue-aware)",
        "description": "Default. Atkinson with hue-constrained LUT and adaptive saturation/vivid. Bold output with believable colours.",
    },
}
