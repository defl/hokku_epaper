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


def _calibration_raw() -> ImageConfig:
    """Pixel-exact passthrough: every tonal and chromatic stage neutralised.

    Intended for images that are already authored in the panel's own inks at
    exact canvas dimensions — the colour-calibration target from
    ``tools/color_target.py`` above all. Every knob here is set to its
    identity value and the dither is ``noop`` (nearest ink, no error
    diffusion), so each source pixel maps to one panel pixel and lands on the
    ink it was painted in. Error diffusion would smear the flat patches; the
    tonal chain would shift them off their anchors.

    Dynamic-range compression still runs — it has no off switch — but it maps
    L\\* onto the panel's own black/white anchors, so pixels already sitting
    on a palette entry quantise straight back to it. ``test_color_target``
    pins that behaviour.
    """
    return ImageConfig(
        dither=DitherConfig(
            algorithm="noop",
            lut_name="euclidean",
            serpentine=False,
            hue_cutoff_deg=95.0,
            neutral_chroma=8.0,
        ),
        prepare_autocontrast_cutoff=0.0,
        prepare_gamma=1.0,
        prepare_brightness=1.0,
        prepare_contrast=1.0,
        color_enhance=1.0,
        adaptive_saturate_space="off",
        saturate_max_enhance=1.0,
        saturate_low_chroma_thresh=5.0,
        saturate_high_chroma_thresh=15.0,
        saturate_low_chroma_thresh_oklab=0.025,
        saturate_high_chroma_thresh_oklab=0.075,
        scale_chroma=False,
        adaptive_vivid=False,
        vivid_chroma_low=5.0,
        vivid_chroma_high=15.0,
        vivid_chroma_low_oklab=0.025,
        vivid_chroma_high_oklab=0.075,
        drc_l_space="cielab",
        drc_chroma_space="cielab",
        prepare_midtone=1.0,
        clahe_clip_limit=0.0,
        clahe_keepout_feather=0.015,
        prepare_usm_radius=1.0,
        prepare_usm_amount=0,
        dither_noise=0.0,
    )


# Per-pipeline defaults for a fresh install: what the classifier dispatches to
# for an ordinary photo, a black-and-white one, and one with faces in it.
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


# The dropdown catalog. The three shipped defaults come first and are the SAME
# objects as the DEFAULT_* constants above, not copies — so a fresh install
# matches a named entry exactly and the UI says "General (default)" instead of
# "Custom (your edits)", which is what it used to do and which read as though
# someone had already been fiddling.
#
# Keeping them out of here was a deliberate choice originally, on the grounds
# that the face tuning is wrong as a general-purpose starting point. That is
# still true — but it is a reason to describe them accurately, not to hide the
# settings the server actually ships with.
#
# The three hand-picked entries below them remain as alternatives. They are not
# redundant: "Atkinson (hue-aware)" differs from the general default in
# serpentine scan and in doing saturation and DRC in CIELAB rather than OKLAB.
#
# "calibration_raw" is last and is a service preset, not a starting point: it
# neutralises every stage so an image already authored in the panel's own inks
# survives to the glass unchanged. Photos rendered with it band badly.
PRESET_IMAGE_CONFIGS: dict[str, ImageConfig] = {
    "default_general": DEFAULT_IMAGE_CONFIG,
    "default_bw": DEFAULT_BW_IMAGE_CONFIG,
    "default_face": DEFAULT_FACE_IMAGE_CONFIG,
    "floyd_steinberg_hue_aware": _hue_aware("floyd_steinberg", serpentine=True),
    "floyd_steinberg_bw": _bw("floyd_steinberg", serpentine=True),
    "atkinson_hue_aware": _hue_aware("atkinson"),
    "calibration_raw": _calibration_raw(),
}

# No production code falls back to this any more — the strict parser removed the
# last caller. Kept as the canonical "ordinary photo" starting point for tests.
FALLBACK_PRESET = "default_general"


# UI-only metadata. Kept out of the dataclass so cache_slug() stays stable
# across copy edits and so dataclass equality isn't perturbed by labels.
PRESET_META: dict[str, dict[str, str]] = {
    "default_general": {
        "label": "General (default)",
        "description": "What the server ships with for ordinary photos. Atkinson with a hue-constrained palette LUT, serpentine scan, and both adaptive saturation and dynamic-range compression in OKLAB. Not hand-picked: this scored best of every algorithm × LUT × colour-space combination over the test corpus. See docs/dither_search.md.",
    },
    "default_bw": {
        "label": "Black & white (default)",
        "description": "What the server ships with for photos it detects as black-and-white. Restricted to the two neutral inks with colour boosting off, so JPEG noise and film grain cannot be amplified into a pink or yellow cast, plus dynamic-range compression in OKLAB.",
    },
    "default_face": {
        "label": "Faces (default)",
        "description": "What the server ships with for photos containing faces. Local contrast is pulled well down and traded for a wider, stronger unsharp mask — aggressive CLAHE makes cheeks blotchy — and both chroma boosters are off, which measured a two-thirds cut in blue ink landing on saturated lips. Deliberately gentle, so it is a poor general-purpose choice.",
    },
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
    "calibration_raw": {
        "label": "Calibration (raw passthrough)",
        "description": "Service preset. No dithering, no tonal or colour processing — each pixel maps straight to its nearest ink. For the colour-calibration target (tools/color_target.py) and other art already authored in the panel's inks at exact panel size. Photos rendered with this will band badly.",
    },
}
