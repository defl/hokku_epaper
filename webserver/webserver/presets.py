"""Named ImageConfig presets — UI selects one, fields populate, user tweaks.

No magic; these are just canned field values. Each algorithm has a plain
(Euclidean LUT) variant and a `_hue_aware` variant. Hue-aware variants enable
adaptive saturation + adaptive vivid by default since they pair well with the
hue-constrained palette mapping.
"""
from __future__ import annotations

from webserver.dither import DitherConfig
from webserver.image import ImageConfig


def _plain(algorithm: str) -> ImageConfig:
    return ImageConfig(
        dither=DitherConfig(
            algorithm=algorithm,  # type: ignore[arg-type]
            lut_name="euclidean",
            serpentine=False,
            hue_cutoff_deg=95.0,
            neutral_chroma=8.0,
        ),
        prepare_autocontrast_cutoff=0.5,
        prepare_gamma=0.85,
        prepare_brightness=1.0,
        prepare_contrast=1.1,
        prepare_sharpness=1.3,
        color_enhance=1.2,
        use_adaptive_saturate=False,
        saturate_max_enhance=1.0,
        saturate_low_chroma_thresh=5.0,
        saturate_high_chroma_thresh=15.0,
        scale_chroma=False,
        adaptive_vivid=False,
        vivid_chroma_low=5.0,
        vivid_chroma_high=15.0,
    )


def _hue_aware(algorithm: str) -> ImageConfig:
    return ImageConfig(
        dither=DitherConfig(
            algorithm=algorithm,  # type: ignore[arg-type]
            lut_name="hue_aware",
            serpentine=False,
            hue_cutoff_deg=95.0,
            neutral_chroma=8.0,
        ),
        prepare_autocontrast_cutoff=0.5,
        prepare_gamma=0.85,
        prepare_brightness=1.0,
        prepare_contrast=1.1,
        prepare_sharpness=1.3,
        color_enhance=1.25,
        use_adaptive_saturate=True,
        saturate_max_enhance=1.25,
        saturate_low_chroma_thresh=5.0,
        saturate_high_chroma_thresh=15.0,
        scale_chroma=False,
        adaptive_vivid=True,
        vivid_chroma_low=5.0,
        vivid_chroma_high=15.0,
    )


PRESET_IMAGE_CONFIGS: dict[str, ImageConfig] = {
    "floyd_steinberg":           _plain("floyd_steinberg"),
    "floyd_steinberg_hue_aware": _hue_aware("floyd_steinberg"),
    "atkinson":                  _plain("atkinson"),
    "atkinson_hue_aware":        _hue_aware("atkinson"),
    "stucki":                    _plain("stucki"),
    "stucki_hue_aware":          _hue_aware("stucki"),
}

DEFAULT_PRESET = "atkinson_hue_aware"


# UI-only metadata. Kept out of the dataclass so cache_slug() stays stable
# across copy edits and so dataclass equality isn't perturbed by labels.
PRESET_META: dict[str, dict[str, str]] = {
    "floyd_steinberg": {
        "label": "Floyd-Steinberg",
        "description": "Classic error diffusion. Smooth gradients, faithful colours; can show diagonal artefacts on flat areas.",
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
