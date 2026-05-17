"""Unit tests for the image processing pipeline (image.py).

Fast tests (always run):
  - compress_dynamic_range: L* remapping, chroma passthrough modes
  - _apply_prepare_enhancements: each knob demonstrably moves pixels
  - _is_near_grayscale: chroma detection edge cases
  - _bw_safe_image_config: correct fields zeroed, others untouched
  - render_preview_png: different configs produce different output
  - Letterboxing: portrait source on landscape canvas, landscape on portrait
  - open_image_for_render: RGB output, handles P/RGBA/L mode inputs

Slow tests (marked ``time_intensive``, skipped by default):
  Run with:  pytest -m time_intensive

  For every image in images/test/ and every named ImageConfig variant
  (using the noop ditherer so visual output reflects the enhancement
  pipeline, not dithering artefacts):
    Writes: build/test_image/<config_name>/<stem>.png
            build/test_image/<config_name>/<stem>_original<ext>
"""
from __future__ import annotations

import shutil
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from hokku_server.display import TOTAL_BYTES
from hokku_server.dither_config import DitherConfig
from hokku_server.dither_streaming import PALETTE_LAB, adaptive_saturate, rgb_to_lab
from hokku_server.dither_streaming_numba import NumbaStreamingDither
from hokku_server.image_abc import _apply_prepare_enhancements
from hokku_server.image_classifier import _is_near_grayscale
from hokku_server.image_config import ImageConfig, _bw_safe_image_config
from hokku_server.image_renderer import ImageRenderer, compress_dynamic_range, open_image_for_render
from hokku_server.presets import PRESET_IMAGE_CONFIGS

from tests._helpers import is_oversize_fixture

# Panel ink L* limits — same derivation as image.py's private constants.
_DISPLAY_BLACK_L = float(PALETTE_LAB[0, 0])
_DISPLAY_WHITE_L = float(PALETTE_LAB[1, 0])


def render_preview_png(img, cfg, orientation, max_side_px=800, crop_to_fill_threshold=0.0):
    return ImageRenderer(NumbaStreamingDither()).render_preview_png(img, cfg, orientation, max_side_px, crop_to_fill_threshold)


# ── shared helpers ────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEST_IMAGES_DIR = _REPO_ROOT / "images" / "test"
_BUILD_IMAGE_DIR = _REPO_ROOT / "build" / "test_image"
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".heic", ".heif"}

# Noop-dither base config — pipeline runs, but the final dither step is a
# nearest-palette quantize with no error diffusion.
_NOOP_DITHER = DitherConfig(
    algorithm="noop",
    lut_name="euclidean",
    serpentine=False,
    hue_cutoff_deg=95.0,
    neutral_chroma=8.0,
)

_BASE_CFG: ImageConfig = replace(
    PRESET_IMAGE_CONFIGS["atkinson"],
    dither=_NOOP_DITHER,
)


def _cfg(**overrides) -> ImageConfig:
    """Return _BASE_CFG with the given field overrides."""
    return replace(_BASE_CFG, **overrides)


def _make_rgb(w: int = 80, h: int = 60, color=(160, 80, 40)) -> Image.Image:
    return Image.new("RGB", (w, h), color)


def _make_gradient(w: int = 200, h: int = 150) -> Image.Image:
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:, :, 0] = np.linspace(20, 230, w, dtype=np.uint8)[None, :]
    arr[:, :, 1] = np.linspace(180, 30, h, dtype=np.uint8)[:, None]
    arr[:, :, 2] = 60
    return Image.fromarray(arr)


def _make_grey(w: int = 80, h: int = 60, level: int = 128) -> Image.Image:
    return Image.new("RGB", (w, h), (level, level, level))


def _mean_brightness(img: Image.Image) -> float:
    return float(np.mean(np.asarray(img.convert("L"), dtype=float)))


def _mean_rgb(img: Image.Image) -> np.ndarray:
    return np.mean(np.asarray(img.convert("RGB"), dtype=float), axis=(0, 1))


def _png_size(png_bytes: bytes) -> tuple[int, int]:
    return Image.open(BytesIO(png_bytes)).size


def _test_images() -> list[Path]:
    if not _TEST_IMAGES_DIR.exists():
        return []
    return sorted(
        p for p in _TEST_IMAGES_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in _IMAGE_EXTS
        and not is_oversize_fixture(p)
    )


# ── fast: compress_dynamic_range ─────────────────────────────────────────────

def test_drc_maps_white_below_display_white():
    """Pure white should map at or below the panel's white L* (rolloff may pull it lower)."""
    white = np.full((1, 1, 3), 255.0, dtype=np.float32)
    out = compress_dynamic_range(
        white, scale_chroma=False, adaptive_vivid=False,
        vivid_chroma_low=5.0, vivid_chroma_high=15.0,
    )
    out_lab = rgb_to_lab(out.astype(np.float64))
    L_out = float(out_lab[0, 0, 0])
    # The rolloff shoulder intentionally maps pure white slightly below display
    # white to avoid hard clipping. Allow up to 5 L* units below display white.
    assert _DISPLAY_WHITE_L - 5.0 <= L_out <= _DISPLAY_WHITE_L + 2.0, (
        f"DRC white L* {L_out:.1f} should be near (or slightly below) display white {_DISPLAY_WHITE_L:.1f}"
    )


