"""Fast unit tests for render_worker.render_one().

Uses a small fixture image (PNG from images/test/) to keep the test fast.
The full-panel render is the real call — no mocking — so this test also
serves as a smoke test that the import chain inside the worker is correct.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS
from hokku.webserver.render_worker import render_one

_HUESSEN = DISPLAY_REGISTRY["huessen_epf1301"]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEST_IMAGES = _REPO_ROOT / "images" / "test"

# Use the smallest available test image for speed.
_FIXTURE_PNG = _TEST_IMAGES / "grayscale_linear_bar_1200x300.png"
_FIXTURE_JPG = _TEST_IMAGES / "RGB_corner_gradient_bilinear_1200.png"


def _cfg_dict(preset: str = "atkinson_hue_aware") -> dict:
    return asdict(PRESET_IMAGE_CONFIGS[preset])


# ── panel bytes size ───────────────────────────────────────────────────────────


def test_render_one_panel_bytes_size():
    panel_bytes, _ = render_one(str(_FIXTURE_PNG), _cfg_dict(), "huessen_epf1301", "landscape")
    assert len(panel_bytes) == _HUESSEN.total_bytes


# ── preview bytes is valid PNG ─────────────────────────────────────────────────


def test_render_one_preview_is_png():
    _, preview_bytes = render_one(str(_FIXTURE_PNG), _cfg_dict(), "huessen_epf1301", "landscape")
    assert preview_bytes[:8] == b"\x89PNG\r\n\x1a\n"


# ── portrait orientation ────────────────────────────────────────────────────────


def test_render_one_portrait_size():
    panel_bytes, _ = render_one(str(_FIXTURE_PNG), _cfg_dict(), "huessen_epf1301", "portrait")
    assert len(panel_bytes) == _HUESSEN.total_bytes


# ── different orientations produce different bytes ─────────────────────────────


def test_render_one_orientation_matters():
    pb_l, _ = render_one(str(_FIXTURE_PNG), _cfg_dict(), "huessen_epf1301", "landscape")
    pb_p, _ = render_one(str(_FIXTURE_PNG), _cfg_dict(), "huessen_epf1301", "portrait")
    assert pb_l != pb_p


# ── crop_to_fill_threshold forwarded correctly ─────────────────────────────────


def test_render_one_crop_threshold_accepted():
    panel_bytes, _ = render_one(
        str(_FIXTURE_PNG), _cfg_dict(), "huessen_epf1301", "landscape", crop_to_fill_threshold=1.0
    )
    assert len(panel_bytes) == _HUESSEN.total_bytes


# ── gradient image (colour content) ───────────────────────────────────────────


def test_render_one_colour_image():
    panel_bytes, preview_bytes = render_one(
        str(_FIXTURE_JPG), _cfg_dict("atkinson_hue_aware"), "huessen_epf1301", "landscape"
    )
    assert len(panel_bytes) == _HUESSEN.total_bytes
    assert preview_bytes[:8] == b"\x89PNG\r\n\x1a\n"


# ── native-decoder (JPEG XL) renders — regression for the RLIMIT_AS removal ────


def test_render_one_renders_jxl():
    """A JPEG XL image renders through the worker.

    libjxl reserves large *virtual* arenas/thread-stacks while decoding. The
    worker used to wrap the render in an RLIMIT_AS cap, under which libjxl failed
    with an opaque "Generic Error" on the memory-constrained Pi — while the same
    image decoded fine without the cap. The cap was removed (redundant with the
    ingest decode budget; RLIMIT_AS never bounded physical RAM anyway). This keeps
    a native decoder in the render smoke-test set so that regression can't return
    silently. Skipped only where pillow-jxl/libjxl isn't functional.
    """
    jxl = _TEST_IMAGES / "Albrecht_Duerer_Hare_1502_Google_Art_Project.jxl"
    if not jxl.is_file():
        pytest.skip("JXL fixture missing")
    try:
        import pillow_jxl  # noqa: F401, PLC0415
        from PIL import Image  # noqa: PLC0415

        with Image.open(jxl) as _probe:
            _probe.load()
    except Exception as e:
        pytest.skip(f"pillow-jxl/libjxl not functional here: {e}")

    panel_bytes, preview_bytes = render_one(str(jxl), _cfg_dict(), "huessen_epf1301", "landscape")
    assert len(panel_bytes) == _HUESSEN.total_bytes
    assert preview_bytes[:8] == b"\x89PNG\r\n\x1a\n"


# ── bad path raises a meaningful exception ────────────────────────────────────


def test_render_one_bad_path_raises():
    with pytest.raises(FileNotFoundError):
        render_one("/nonexistent/path/image.png", _cfg_dict(), "huessen_epf1301", "landscape")
