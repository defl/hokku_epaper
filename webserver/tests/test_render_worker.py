"""Fast unit tests for render_worker.render_one().

Uses a small fixture image (PNG from images/test/) to keep the test fast.
The full-panel render is the real call — no mocking — so this test also
serves as a smoke test that the import chain inside the worker is correct.
"""
from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path

import pytest

from webserver.display import TOTAL_BYTES, VISUAL_H, VISUAL_W
from webserver.presets import PRESET_IMAGE_CONFIGS
from webserver.render_worker import render_one

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEST_IMAGES = _REPO_ROOT / "images" / "test"

# Use the smallest available test image for speed.
_FIXTURE_PNG = _TEST_IMAGES / "grayscale_linear_bar_1200x300.png"
_FIXTURE_JPG = _TEST_IMAGES / "RGB_corner_gradient_bilinear_1200.png"


def _cfg_dict(preset: str = "atkinson") -> dict:
    return asdict(PRESET_IMAGE_CONFIGS[preset])


# ── panel bytes size ───────────────────────────────────────────────────────────

@pytest.mark.skipif(not _FIXTURE_PNG.exists(), reason="test image not found")
def test_render_one_panel_bytes_size():
    panel_bytes, preview_bytes = render_one(
        str(_FIXTURE_PNG), _cfg_dict(), "landscape"
    )
    assert len(panel_bytes) == TOTAL_BYTES


# ── preview bytes is valid PNG ─────────────────────────────────────────────────

@pytest.mark.skipif(not _FIXTURE_PNG.exists(), reason="test image not found")
def test_render_one_preview_is_png():
    _, preview_bytes = render_one(
        str(_FIXTURE_PNG), _cfg_dict(), "landscape"
    )
    assert preview_bytes[:8] == b"\x89PNG\r\n\x1a\n"


# ── portrait orientation ────────────────────────────────────────────────────────

@pytest.mark.skipif(not _FIXTURE_PNG.exists(), reason="test image not found")
def test_render_one_portrait_size():
    panel_bytes, _ = render_one(
        str(_FIXTURE_PNG), _cfg_dict(), "portrait"
    )
    assert len(panel_bytes) == TOTAL_BYTES


# ── different orientations produce different bytes ─────────────────────────────

@pytest.mark.skipif(not _FIXTURE_PNG.exists(), reason="test image not found")
def test_render_one_orientation_matters():
    pb_l, _ = render_one(str(_FIXTURE_PNG), _cfg_dict(), "landscape")
    pb_p, _ = render_one(str(_FIXTURE_PNG), _cfg_dict(), "portrait")
    assert pb_l != pb_p


# ── crop_to_fill_threshold forwarded correctly ─────────────────────────────────

@pytest.mark.skipif(not _FIXTURE_PNG.exists(), reason="test image not found")
def test_render_one_crop_threshold_accepted():
    panel_bytes, _ = render_one(
        str(_FIXTURE_PNG), _cfg_dict(), "landscape", crop_to_fill_threshold=1.0
    )
    assert len(panel_bytes) == TOTAL_BYTES


# ── gradient image (colour content) ───────────────────────────────────────────

@pytest.mark.skipif(not _FIXTURE_JPG.exists(), reason="test image not found")
def test_render_one_colour_image():
    panel_bytes, preview_bytes = render_one(
        str(_FIXTURE_JPG), _cfg_dict("atkinson_hue_aware"), "landscape"
    )
    assert len(panel_bytes) == TOTAL_BYTES
    assert preview_bytes[:8] == b"\x89PNG\r\n\x1a\n"


# ── bad path raises a meaningful exception ────────────────────────────────────

def test_render_one_bad_path_raises():
    with pytest.raises(Exception):
        render_one("/nonexistent/path/image.png", _cfg_dict(), "landscape")