def test_drc_maps_black_above_display_black():
    """A pure-black input should map to the panel's black L*, not stay at L*=0."""
    black = np.zeros((1, 1, 3), dtype=np.float32)
    out = compress_dynamic_range(
        black, scale_chroma=False, adaptive_vivid=False,
        vivid_chroma_low=5.0, vivid_chroma_high=15.0,
    )
    out_lab = rgb_to_lab(out.astype(np.float64))
    L_out = float(out_lab[0, 0, 0])
    assert abs(L_out - _DISPLAY_BLACK_L) < 2.0, (
        f"DRC black L* {L_out:.1f} should be near display black {_DISPLAY_BLACK_L:.1f}"
    )


def test_drc_output_in_valid_rgb_range():
    """DRC output must be clipped to [0, 255]."""
    arr = _make_gradient().convert("RGB")
    np_arr = np.asarray(arr, dtype=np.float32)
    out = compress_dynamic_range(
        np_arr, scale_chroma=True, adaptive_vivid=False,
        vivid_chroma_low=5.0, vivid_chroma_high=15.0,
    )
    assert float(out.min()) >= 0.0
    assert float(out.max()) <= 255.0


def test_drc_scale_chroma_reduces_saturation():
    """With scale_chroma=True, a saturated pixel's chroma should be reduced."""
    vivid_red = np.array([[[200.0, 20.0, 20.0]]], dtype=np.float32)
    out_scaled = compress_dynamic_range(
        vivid_red, scale_chroma=True, adaptive_vivid=False,
        vivid_chroma_low=5.0, vivid_chroma_high=15.0,
    )
    out_plain = compress_dynamic_range(
        vivid_red, scale_chroma=False, adaptive_vivid=False,
        vivid_chroma_low=5.0, vivid_chroma_high=15.0,
    )
    lab_scaled = rgb_to_lab(out_scaled.astype(np.float64))
    lab_plain = rgb_to_lab(out_plain.astype(np.float64))
    chroma_scaled = float(np.sqrt(lab_scaled[0, 0, 1] ** 2 + lab_scaled[0, 0, 2] ** 2))
    chroma_plain = float(np.sqrt(lab_plain[0, 0, 1] ** 2 + lab_plain[0, 0, 2] ** 2))
    assert chroma_scaled < chroma_plain, "scale_chroma should reduce chroma"


def test_drc_adaptive_vivid_preserves_more_chroma_than_scale_chroma():
    """adaptive_vivid blends between scale_chroma (low-chroma pixels) and no-scaling
    (high-chroma pixels). So for a saturated pixel it must preserve more chroma than
    plain scale_chroma (which scales all chroma down by c_ratio ≈ 0.6).
    """
    vivid_red = np.array([[[200.0, 20.0, 20.0]]], dtype=np.float32)
    out_vivid = compress_dynamic_range(
        vivid_red, scale_chroma=False, adaptive_vivid=True,
        vivid_chroma_low=5.0, vivid_chroma_high=15.0,
    )
    out_scaled = compress_dynamic_range(
        vivid_red, scale_chroma=True, adaptive_vivid=False,
        vivid_chroma_low=5.0, vivid_chroma_high=15.0,
    )
    lab_vivid = rgb_to_lab(out_vivid.astype(np.float64))
    lab_scaled = rgb_to_lab(out_scaled.astype(np.float64))
    chroma_vivid = float(np.sqrt(lab_vivid[0, 0, 1] ** 2 + lab_vivid[0, 0, 2] ** 2))
    chroma_scaled = float(np.sqrt(lab_scaled[0, 0, 1] ** 2 + lab_scaled[0, 0, 2] ** 2))
    assert chroma_vivid > chroma_scaled, (
        "adaptive_vivid should preserve more chroma than scale_chroma for a saturated pixel"
    )


def test_drc_adaptive_vivid_no_boost_for_neutral():
    """With adaptive_vivid=True, a neutral grey must not get a chroma boost."""
    grey = np.array([[[128.0, 128.0, 128.0]]], dtype=np.float32)
    out_vivid = compress_dynamic_range(
        grey, scale_chroma=False, adaptive_vivid=True,
        vivid_chroma_low=5.0, vivid_chroma_high=15.0,
    )
    out_plain = compress_dynamic_range(
        grey, scale_chroma=False, adaptive_vivid=False,
        vivid_chroma_low=5.0, vivid_chroma_high=15.0,
    )
    # Both outputs should be very close — neutral grey has no chroma to boost.
    assert np.allclose(out_vivid, out_plain, atol=2.0), (
        "adaptive_vivid should not change neutral grey"
    )


# ── fast: _apply_prepare_enhancements ────────────────────────────────────────

def test_brightness_increase_brightens():
    img = _make_rgb(40, 30, (100, 100, 100))
    cfg_bright = _cfg(prepare_brightness=1.5, prepare_contrast=1.0,
                      prepare_usm_amount=0, prepare_gamma=1.0,
                      prepare_autocontrast_cutoff=0.0, color_enhance=1.0)
    cfg_base = _cfg(prepare_brightness=1.0, prepare_contrast=1.0,
                    prepare_usm_amount=0, prepare_gamma=1.0,
                    prepare_autocontrast_cutoff=0.0, color_enhance=1.0)
    out_bright = _apply_prepare_enhancements(img.copy(), cfg_bright)
    out_base = _apply_prepare_enhancements(img.copy(), cfg_base)
    assert _mean_brightness(out_bright) > _mean_brightness(out_base), (
        "Increasing brightness should brighten the image"
    )


