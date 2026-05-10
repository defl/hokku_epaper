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

import numpy as np
import pytest
from PIL import Image

from webserver.dither import PALETTE_LAB, adaptive_saturate, rgb_to_lab
from webserver.dither_config import DitherConfig

# Panel ink L* limits — same derivation as image.py's private constants.
_DISPLAY_BLACK_L = float(PALETTE_LAB[0, 0])
_DISPLAY_WHITE_L = float(PALETTE_LAB[1, 0])
from webserver.image import (
    _apply_prepare_enhancements,
    _bw_safe_image_config,
    _is_near_grayscale,
    compress_dynamic_range,
    open_image_for_render,
    render_preview_png,
)
from webserver.image_config import ImageConfig
from webserver.presets import PRESET_IMAGE_CONFIGS


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
    )


# ── fast: compress_dynamic_range ─────────────────────────────────────────────

def test_drc_maps_white_below_display_white():
    """A pure-white input should map to the panel's white L*, not stay at L*=100."""
    white = np.full((1, 1, 3), 255.0, dtype=np.float32)
    out = compress_dynamic_range(
        white, scale_chroma=False, adaptive_vivid=False,
        vivid_chroma_low=5.0, vivid_chroma_high=15.0,
    )
    out_lab = rgb_to_lab(out.astype(np.float64))
    L_out = float(out_lab[0, 0, 0])
    assert abs(L_out - _DISPLAY_WHITE_L) < 2.0, (
        f"DRC white L* {L_out:.1f} should be near display white {_DISPLAY_WHITE_L:.1f}"
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
                      prepare_sharpness=1.0, prepare_gamma=1.0,
                      prepare_autocontrast_cutoff=0.0, color_enhance=1.0)
    cfg_base = _cfg(prepare_brightness=1.0, prepare_contrast=1.0,
                    prepare_sharpness=1.0, prepare_gamma=1.0,
                    prepare_autocontrast_cutoff=0.0, color_enhance=1.0)
    out_bright = _apply_prepare_enhancements(img.copy(), cfg_bright)
    out_base = _apply_prepare_enhancements(img.copy(), cfg_base)
    assert _mean_brightness(out_bright) > _mean_brightness(out_base), (
        "Increasing brightness should brighten the image"
    )


def test_brightness_decrease_darkens():
    img = _make_rgb(40, 30, (160, 160, 160))
    cfg_dark = _cfg(prepare_brightness=0.5, prepare_contrast=1.0,
                    prepare_sharpness=1.0, prepare_gamma=1.0,
                    prepare_autocontrast_cutoff=0.0, color_enhance=1.0)
    cfg_base = _cfg(prepare_brightness=1.0, prepare_contrast=1.0,
                    prepare_sharpness=1.0, prepare_gamma=1.0,
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
                     prepare_contrast=1.0, prepare_sharpness=1.0,
                     prepare_autocontrast_cutoff=0.0, color_enhance=1.0)
    cfg_base = _cfg(prepare_gamma=1.0, prepare_brightness=1.0,
                    prepare_contrast=1.0, prepare_sharpness=1.0,
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
                     prepare_contrast=1.0, prepare_sharpness=1.0,
                     prepare_autocontrast_cutoff=0.0, color_enhance=1.0)
    cfg_base = _cfg(prepare_gamma=1.0, prepare_brightness=1.0,
                    prepare_contrast=1.0, prepare_sharpness=1.0,
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
                     prepare_brightness=1.0, prepare_contrast=1.0, prepare_sharpness=1.0)
    cfg_flat = _cfg(color_enhance=1.0, use_adaptive_saturate=False,
                    prepare_autocontrast_cutoff=0.0, prepare_gamma=1.0,
                    prepare_brightness=1.0, prepare_contrast=1.0, prepare_sharpness=1.0)
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
                    prepare_brightness=1.0, prepare_contrast=1.0, prepare_sharpness=1.0)
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
                     prepare_brightness=1.0, prepare_contrast=1.0, prepare_sharpness=1.0)
    cfg_enhance = _cfg(use_adaptive_saturate=False, color_enhance=1.5,
                       prepare_autocontrast_cutoff=0.0, prepare_gamma=1.0,
                       prepare_brightness=1.0, prepare_contrast=1.0, prepare_sharpness=1.0)
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
    if not bw_path.exists():
        pytest.skip("BW test image not present")
    with open_image_for_render(bw_path) as img:
        assert _is_near_grayscale(img), "BW forest image should be detected as grayscale"


def test_near_grayscale_colour_photo():
    """A colour photo should not be detected as near-grayscale."""
    colour_path = _TEST_IMAGES_DIR / "Actress_Anna_Unterberger-2.jpg"
    if not colour_path.exists():
        pytest.skip("Colour test image not present")
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
                      prepare_gamma=1.0, prepare_contrast=1.0, prepare_sharpness=1.0)
    cfg_dark = _cfg(prepare_brightness=0.4, prepare_autocontrast_cutoff=0.0,
                    prepare_gamma=1.0, prepare_contrast=1.0, prepare_sharpness=1.0)
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
    from webserver.image import render_panel_bytes
    from webserver.display import TOTAL_BYTES

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
    "sharpness_2.0": _cfg(prepare_sharpness=2.0),
    "sharpness_0.5": _cfg(prepare_sharpness=0.5),

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
        prepare_sharpness=1.8,
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
        prepare_sharpness=1.0,
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
