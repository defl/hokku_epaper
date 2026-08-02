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
        # A monochrome image carries all its structure in luminance, so it can
        # take a harder local-contrast push than a colour photo before the
        # result looks processed.
        clahe_clip_limit=2.25,
        # Saturation boosting is off above; leave the ceiling at neutral so the
        # value doesn't read as if a boost were configured.
        saturate_max_enhance=1.0,
    )


def _face(algorithm: AlgorithmName = "atkinson", serpentine: bool = False) -> ImageConfig:
    """Tuning for photos with detected faces.

    Skin is the least forgiving subject on a 6-colour panel: aggressive local
    contrast turns cheeks blotchy and a hard unsharp halo reads as a bad
    print. So relative to the default pipeline this pulls CLAHE well down and
    trades it for a slightly wider, stronger unsharp mask, which sharpens
    features (eyes, hairline) without amplifying skin texture.
    """
    return replace(
        _hue_aware(algorithm, serpentine),
        clahe_clip_limit=1.25,
        prepare_usm_amount=130,
        prepare_usm_radius=1.2,
    )


PRESET_IMAGE_CONFIGS: dict[str, ImageConfig] = {
    "floyd_steinberg_hue_aware": _hue_aware("floyd_steinberg", serpentine=True),
    "floyd_steinberg_bw": _bw("floyd_steinberg", serpentine=True),
    "atkinson_hue_aware": _hue_aware("atkinson"),
}

FALLBACK_PRESET = "floyd_steinberg_hue_aware"


# Per-pipeline defaults for a fresh install: what the classifier dispatches to
# for an ordinary photo, a black-and-white one, and one with faces in it.
#
# These are deliberately NOT all entries of PRESET_IMAGE_CONFIGS. That dict is
# the UI dropdown — a short list of general-purpose starting points. The face
# pipeline wants tuning (gentle CLAHE, stronger USM) that is right for skin and
# wrong as a general-purpose choice, so it lives here instead of being forced
# onto the "Atkinson (hue-aware)" preset that users pick by hand.
#
# The dither settings below are measured, not chosen: each is the best scoring
# combination of algorithm x LUT x saturation space x DRC space x adaptive-vivid
# x serpentine over the test corpus, per pipeline. Method, numbers and the
# things that did NOT work are in docs/dither_search.md. Only those six
# dimensions were swept — the tonal chain (CLAHE, unsharp, gamma) is unchanged
# and still hand-tuned.


def _general_default() -> ImageConfig:
    """Ordinary photos: Atkinson, with saturation and DRC in OKLAB.

    Atkinson over Floyd-Steinberg is the larger part of the win; moving the
    adaptive-saturation and dynamic-range-compression stages into OKLAB is the
    rest, and shows up mainly as less colour bleeding into near-neutral areas
    (measured neutral_leak 19.6 -> 16.4).
    """
    return replace(
        _hue_aware("atkinson", serpentine=True),
        adaptive_saturate_space="oklab",
        drc_l_space="oklab",
        drc_chroma_space="oklab",
    )


def _bw_default() -> ImageConfig:
    """Black-and-white photos: Atkinson, DRC in OKLAB.

    Saturation stays off, as _bw() sets it: with the two-ink LUT the saturation
    space cannot change which ink is picked, so the top scorers differed by
    less than 0.01 and "off" is the honest description of what happens.
    """
    return replace(
        _bw("atkinson", serpentine=True),
        drc_l_space="oklab",
        drc_chroma_space="oklab",
    )


def _face_default() -> ImageConfig:
    """Faces: Atkinson, and both chroma boosters off.

    Skin and lips are where boosting hurts. Turning adaptive saturation and
    adaptive vivid off measured best by a clear margin and cut blue ink in
    saturated lips by about two thirds (lips_blue 2.9 % -> 0.9 %). DRC stays in
    CIELAB here — OKLAB won for the other two pipelines but scored worse on
    faces, which is why the spaces are set per pipeline rather than globally.
    """
    return replace(
        _face("atkinson", serpentine=True),
        adaptive_saturate_space="off",
        adaptive_vivid=False,
    )


DEFAULT_IMAGE_CONFIG: ImageConfig = _general_default()
DEFAULT_BW_IMAGE_CONFIG: ImageConfig = _bw_default()
DEFAULT_FACE_IMAGE_CONFIG: ImageConfig = _face_default()


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