def test_brightness_decrease_darkens():
    img = _make_rgb(40, 30, (160, 160, 160))
    cfg_dark = _cfg(prepare_brightness=0.5, prepare_contrast=1.0,
                    prepare_usm_amount=0, prepare_gamma=1.0,
                    prepare_autocontrast_cutoff=0.0, color_enhance=1.0)
    cfg_base = _cfg(prepare_brightness=1.0, prepare_contrast=1.0,
                    prepare_usm_amount=0, prepare_gamma=1.0,
                    prepare_autocontrast_cutoff=0.0, color_enhance=1.0)
    out_dark = _apply_prepare_enhancements(img.copy(), cfg_dark)
    out_base = _apply_prepare_enhancements(img.copy(), cfg_base)
    assert _mean_brightness(out_dark) < _mean_brightness(out_base), (
        "Decreasing brightness should darken the image"
    )


def test_gamma_below_one_brightens_midtones():
    """Gamma < 1 maps mid-grey upward (brightens)."""
    img = _make_rgb(40, 30, (128, 128, 128))
    cfg_gamma = _cfg(prepare_gamma=0.5, prepare_brightness=1.0,
                     prepare_contrast=1.0, prepare_usm_amount=0,
                     prepare_autocontrast_cutoff=0.0, color_enhance=1.0)
    cfg_base = _cfg(prepare_gamma=1.0, prepare_brightness=1.0,
                    prepare_contrast=1.0, prepare_usm_amount=0,
                    prepare_autocontrast_cutoff=0.0, color_enhance=1.0)
    out_gamma = _apply_prepare_enhancements(img.copy(), cfg_gamma)
    out_base = _apply_prepare_enhancements(img.copy(), cfg_base)
    assert _mean_brightness(out_gamma) > _mean_brightness(out_base), (
        "Gamma < 1 should brighten mid-grey"
    )


def test_gamma_above_one_darkens_midtones():
    """Gamma > 1 maps mid-grey downward (darkens)."""
    img = _make_rgb(40, 30, (128, 128, 128))
    cfg_gamma = _cfg(prepare_gamma=2.0, prepare_brightness=1.0,
                     prepare_contrast=1.0, prepare_usm_amount=0,
                     prepare_autocontrast_cutoff=0.0, color_enhance=1.0)
    cfg_base = _cfg(prepare_gamma=1.0, prepare_brightness=1.0,
                    prepare_contrast=1.0, prepare_usm_amount=0,
                    prepare_autocontrast_cutoff=0.0, color_enhance=1.0)
    out_gamma = _apply_prepare_enhancements(img.copy(), cfg_gamma)
    out_base = _apply_prepare_enhancements(img.copy(), cfg_base)
    assert _mean_brightness(out_gamma) < _mean_brightness(out_base), (
        "Gamma > 1 should darken mid-grey"
    )


def test_color_enhance_boosts_saturation():
    """color_enhance > 1 should increase the gap between RGB channels."""
    img = _make_rgb(40, 30, (200, 80, 40))  # warm orange
    cfg_vivid = _cfg(color_enhance=2.0, use_adaptive_saturate=False,
                     prepare_autocontrast_cutoff=0.0, prepare_gamma=1.0,
                     prepare_brightness=1.0, prepare_contrast=1.0, prepare_usm_amount=0)
    cfg_flat = _cfg(color_enhance=1.0, use_adaptive_saturate=False,
                    prepare_autocontrast_cutoff=0.0, prepare_gamma=1.0,
                    prepare_brightness=1.0, prepare_contrast=1.0, prepare_usm_amount=0)
    out_vivid = _apply_prepare_enhancements(img.copy(), cfg_vivid)
    out_flat = _apply_prepare_enhancements(img.copy(), cfg_flat)
    rgb_vivid = _mean_rgb(out_vivid)
    rgb_flat = _mean_rgb(out_flat)
    # Saturation boost increases the spread between R (dominant) and B (weakest).
    spread_vivid = float(rgb_vivid[0] - rgb_vivid[2])
    spread_flat = float(rgb_flat[0] - rgb_flat[2])
    assert spread_vivid > spread_flat, "color_enhance > 1 should increase channel spread"


def test_color_enhance_below_one_desaturates():
    img = _make_rgb(40, 30, (200, 80, 40))
    cfg_grey = _cfg(color_enhance=0.0, use_adaptive_saturate=False,
                    prepare_autocontrast_cutoff=0.0, prepare_gamma=1.0,
                    prepare_brightness=1.0, prepare_contrast=1.0, prepare_usm_amount=0)
    out = _apply_prepare_enhancements(img.copy(), cfg_grey)
    rgb = _mean_rgb(out)
    # At color_enhance=0 the image becomes fully greyscale.
    assert abs(float(rgb[0]) - float(rgb[1])) < 5.0, (
        "color_enhance=0 should produce a near-grey image"
    )


