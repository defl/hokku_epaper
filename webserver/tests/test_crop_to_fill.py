"""Tests for the crop-to-fill letterbox-removal feature.

Unit tests (always fast — synthetic images only):
  - Threshold 0 always letterboxes.
  - Threshold high enough causes cover+crop — no white rows/columns.
  - Threshold just below the required zoom still letterboxes.
  - Perfect-fit images are unchanged at any threshold.
  - Output dimensions are exactly the panel canvas dimensions.
  - crop_to_fill_threshold is included in ScreenImageConfig.cache_slug().
  - AppConfig.crop_to_fill_threshold round-trips through to_dict/from_dict.
  - crop_to_fill_threshold is in AppConfig.cache_slug().

Slow visual test (marked time_intensive):
  - Renders every image in images/test/ at threshold 0, default-feeling 0.02,
    and 0.5 for both orientations, outputting PNGs to build/test_letterbox/.
"""
from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from webserver.app_config import AppConfig
from webserver.dither_config import DitherConfig
from webserver.image import _render_indices
from webserver.image_config import ImageConfig
from webserver.presets import DEFAULT_PRESET, PRESET_IMAGE_CONFIGS
from webserver.screen_image_config import ScreenImageConfig


# ── helpers ───────────────────────────────────────────────────────────────────

def _noop_cfg() -> ImageConfig:
    base = PRESET_IMAGE_CONFIGS[DEFAULT_PRESET]
    return replace(
        base,
        prepare_autocontrast_cutoff=0.0,
        prepare_gamma=1.0,
        prepare_brightness=1.0,
        prepare_contrast=1.0,
        prepare_sharpness=1.0,
        color_enhance=1.0,
        use_adaptive_saturate=False,
        adaptive_vivid=False,
        scale_chroma=False,
        dither=DitherConfig(
            algorithm="noop",
            lut_name="euclidean",
            serpentine=False,
            hue_cutoff_deg=95.0,
            neutral_chroma=8.0,
        ),
    )


def _solid_rgb(w: int, h: int, color=(200, 100, 50)) -> Image.Image:
    """Solid-colour image — easy to detect white padding vs. actual content."""
    img = Image.new("RGB", (w, h), color)
    return img


def _has_white_column(indices: np.ndarray) -> bool:
    """True if any column is entirely palette index 1 (white)."""
    WHITE = 1
    return bool(np.any(np.all(indices == WHITE, axis=0)))


def _has_white_row(indices: np.ndarray) -> bool:
    """True if any row is entirely palette index 1 (white)."""
    WHITE = 1
    return bool(np.any(np.all(indices == WHITE, axis=1)))


def _render(img, orientation, canvas_w, canvas_h, threshold):
    return _render_indices(img, _noop_cfg(), orientation, canvas_w, canvas_h, threshold)


# ── output-dimension tests ────────────────────────────────────────────────────

@pytest.mark.parametrize("orientation,canvas_w,canvas_h", [
    ("portrait",  100, 133),
    ("landscape", 133, 100),
])
def test_output_dims_are_canvas_dims(orientation, canvas_w, canvas_h):
    """Result shape must always equal canvas dimensions regardless of threshold."""
    img = _solid_rgb(80, 80)  # square on a non-square canvas
    for threshold in (0.0, 0.5):
        result = _render_indices(img, _noop_cfg(), orientation, canvas_w, canvas_h, threshold)
        assert result.shape == (canvas_h, canvas_w), (
            f"orientation={orientation}, threshold={threshold}: "
            f"got {result.shape}, want ({canvas_h}, {canvas_w})"
        )


# ── letterbox behaviour (threshold=0) ────────────────────────────────────────

def test_zero_threshold_produces_white_bands_for_non_fitting_image():
    """threshold=0 should produce white bands when aspect ratios differ."""
    # 100×50 image on 100×100 canvas: will have top/bottom white bands (portrait).
    img = _solid_rgb(100, 50)
    result = _render_indices(img, _noop_cfg(), "portrait", 100, 100, 0.0)
    assert _has_white_row(result), "Expected horizontal white bands with threshold=0"


def test_zero_threshold_no_crop():
    """threshold=0 must letterbox even when a tiny zoom would eliminate bands."""
    # 98×100 image on 100×100 canvas: only 2% zoom needed.
    img = _solid_rgb(98, 100)
    result = _render_indices(img, _noop_cfg(), "portrait", 100, 100, 0.0)
    assert _has_white_column(result), "Expected vertical white bands with threshold=0"


# ── crop-to-fill behaviour ────────────────────────────────────────────────────

def test_sufficient_threshold_removes_white_bands():
    """When zoom ≤ threshold, no white padding should remain."""
    # 80×100 image on 100×100 canvas (portrait): zoom needed = 100/80 / 1 - 1 = 25%.
    # With threshold=0.3, bands should be eliminated.
    img = _solid_rgb(80, 100)
    result = _render_indices(img, _noop_cfg(), "portrait", 100, 100, 0.30)
    assert not _has_white_row(result),    "No white rows expected after crop-to-fill"
    assert not _has_white_column(result), "No white columns expected after crop-to-fill"


def test_threshold_just_below_zoom_still_letterboxes():
    """threshold just below the required zoom must fall back to letterbox."""
    # 100×80 image on 100×100 canvas (portrait):
    # scale_fit = min(100/100, 100/80) = 1.0; scale_cover = max(100/100, 100/80) = 1.25
    # zoom_ratio = 1.25 - 1 = 0.25
    img = _solid_rgb(100, 80)
    result = _render_indices(img, _noop_cfg(), "portrait", 100, 100, 0.24)
    assert _has_white_row(result), "Should still letterbox when threshold < zoom_ratio"


