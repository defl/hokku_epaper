#!/usr/bin/env python3
"""The measurement-derived corrections, as arms that can be switched off.

Three separate products came out of a colour campaign, and the render pipeline
applies all three at once:

  * ``drc``     — ``display.drc_anchor_l``, the panel's measured reachable L*
                  range, which the dynamic-range compressor squeezes into.
  * ``lut``     — ``display.correction_lut_path``, the 3-D RGB->RGB gamut
                  correction built by ``color_lut_build.py``.
  * ``palette`` — the six ink colours themselves, recomputed from the campaign's
                  own reflectance spectra instead of the shipped table (whose
                  provenance is a third party's file; see ``findings.md``).

"Applying the measurements makes it look worse" is therefore a claim about the
SUM of three things. It cannot be acted on until it is known which one is doing
the damage, so every arm here is independently switchable and each is rendered
through the real pipeline — no reimplementation of the renderer, so an arm
cannot drift from what the server would actually produce.

Nothing outside these three toggles changes between arms. The tonal chain
(autocontrast, gamma, CLAHE, unsharp, saturation) is hand-tuned, not measured,
so it is held fixed on every arm; letting it vary would confound the answer.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
from PIL import Image

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

# _variant_display carries two hard-won requirements (a distinct model_id, and
# registry membership, or the palette LUT cache silently hands back the base
# panel's LUT). Imported rather than reimplemented so those stay in one place.
from ab_session import _variant_display
from color_model import INK_NAMES, load_records
from color_under_illuminant import ambient_palette_srgb, cmfs_at, illuminant_at, ink_spectra
from hokku.webserver.dither_streaming import StreamingDither, rgb_to_lab
from hokku.webserver.image_renderer import ImageRenderer
from hokku.webserver.orientation import Orientation

TOGGLES = ("drc", "lut", "palette", "gmap", "wlut", "nchroma", "nblack")

# "all" stays the three campaign corrections. clamp and wlut are candidate FIXES
# rather than measurement products, so folding them into "all" would quietly
# change what every earlier comparison meant.
ARM_ALIASES = {
    "none": frozenset(),
    "all": frozenset({"drc", "lut", "palette"}),
}

# Built by tools/gamut_clamp.py. Its --mode decides the strategy; "cusp" is the
# one that survived glass, "chroma" is kept only so the failure stays reproducible.
GAMUT_LUT_PATH = Path("build/camcal/clamp_lut.npy")

# The shipped alternative to "hue_aware": identical hue gating, but nearest-ink
# distance weighted 2L^2 + a^2 + b^2 instead of unweighted. Tried before writing
# anything new, because measured black ink is chromatic (C* 15.6) and the
# unweighted metric spends lighter inks cancelling a tint nobody can see at
# L* 15 — which is where the panel's black end went.
WEIGHTED_LUT_NAME = "hue_aware_weighted"

# The hue gate exempts palette entries whose chroma is below
# ``neutral_chroma`` — they may be chosen for a pixel of any hue. It ships at
# 8, which was right when black ink was assumed to be [2,2,2] with chroma 0.
# Measured black on this glass has chroma 15.5, so it stops being exempt, and
# once error diffusion swings the residual to black's hue opposite the gate
# FORBIDS black outright. Green (L* 31.6, three times lighter) is then the
# best legal choice, which is where the shadows went. 18 puts measured black
# and white back inside the exemption and leaves the four chromatic inks gated.
NEUTRAL_CHROMA_MEASURED = 18.0


def campaign_data_path(model: str) -> Path:
    """Where a model's committed campaign dataset lives."""
    return Path("docs/screens") / model / "measurements" / "data" / "campaign.jsonl"


def parse_arm(name: str) -> frozenset[str]:
    """'none' | 'all' | '+'-joined subset of TOGGLES -> the toggle set."""
    key = name.strip().lower()
    if key in ARM_ALIASES:
        return ARM_ALIASES[key]
    parts = {p.strip() for p in key.split("+") if p.strip()}
    unknown = parts - set(TOGGLES)
    if unknown:
        raise ValueError(f"unknown correction(s) {sorted(unknown)} — choices: {list(TOGGLES)}")
    return frozenset(parts)


def arm_label(toggles: frozenset[str]) -> str:
    if not toggles:
        return "none"
    if toggles == frozenset(TOGGLES):
        return "all"
    return "+".join(t for t in TOGGLES if t in toggles)