def test_adaptive_saturate_changes_output_vs_color_enhance():
    """use_adaptive_saturate path should produce different output than color_enhance path."""
    img = _make_gradient()
    cfg_adapt = _cfg(use_adaptive_saturate=True, saturate_max_enhance=1.5,
                     saturate_low_chroma_thresh=5.0, saturate_high_chroma_thresh=20.0,
                     prepare_autocontrast_cutoff=0.0, prepare_gamma=1.0,
                     prepare_brightness=1.0, prepare_contrast=1.0, prepare_usm_amount=0)
    cfg_enhance = _cfg(use_adaptive_saturate=False, color_enhance=1.5,
                       prepare_autocontrast_cutoff=0.0, prepare_gamma=1.0,
                       prepare_brightness=1.0, prepare_contrast=1.0, prepare_usm_amount=0)
    out_adapt = np.asarray(_apply_prepare_enhancements(img.copy(), cfg_adapt))
    out_enhance = np.asarray(_apply_prepare_enhancements(img.copy(), cfg_enhance))
    assert not np.array_equal(out_adapt, out_enhance), (
        "adaptive_saturate and color_enhance paths should produce different results"
    )


def test_enhancements_output_is_rgb_image():
    img = _make_rgb()
    out = _apply_prepare_enhancements(img, _BASE_CFG)
    assert out.mode == "RGB"


def test_enhancements_preserves_image_size():
    img = _make_rgb(120, 90)
    out = _apply_prepare_enhancements(img, _BASE_CFG)
    assert out.size == (120, 90)


# ── fast: CLAHE keepout (face protection) ─────────────────────────────────────
#
# The keepout mechanism (in _apply_prepare_enhancements):
#   1. Convert canvas to Lab after autocontrast / gamma / brightness / contrast.
#   2. Save face region L* values (byte-for-byte copy of the uint8 Lab slice).
#   3. Apply CLAHE to the *entire* L* channel (including the face area).
#   4. Restore the saved L* into the face region, overwriting the CLAHE result.
#   5. Convert back to RGB.
#
# Key insight for testing: many pipeline steps (autocontrast, gamma, …) run
# *before* CLAHE and change L* independently of keepout.  Comparing L* on the
# *input* image vs the *output* is therefore meaningless.  All assertions must
# compare two outputs that went through *identical* pipeline steps and only
# differ in whether keepout was active.

def _low_contrast_grey(w: int = 120, h: int = 100) -> Image.Image:
    """Low-contrast grey gradient — every 8×8 CLAHE tile has a real but small
    histogram, guaranteeing a measurable contrast expansion."""
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    for y in range(h):
        v = 85 + int(25 * y / h)   # luminance 85–110
        arr[y, :] = [v, v, v]
    return Image.fromarray(arr)


def _extract_L(img_rgb: Image.Image, x: int, y: int, w: int, h: int) -> np.ndarray:
    """Return the uint8 L* sub-region from an RGB PIL image."""
    arr = np.asarray(img_rgb, dtype=np.uint8)
    lab = cv2.cvtColor(arr, cv2.COLOR_RGB2LAB)
    return lab[y:y + h, x:x + w, 0].copy()


# Strong CLAHE; all other pipeline steps at identity so L* changes are
# solely attributable to CLAHE.  Note: autocontrast is OFF (cutoff=0 DOES
# run but on a near-flat image it is a no-op; the critical thing is that
# both test branches call _apply_prepare_enhancements with the same cfg).
_CLAHE_CFG = _cfg(
    clahe_clip_limit=8.0,
    prepare_autocontrast_cutoff=0.0,
    prepare_gamma=1.0,
    prepare_midtone=1.0,
    prepare_brightness=1.0,
    prepare_contrast=1.0,
    prepare_usm_amount=0,
    color_enhance=1.0,
    use_adaptive_saturate=False,
)


def test_clahe_changes_face_l_when_no_keepout():
    """Sanity check: CLAHE actually changes L* in the face region.

    Compares the face region in output-WITH-keepout vs output-WITHOUT-keepout.
    If CLAHE never changes L* at all, the keepout tests below are vacuous.
    """
    img = _low_contrast_grey()
    fx, fy, fw, fh = 30, 25, 60, 50

    out_with = _apply_prepare_enhancements(img.copy(), _CLAHE_CFG, keepout_bboxes_canvas=[(fx, fy, fw, fh)])
    out_without = _apply_prepare_enhancements(img.copy(), _CLAHE_CFG, keepout_bboxes_canvas=None)

    L_with = _extract_L(out_with, fx, fy, fw, fh)
    L_without = _extract_L(out_without, fx, fy, fw, fh)

    max_delta = int(np.abs(L_with.astype(int) - L_without.astype(int)).max())
    assert max_delta > 5, (
        f"CLAHE must change face-region L* by >5 units (keepout vs no-keepout delta={max_delta}). "
        "If delta is tiny CLAHE is not working and keepout tests are meaningless."
    )


