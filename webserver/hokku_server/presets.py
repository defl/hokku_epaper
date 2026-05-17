"""Named ImageConfig presets — UI selects one, fields populate, user tweaks.

No magic; these are just canned field values. Each algorithm has a plain
(Euclidean LUT) variant, a `_hue_aware` variant, and a `_bw` variant.
Hue-aware variants enable adaptive saturation + adaptive vivid by default
since they pair well with the hue-constrained palette mapping. BW variants
disable colour boosting to avoid tinting near-neutral greys.
"""
from __future__ import annotations

from dataclasses import replace

from hokku_server.dither_config import DitherConfig
from hokku_server.image_config import ImageConfig


def _plain(algorithm: str, serpentine: bool = False) -> ImageConfig:
    return ImageConfig(
        dither=DitherConfig(
            algorithm=algorithm,  # type: ignore[arg-type]
            lut_name="euclidean",
            serpentine=serpentine,
            hue_cutoff_deg=95.0,
            neutral_chroma=8.0,
        ),
        prepare_autocontrast_cutoff=0.5,
        prepare_gamma=0.88,
        prepare_brightness=1.0,
        prepare_contrast=1.1,
        color_enhance=1.2,
        use_adaptive_saturate=False,
        saturate_max_enhance=1.0,
        saturate_low_chroma_thresh=5.0,
        saturate_high_chroma_thresh=15.0,
        scale_chroma=False,
        adaptive_vivid=False,
        vivid_chroma_low=5.0,
        vivid_chroma_high=15.0,
        prepare_midtone=1.02,
        clahe_clip_limit=1.75,
        clahe_keepout_feather=0.015,
        prepare_usm_radius=1.0,
        prepare_usm_amount=120,
        dither_noise=2.0,
    )


def _hue_aware(algorithm: str, serpentine: bool = False) -> ImageConfig:
    return ImageConfig(
        dither=DitherConfig(
            algorithm=algorithm,  # type: ignore[arg-type]
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
        use_adaptive_saturate=True,
        saturate_max_enhance=1.25,
        saturate_low_chroma_thresh=5.0,
        saturate_high_chroma_thresh=15.0,
        scale_chroma=False,
        adaptive_vivid=True,
        vivid_chroma_low=5.0,
        vivid_chroma_high=15.0,
        prepare_midtone=1.02,
        clahe_clip_limit=1.75,
        clahe_keepout_feather=0.015,
        prepare_usm_radius=1.0,
        prepare_usm_amount=120,
        dither_noise=2.0,
    )


def _bw(algorithm: str, serpentine: bool = False) -> ImageConfig:
    plain_cfg = _plain(algorithm, serpentine)
    bw_dither = replace(plain_cfg.dither, lut_name="bw")
    return replace(plain_cfg, dither=bw_dither, color_enhance=1.05)


PRESET_IMAGE_CONFIGS: dict[str, ImageConfig] = {
    "floyd_steinberg":           _plain("floyd_steinberg", serpentine=True),
    "floyd_steinberg_hue_aware": _hue_aware("floyd_steinberg", serpentine=True),
    "floyd_steinberg_bw":        _bw("floyd_steinberg", serpentine=True),
    "atkinson":                  _plain("atkinson"),
    "atkinson_hue_aware":        _hue_aware("atkinson"),
    "stucki":                    _plain("stucki"),
    "stucki_hue_aware":          _hue_aware("stucki"),
}

FALLBACK_PRESET = "floyd_steinberg_hue_aware"


# UI-only metadata. Kept out of the dataclass so cache_slug() stays stable
# across copy edits and so dataclass equality isn't perturbed by labels.
PRESET_META: dict[str, dict[str, str]] = {
    "floyd_steinberg": {
        "label": "Floyd-Steinberg",
        "description": "Classic error diffusion. Smooth gradients, faithful colours; can show diagonal artefacts on flat areas.",
    },
    "floyd_steinberg_bw": {
        "label": "Floyd-Steinberg (neutral)",
        "description": "Floyd-Steinberg with colour boosting disabled. For B&W photos and near-monochrome images — avoids tinting near-neutral greys pink or yellow.",
    },
    "floyd_steinberg_hue_aware": {
        "label": "Floyd-Steinberg (hue-aware)",
        "description": "Floyd-Steinberg with a hue-constrained palette LUT, adaptive saturation + adaptive vivid. Punchier reds/blues without bleeding into other inks.",
    },
    "atkinson": {
        "label": "Atkinson",
        "description": "Original Mac dithering. High contrast with crisp edges; mid-tones can clip but the look is bold.",
    },
    "atkinson_hue_aware": {
        "label": "Atkinson (hue-aware)",
        "description": "Default. Atkinson with hue-constrained LUT and adaptive saturation/vivid. Bold output with believable colours.",
    },
    "stucki": {
        "label": "Stucki",
        "description": "Two-row diffusion (similar to Jarvis). Smoother than Floyd-Steinberg, slower per pixel.",
    },
    "stucki_hue_aware": {
        "label": "Stucki (hue-aware)",
        "description": "Stucki with hue-constrained LUT and adaptive saturation/vivid.",
    },
}