def measured_palette_d65(data_path: Path) -> np.ndarray:
    """The six inks' sRGB appearance under D65, from the campaign's own spectra.

    Deliberately a corresponding colour rather than raw measured XYZ: the dither
    reasons in sRGB/CIELAB, which carry an implicit D65 viewer. See
    ``color_under_illuminant.ambient_palette_srgb``.
    """
    records = load_records(data_path)
    wl, inks = ink_spectra(records)
    return ambient_palette_srgb(inks, INK_NAMES, illuminant_at("D65", wl), cmfs_at(wl))


def neutralise_black(palette: np.ndarray) -> np.ndarray:
    """Keep measured black's LIGHTNESS, discard its cast. Returns a new palette.

    Measured black is blue-purple (a* +8.6, b* -12.9). Told that, the dither does
    the arithmetically correct thing for a dark NEUTRAL: it mixes in green, whose
    a*/b* are roughly opposite, until the average is neutral again. Measured: 41 %
    green in the darkest pixels, and since green is L* 31.6 against black's 10.9
    the shadows lift by 9 L*.

    That is a bad trade. Chroma discrimination at L* 15 is very poor and lightness
    discrimination is not, so spending a third of the panel's dynamic range to
    cancel a tint nobody can see is the wrong way round.

    So this keeps the part of the measurement that matters — black's real
    lightness, which the DRC and the tone response depend on — and drops the part
    that only causes the dither to fight itself. It is deliberately a half-truth
    about the ink, not a correction of it.
    """
    out = np.array(palette, dtype=np.float32, copy=True)
    lab = np.asarray(rgb_to_lab(out[:1]))[0]
    grey = np.stack([np.arange(256, dtype=np.float32)] * 3, axis=-1)
    greys_l = np.asarray(rgb_to_lab(grey))[:, 0]
    out[0] = grey[int(np.argmin(np.abs(greys_l - lab[0])))]
    return out


def arm_display(base, toggles: frozenset[str], data_path: Path | None = None):
    """A Display with exactly the requested corrections active.

    Always a variant, never the base object, even for the full-corrections arm:
    mutating the registered Display would leak the arm into every later render in
    the process, and the LUT caches key on model_id, so an arm that reused the
    base id would be served the base panel's cached LUT.
    """
    label = arm_label(toggles)
    palette = base.palette_measured_rgb
    if "palette" in toggles:
        path = data_path or campaign_data_path(base.model_id)
        if not path.exists():
            raise SystemExit(f"no campaign dataset at {path} — cannot build the measured palette")
        palette = measured_palette_d65(path)
    if "nblack" in toggles:
        palette = neutralise_black(palette)

    if "lut" in toggles and "gmap" in toggles:
        raise SystemExit("'lut' and 'clamp' both occupy correction_lut_path — pick one per arm")

    var = _variant_display(base, f"arm_{label}", palette)
    var.drc_anchor_l = base.drc_anchor_l if "drc" in toggles else None
    var.correction_lut_path = base.correction_lut_path if "lut" in toggles else None
    if "gmap" in toggles:
        if not GAMUT_LUT_PATH.exists():
            raise SystemExit(f"no gamut-map LUT at {GAMUT_LUT_PATH} — run tools/gamut_clamp.py")
        var.correction_lut_path = GAMUT_LUT_PATH
    return var


def arm_config(cfg, toggles: frozenset[str]):
    """The ImageConfig for an arm — the base unless 'wlut' or 'nchroma' is set."""
    dither = cfg.dither
    if "wlut" in toggles:
        dither = replace(dither, lut_name=WEIGHTED_LUT_NAME)
    if "nchroma" in toggles:
        dither = replace(dither, neutral_chroma=NEUTRAL_CHROMA_MEASURED)
    return cfg if dither is cfg.dither else replace(cfg, dither=dither)


def render_arm(display, img: Image.Image, cfg, canvas_w: int, canvas_h: int) -> np.ndarray:
    """Render through the production renderer at the given canvas size.

    Returns a palette-index raster shaped (canvas_h, canvas_w) — panel memory
    when called with the panel's own dimensions.
    """
    renderer = ImageRenderer(dither=StreamingDither(display), display=display)
    return renderer.render_indices(img.copy(), cfg, Orientation.LANDSCAPE, canvas_w, canvas_h)


def to_visible(idx: np.ndarray, display) -> np.ndarray:
    """Panel-memory raster -> the orientation a viewer sees."""
    return np.rot90(idx, k=1) if display.panel_rotated else idx


def to_panel(idx: np.ndarray, display) -> np.ndarray:
    """Inverse of ``to_visible``."""
    return np.rot90(idx, k=3) if display.panel_rotated else idx