def test_clahe_keepout_face_l_differs_from_no_keepout():
    """Core test: keepout prevents CLAHE from changing the face region's L*.

    Both branches go through *identical* pipeline steps; the only difference is
    whether the face L* slice is restored after CLAHE.  If keepout is working:
      - face L* with-keepout  ≠  face L* without-keepout   (CLAHE was undone)
      - face interior L* with-keepout  ≈  face L* no-clahe  (same as skipping CLAHE)

    The interior (inset by INNER px on each side) is used for the no-clahe
    comparison because Gaussian feathering blends the outer edge pixels — only
    the interior is guaranteed to be fully protected (mask ≈ 1.0).
    """
    img = _low_contrast_grey()
    fx, fy, fw, fh = 30, 25, 60, 50
    face_box = [(fx, fy, fw, fh)]

    out_keepout   = _apply_prepare_enhancements(img.copy(), _CLAHE_CFG, keepout_bboxes_canvas=face_box)
    out_no_keepout = _apply_prepare_enhancements(img.copy(), _CLAHE_CFG, keepout_bboxes_canvas=None)
    out_no_clahe  = _apply_prepare_enhancements(img.copy(), replace(_CLAHE_CFG, clahe_clip_limit=0.0))

    L_keepout    = _extract_L(out_keepout,    fx, fy, fw, fh)
    L_no_keepout = _extract_L(out_no_keepout, fx, fy, fw, fh)

    # keepout should differ from no-keepout — CLAHE changed L* in the no-keepout branch
    delta_vs_no_keepout = int(np.abs(L_keepout.astype(int) - L_no_keepout.astype(int)).max())
    assert delta_vs_no_keepout > 5, (
        f"Face L* with-keepout should differ from without-keepout by >5 "
        f"(got max delta={delta_vs_no_keepout}); keepout does not appear to be working."
    )

    # Interior of face should match no-CLAHE — protected from CLAHE even with feathering.
    # Gaussian feathering blends the edge pixels; at INNER px inside the bbox the mask
    # is ≈1.0 (erf(INNER/(√2·σ)) ≈ 1 for the small σ the 120×100 test canvas gives).
    # Small tolerance (≤2) for uint8 Lab round-trip rounding (RGB→Lab→RGB).
    INNER = 5
    L_keepout_inner = _extract_L(out_keepout,  fx + INNER, fy + INNER, fw - 2 * INNER, fh - 2 * INNER)
    L_no_clahe_inner = _extract_L(out_no_clahe, fx + INNER, fy + INNER, fw - 2 * INNER, fh - 2 * INNER)
    delta_vs_no_clahe = int(np.abs(L_keepout_inner.astype(int) - L_no_clahe_inner.astype(int)).max())
    assert delta_vs_no_clahe <= 2, (
        f"Face interior L* with-keepout should equal no-CLAHE result (±2 for round-trip rounding); "
        f"got max delta={delta_vs_no_clahe}.  The keepout restore is not working correctly."
    )


def test_clahe_keepout_background_identical_to_no_keepout():
    """Pixels *outside* the keepout bbox get the same CLAHE treatment in both branches.

    The keepout implementation runs CLAHE on the full image first, then patches
    only the face slice.  The background pixels must therefore be byte-identical
    between keepout and no-keepout.
    """
    img = _low_contrast_grey()
    fx, fy, fw, fh = 30, 25, 60, 50

    out_keepout    = _apply_prepare_enhancements(img.copy(), _CLAHE_CFG, keepout_bboxes_canvas=[(fx, fy, fw, fh)])
    out_no_keepout = _apply_prepare_enhancements(img.copy(), _CLAHE_CFG, keepout_bboxes_canvas=None)

    # Background pixel clearly outside the face region.
    bg_y, bg_x = 5, 5
    L_bg_keepout    = int(_extract_L(out_keepout,    bg_x, bg_y, 1, 1)[0, 0])
    L_bg_no_keepout = int(_extract_L(out_no_keepout, bg_x, bg_y, 1, 1)[0, 0])

    assert L_bg_keepout == L_bg_no_keepout, (
        f"Background L* must be identical with and without keepout "
        f"(keepout={L_bg_keepout}, no-keepout={L_bg_no_keepout}).  "
        "If keepout incorrectly skips CLAHE entirely, background would also differ."
    )


def test_clahe_keepout_multiple_bboxes():
    """Multiple non-overlapping keepout boxes must each be preserved."""
    img = _low_contrast_grey(w=200, h=100)

    box1 = (10, 10, 40, 40)
    box2 = (130, 10, 40, 40)

    out_keepout    = _apply_prepare_enhancements(img.copy(), _CLAHE_CFG, keepout_bboxes_canvas=[box1, box2])
    out_no_keepout = _apply_prepare_enhancements(img.copy(), _CLAHE_CFG, keepout_bboxes_canvas=None)
    out_no_clahe   = _apply_prepare_enhancements(img.copy(), replace(_CLAHE_CFG, clahe_clip_limit=0.0))

    INNER = 5
    for label, (bx, by, bw, bh) in [("box1", box1), ("box2", box2)]:
        L_keepout    = _extract_L(out_keepout,    bx, by, bw, bh)
        L_no_keepout = _extract_L(out_no_keepout, bx, by, bw, bh)

        assert int(np.abs(L_keepout.astype(int) - L_no_keepout.astype(int)).max()) > 5, (
            f"{label}: keepout should differ from no-keepout (CLAHE not undone)"
        )
        # Check interior only — feathered edges may be partially blended.
        L_keepout_inner  = _extract_L(out_keepout,  bx + INNER, by + INNER, bw - 2 * INNER, bh - 2 * INNER)
        L_no_clahe_inner = _extract_L(out_no_clahe, bx + INNER, by + INNER, bw - 2 * INNER, bh - 2 * INNER)
        assert int(np.abs(L_keepout_inner.astype(int) - L_no_clahe_inner.astype(int)).max()) <= 2, (
            f"{label}: keepout interior should match no-CLAHE result (±2 round-trip tolerance)"
        )


