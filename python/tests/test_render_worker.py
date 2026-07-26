"""Fast unit tests for render_worker.render_one().

Uses a small fixture image (PNG from images/test/) to keep the test fast.
The full-panel render is the real call — no mocking — so this test also
serves as a smoke test that the import chain inside the worker is correct.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

import hokku.webserver.image_renderer as image_renderer
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS
from hokku.webserver.render_worker import render_image_variants, render_one

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


# ── decode-once: many variants, one decode ────────────────────────────────────


def test_render_image_variants_decodes_source_once(monkeypatch):
    """render_image_variants decodes the source exactly once for N variants.

    The whole point of the batch: the source is decoded a single time and every
    (model, orientation) variant is dithered off that one buffer. Regression
    guard — previously each variant re-decoded (and re-took the decode lock).
    """
    decode_calls = {"n": 0}
    real_open = image_renderer.open_image_for_render

    def counting_open(path):
        decode_calls["n"] += 1
        return real_open(path)

    # The worker imports open_image_for_render from image_renderer at call time,
    # so patching the module attribute is picked up inside render_image_variants.
    monkeypatch.setattr(image_renderer, "open_image_for_render", counting_open)

    variants = [
        {"model": "huessen_epf1301", "orientation": "landscape", "image_config": _cfg_dict()},
        {"model": "huessen_epf1301", "orientation": "portrait", "image_config": _cfg_dict()},
    ]
    results = render_image_variants(str(_FIXTURE_PNG), variants)

    assert decode_calls["n"] == 1, f"source must decode once, decoded {decode_calls['n']}x"
    assert len(results) == 2
    assert all(r["ok"] for r in results)
    assert all(len(r["panel_bytes"]) == _HUESSEN.total_bytes for r in results)
    # Distinct orientations → distinct panels. Also proves the shared decoded
    # buffer survived the first render (release_input=False): if the first variant
    # had closed it, the second would have failed or produced garbage.
    assert results[0]["panel_bytes"] != results[1]["panel_bytes"]


def test_render_image_variants_isolates_one_variant_failure():
    """A single bad variant fails alone; siblings still render.

    Decode succeeds, so the batch does not raise; the bad variant (unknown model)
    comes back ``ok=False`` while the good one renders normally.
    """
    variants = [
        {"model": "huessen_epf1301", "orientation": "landscape", "image_config": _cfg_dict()},
        {"model": "no_such_model", "orientation": "landscape", "image_config": _cfg_dict()},
    ]
    results = render_image_variants(str(_FIXTURE_PNG), variants)

    assert results[0]["ok"] is True
    assert len(results[0]["panel_bytes"]) == _HUESSEN.total_bytes
    assert results[1]["ok"] is False
    assert "error" in results[1]


def test_render_image_variants_decode_failure_raises():
    """A source that cannot be decoded dooms every variant → the call raises."""
    variants = [
        {"model": "huessen_epf1301", "orientation": "landscape", "image_config": _cfg_dict()},
    ]
    with pytest.raises(FileNotFoundError):
        render_image_variants("/nonexistent/path/image.png", variants)