def test_threshold_at_exact_zoom_crops():
    """threshold exactly equal to zoom_ratio should trigger crop-to-fill."""
    # Same setup: zoom_ratio = 0.25
    img = _solid_rgb(100, 80)
    result = _render_indices(img, _noop_cfg(), "portrait", 100, 100, 0.25)
    assert not _has_white_row(result),    "Should crop-to-fill when threshold == zoom_ratio"
    assert not _has_white_column(result), "Should crop-to-fill when threshold == zoom_ratio"


def test_landscape_orientation_crop():
    """Crop-to-fill works for landscape orientation too.

    For landscape, visible_w = canvas_h and visible_h = canvas_w (the image
    is composed in that pre-rotation space, then rotated -90°).

    canvas=(133,100) → visible_w=100, visible_h=133.
    Image 97×133: scale_fit = min(100/97, 133/133) = 1.0,
                  scale_cover = max(100/97, 133/133) = 1.031, zoom ≈ 3.1%.
    threshold=0.04 > 0.031 → crop eliminates the ~3 % column bands.
    """
    img = _solid_rgb(97, 133)
    result = _render_indices(img, _noop_cfg(), "landscape", 133, 100, 0.04)
    # Result shape = (canvas_h=100, canvas_w=133)
    assert result.shape == (100, 133)
    assert not _has_white_row(result)
    assert not _has_white_column(result)


# ── perfect-fit image ─────────────────────────────────────────────────────────

def test_perfect_fit_no_bands_at_zero_threshold():
    """Image with same aspect ratio as canvas — no white bands at threshold=0."""
    img = _solid_rgb(100, 100)
    result = _render_indices(img, _noop_cfg(), "portrait", 100, 100, 0.0)
    assert not _has_white_row(result)
    assert not _has_white_column(result)


# ── ScreenImageConfig cache_slug ─────────────────────────────────────────────

def test_screen_cfg_slug_changes_with_threshold():
    ic = _noop_cfg()
    a = ScreenImageConfig(image_config=ic, orientation="portrait", crop_to_fill_threshold=0.0)
    b = ScreenImageConfig(image_config=ic, orientation="portrait", crop_to_fill_threshold=0.02)
    assert a.cache_slug() != b.cache_slug()


def test_screen_cfg_slug_stable():
    ic = _noop_cfg()
    cfg = ScreenImageConfig(image_config=ic, orientation="landscape", crop_to_fill_threshold=0.05)
    assert cfg.cache_slug() == cfg.cache_slug()


# ── AppConfig round-trip ──────────────────────────────────────────────────────

def test_app_config_crop_threshold_default():
    assert AppConfig().crop_to_fill_threshold == 0.0


def test_app_config_crop_threshold_roundtrip():
    cfg = AppConfig(crop_to_fill_threshold=0.07)
    restored = AppConfig.from_dict(cfg.to_dict())
    assert restored.crop_to_fill_threshold == pytest.approx(0.07)


def test_app_config_cache_slug_changes_with_threshold():
    a = AppConfig(crop_to_fill_threshold=0.0)
    b = AppConfig(crop_to_fill_threshold=0.05)
    assert a.cache_slug() != b.cache_slug()


# ── slow visual test ──────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEST_IMAGES_DIR = _REPO_ROOT / "images" / "test"
_BUILD_DIR = _REPO_ROOT / "build" / "test_letterbox"
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".heic", ".heif"}

_THRESHOLDS = [
    ("0pct",     0.0),
    ("2pct",     0.02),
    ("50pct",    0.50),
]
_ORIENTATIONS = ["portrait", "landscape"]


@pytest.fixture(scope="module", autouse=False)
def _wipe_letterbox_build():
    if _BUILD_DIR.exists():
        shutil.rmtree(_BUILD_DIR)
    _BUILD_DIR.mkdir(parents=True, exist_ok=True)


@pytest.mark.time_intensive
def test_visual_letterbox_all_images(_wipe_letterbox_build):
    """Render every test image at three thresholds × two orientations.

    Output layout:
      build/test_letterbox/<stem>_original<ext>
      build/test_letterbox/<stem>__portrait_0pct.png
      build/test_letterbox/<stem>__portrait_2pct.png
      build/test_letterbox/<stem>__portrait_50pct.png
      build/test_letterbox/<stem>__landscape_0pct.png
      …
    """
    from webserver.image import open_image_for_render, render_panel_bytes, preview_png_from_panel_bytes
    from webserver.display import TOTAL_BYTES

    test_images = sorted(
        p for p in _TEST_IMAGES_DIR.iterdir()
        if p.suffix.lower() in _IMAGE_EXTS
    )
    assert test_images, f"No test images found in {_TEST_IMAGES_DIR}"

    noop = _noop_cfg()

    for src in test_images:
        # Copy original for side-by-side comparison.
        shutil.copy(src, _BUILD_DIR / f"{src.stem}_original{src.suffix}")

        with open_image_for_render(src) as img:
            for orientation in _ORIENTATIONS:
                for label, threshold in _THRESHOLDS:
                    panel_bytes = render_panel_bytes(img, noop, orientation, threshold)
                    assert len(panel_bytes) == TOTAL_BYTES
                    preview = preview_png_from_panel_bytes(panel_bytes, orientation)
                    out = _BUILD_DIR / f"{src.stem}__{orientation}_{label}.png"
                    out.write_bytes(preview)