def test_clahe_keepout_no_crash_when_clip_limit_zero():
    """With clahe_clip_limit=0, CLAHE is disabled; keepout boxes must not crash."""
    cfg_no_clahe = replace(_CLAHE_CFG, clahe_clip_limit=0.0)
    img = _low_contrast_grey()
    # Must not raise.
    out_with_boxes    = _apply_prepare_enhancements(img.copy(), cfg_no_clahe, keepout_bboxes_canvas=[(30, 25, 60, 50)])
    out_without_boxes = _apply_prepare_enhancements(img.copy(), cfg_no_clahe, keepout_bboxes_canvas=None)
    # Both should produce the same result — CLAHE path was never entered.
    assert np.array_equal(np.asarray(out_with_boxes), np.asarray(out_without_boxes)), (
        "clahe_clip_limit=0 should produce identical output regardless of keepout boxes"
    )


# ── fast: _is_near_grayscale ──────────────────────────────────────────────────

def test_near_grayscale_pure_grey():
    assert _is_near_grayscale(_make_grey(100, 100, 128))


def test_near_grayscale_white():
    assert _is_near_grayscale(_make_grey(100, 100, 255))


def test_near_grayscale_black():
    assert _is_near_grayscale(_make_grey(100, 100, 0))


def test_near_grayscale_vivid_red():
    assert not _is_near_grayscale(Image.new("RGB", (100, 100), (220, 20, 20)))


def test_near_grayscale_vivid_blue():
    assert not _is_near_grayscale(Image.new("RGB", (100, 100), (20, 50, 200)))


def test_near_grayscale_vivid_green():
    assert not _is_near_grayscale(Image.new("RGB", (100, 100), (20, 180, 30)))


def test_near_grayscale_mixed_mostly_grey():
    """Mostly grey with a small coloured patch should still read as grayscale
    (95th-percentile chroma stays low)."""
    arr = np.full((100, 100, 3), 128, dtype=np.uint8)
    arr[:5, :5] = [220, 20, 20]   # tiny red patch — below the 95th-percentile
    img = Image.fromarray(arr)
    assert _is_near_grayscale(img)


def test_near_grayscale_forest_bw():
    """The BW forest test image should be detected as near-grayscale."""
    bw_path = _TEST_IMAGES_DIR / "Forest_road_Slavne_2017_BW_G9.jpg"
    assert bw_path.exists(), f"Test image missing from repo: {bw_path}"
    with open_image_for_render(bw_path) as img:
        assert _is_near_grayscale(img), "BW forest image should be detected as grayscale"


def test_near_grayscale_colour_photo():
    """A colour photo should not be detected as near-grayscale."""
    colour_path = _TEST_IMAGES_DIR / "Actress_Anna_Unterberger-2.jpg"
    assert colour_path.exists(), f"Test image missing from repo: {colour_path}"
    with open_image_for_render(colour_path) as img:
        assert not _is_near_grayscale(img), "Colour photo should not read as grayscale"


# ── fast: _bw_safe_image_config ───────────────────────────────────────────────

def test_bw_safe_disables_adaptive_saturate():
    cfg = _cfg(use_adaptive_saturate=True)
    safe = _bw_safe_image_config(cfg)
    assert not safe.use_adaptive_saturate


def test_bw_safe_disables_adaptive_vivid():
    cfg = _cfg(adaptive_vivid=True)
    safe = _bw_safe_image_config(cfg)
    assert not safe.adaptive_vivid


def test_bw_safe_disables_scale_chroma():
    cfg = _cfg(scale_chroma=True)
    safe = _bw_safe_image_config(cfg)
    assert not safe.scale_chroma


def test_bw_safe_sets_minimal_color_enhance():
    cfg = _cfg(color_enhance=1.8)
    safe = _bw_safe_image_config(cfg)
    assert safe.color_enhance == pytest.approx(1.05)


def test_bw_safe_preserves_other_fields():
    cfg = _cfg(prepare_brightness=1.3, prepare_gamma=0.8, prepare_contrast=1.2)
    safe = _bw_safe_image_config(cfg)
    assert safe.prepare_brightness == pytest.approx(1.3)
    assert safe.prepare_gamma == pytest.approx(0.8)
    assert safe.prepare_contrast == pytest.approx(1.2)
    assert safe.dither == cfg.dither


# ── fast: adaptive_saturate ───────────────────────────────────────────────────

def test_adaptive_saturate_no_change_on_grey():
    """Neutral grey has near-zero chroma; adaptive_saturate factor ≈ 1.0."""
    grey = np.full((10, 10, 3), 128.0, dtype=np.float64)
    out = adaptive_saturate(grey, max_enhance=2.0, low_thresh=5.0, high_thresh=20.0)
    assert np.allclose(out, grey, atol=2.0)


def test_adaptive_saturate_boosts_colourful_pixels():
    red = np.array([[[200.0, 30.0, 30.0]]], dtype=np.float64)
    out = adaptive_saturate(red, max_enhance=2.0, low_thresh=5.0, high_thresh=20.0)
    lab_in = rgb_to_lab(red)
    lab_out = rgb_to_lab(out.astype(np.float64))
    chroma_in = float(np.sqrt(lab_in[0, 0, 1] ** 2 + lab_in[0, 0, 2] ** 2))
    chroma_out = float(np.sqrt(lab_out[0, 0, 1] ** 2 + lab_out[0, 0, 2] ** 2))
    assert chroma_out > chroma_in, "adaptive_saturate should boost chroma on colourful pixels"


# ── fast: open_image_for_render ───────────────────────────────────────────────

def test_open_image_for_render_rgb_mode(tmp_path):
    p = tmp_path / "test.png"
    Image.new("RGB", (20, 20), (100, 150, 200)).save(p)
    with open_image_for_render(p) as img:
        assert img.mode == "RGB"


def test_open_image_for_render_converts_l_to_rgb(tmp_path):
    p = tmp_path / "grey.png"
    Image.new("L", (20, 20), 128).save(p)
    with open_image_for_render(p) as img:
        assert img.mode == "RGB"


def test_open_image_for_render_converts_rgba_to_rgb(tmp_path):
    p = tmp_path / "rgba.png"
    Image.new("RGBA", (20, 20), (100, 150, 200, 128)).save(p)
    with open_image_for_render(p) as img:
        assert img.mode == "RGB"


def test_open_image_for_render_converts_palette_to_rgb(tmp_path):
    p = tmp_path / "palette.png"
    Image.new("RGB", (20, 20), (80, 120, 160)).convert("P").save(p)
    with open_image_for_render(p) as img:
        assert img.mode == "RGB"


# ── fast: render_preview_png with different configs ───────────────────────────

def test_different_configs_produce_different_preview():
    """Pipeline knobs must actually affect the output pixels."""
    img = _make_gradient(120, 90)
    cfg_bright = _cfg(prepare_brightness=1.8, prepare_autocontrast_cutoff=0.0,
                      prepare_gamma=1.0, prepare_contrast=1.0, prepare_usm_amount=0)
    cfg_dark = _cfg(prepare_brightness=0.4, prepare_autocontrast_cutoff=0.0,
                    prepare_gamma=1.0, prepare_contrast=1.0, prepare_usm_amount=0)
    p_bright = render_preview_png(img.copy(), cfg_bright, "landscape", max_side_px=100)
    p_dark = render_preview_png(img.copy(), cfg_dark, "landscape", max_side_px=100)
    assert p_bright != p_dark, "brightness 1.8 vs 0.4 must produce different output"


def test_render_preview_png_returns_png():
    png = render_preview_png(_make_gradient(), _BASE_CFG, "landscape", max_side_px=100)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_preview_png_landscape_aspect():
    png = render_preview_png(_make_rgb(), _BASE_CFG, "landscape", max_side_px=200)
    w, h = _png_size(png)
    assert w > h


def test_render_preview_png_portrait_aspect():
    png = render_preview_png(_make_rgb(), _BASE_CFG, "portrait", max_side_px=200)
    w, h = _png_size(png)
    assert h > w


def test_hue_aware_config_on_grey_source_renders_without_crash():
    """Hue-aware preset on a grey image renders successfully.

    The auto-bw-safe fallback was removed (dispatch is now opt-in via
    ImageClassifier); render_panel_bytes/render_preview_png honour the cfg given.
    """
    cfg = replace(
        PRESET_IMAGE_CONFIGS["atkinson_hue_aware"],
        dither=_NOOP_DITHER,
    )
    png = render_preview_png(_make_grey(60, 40), cfg, "landscape", max_side_px=100)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_panel_bytes_honours_cfg_without_hidden_override():
    """render_panel_bytes must use the exact cfg it was given — no hidden B&W override.

    Previously a hue-aware cfg on a grey source was silently rewritten to a B&W-safe
    variant. That auto-fallback is gone; ImageClassifier owns the dispatch policy.

    We verify this by rendering the SAME coloured image with hue-aware vs bw-safe
    configs and asserting the outputs differ (proving each cfg was actually honoured).
    """
    def render_panel_bytes(img, cfg, orientation, crop_to_fill_threshold=0.0):
        return ImageRenderer(NumbaStreamingDither()).render_panel_bytes(img, cfg, orientation, crop_to_fill_threshold)

    # A hue-aware cfg that boosts saturation.
    cfg_hue = replace(
        PRESET_IMAGE_CONFIGS["atkinson_hue_aware"],
        dither=_NOOP_DITHER,
    )
    # A bw-safe cfg (no saturation boost).
    cfg_bw = _bw_safe_image_config(cfg_hue)

    # render_panel_bytes consumes the input image (closes the PIL buffer to
    # keep the per-render memory budget honest), so we make a fresh gradient
    # per call.
    out_hue = render_panel_bytes(_make_gradient(60, 40), cfg_hue, "landscape")
    out_bw = render_panel_bytes(_make_gradient(60, 40), cfg_bw, "landscape")

    # Both should produce valid panel output.
    assert len(out_hue) == TOTAL_BYTES
    assert len(out_bw) == TOTAL_BYTES
    # Outputs must differ — proves render_panel_bytes honoured the distinct cfgs.
    assert out_hue != out_bw, (
        "hue-aware and bw-safe configs must produce different outputs on a coloured image"
    )


# ── slow: visual inspection outputs ──────────────────────────────────────────

# Named ImageConfig variants that showcase different pipeline stages.
# All use noop dither so the output reflects the enhancement pipeline, not diffusion.
_SLOW_CONFIGS: dict[str, ImageConfig] = {
    "baseline": _BASE_CFG,

    # ── tonal preparation ──
    "gamma_0.6": _cfg(prepare_gamma=0.6),
    "gamma_1.2": _cfg(prepare_gamma=1.2),
    "brightness_1.5": _cfg(prepare_brightness=1.5),
    "brightness_0.6": _cfg(prepare_brightness=0.6),
    "contrast_1.5": _cfg(prepare_contrast=1.5),
    "contrast_0.6": _cfg(prepare_contrast=0.6),
    "autocontrast_0": _cfg(prepare_autocontrast_cutoff=0.0),
    "autocontrast_2": _cfg(prepare_autocontrast_cutoff=2.0),
    "usm_strong": _cfg(prepare_usm_amount=200, prepare_usm_radius=2.0),
    "usm_off": _cfg(prepare_usm_amount=0),
    "midtone_lift": _cfg(prepare_midtone=1.5),
    "midtone_darken": _cfg(prepare_midtone=0.7),
    "noise_light": _cfg(dither_noise=3.0),

    # ── color enhancement ──
    "color_enhance_1.8": _cfg(color_enhance=1.8, use_adaptive_saturate=False),
    "color_enhance_0.5": _cfg(color_enhance=0.5, use_adaptive_saturate=False),
    "color_enhance_0": _cfg(color_enhance=0.0, use_adaptive_saturate=False),

    # ── adaptive saturation ──
    "adaptive_saturate": _cfg(
        use_adaptive_saturate=True,
        saturate_max_enhance=1.5,
        saturate_low_chroma_thresh=5.0,
        saturate_high_chroma_thresh=20.0,
    ),
    "adaptive_saturate_strong": _cfg(
        use_adaptive_saturate=True,
        saturate_max_enhance=2.0,
        saturate_low_chroma_thresh=3.0,
        saturate_high_chroma_thresh=12.0,
    ),

    # ── dynamic range compression ──
    "scale_chroma": _cfg(scale_chroma=True, adaptive_vivid=False),
    "adaptive_vivid": _cfg(
        adaptive_vivid=True,
        vivid_chroma_low=5.0,
        vivid_chroma_high=15.0,
    ),
    "adaptive_vivid_strong": _cfg(
        adaptive_vivid=True,
        vivid_chroma_low=2.0,
        vivid_chroma_high=8.0,
    ),

    # ── combined presets (hue-aware LUT, noop dither) ──
    "atkinson_hue_aware_noop": replace(
        PRESET_IMAGE_CONFIGS["atkinson_hue_aware"],
        dither=_NOOP_DITHER,
    ),
    "stucki_hue_aware_noop": replace(
        PRESET_IMAGE_CONFIGS["stucki_hue_aware"],
        dither=_NOOP_DITHER,
    ),

    # ── extremes ──
    "all_boost": _cfg(
        prepare_gamma=0.7,
        prepare_brightness=1.2,
        prepare_contrast=1.4,
        prepare_usm_amount=320,
        color_enhance=1.8,
        use_adaptive_saturate=False,
        adaptive_vivid=True,
        vivid_chroma_low=5.0,
        vivid_chroma_high=15.0,
    ),
    "neutral": _cfg(
        prepare_autocontrast_cutoff=0.0,
        prepare_gamma=1.0,
        prepare_brightness=1.0,
        prepare_contrast=1.0,
        prepare_usm_amount=0,
        color_enhance=1.0,
        use_adaptive_saturate=False,
        scale_chroma=False,
        adaptive_vivid=False,
    ),
}


def _slow_params():
    imgs = _test_images()
    cfgs = list(_SLOW_CONFIGS)
    return [(img, cfg_name) for img in imgs for cfg_name in cfgs]


def _slow_ids():
    return [f"{img.stem}__{cfg}" for img, cfg in _slow_params()]


@pytest.mark.time_intensive
@pytest.mark.parametrize("src,cfg_name", _slow_params(), ids=_slow_ids())
def test_image_pipeline_visual(src: Path, cfg_name: str):
    """Render src with cfg_name using noop dither; write PNG to build/test_image/.

    Output layout (flat):
      build/test_image/<stem>__<cfg_name>.png   — processed preview
      build/test_image/<stem>_original<ext>     — source copy (written once)
    """
    _BUILD_IMAGE_DIR.mkdir(parents=True, exist_ok=True)

    original_dest = _BUILD_IMAGE_DIR / f"{src.stem}_original{src.suffix}"
    if not original_dest.exists():
        shutil.copy2(src, original_dest)

    cfg = _SLOW_CONFIGS[cfg_name]
    with open_image_for_render(src) as img:
        png_bytes = render_preview_png(img, cfg, "landscape", max_side_px=800)

    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    (_BUILD_IMAGE_DIR / f"{src.stem}__{cfg_name}.png").write_bytes(png_bytes)

    w, h = _png_size(png_bytes)
    assert max(w, h) <= 800
    assert w > h, f"{src.name}/{cfg_name}: expected landscape aspect, got {w}x{h}"
