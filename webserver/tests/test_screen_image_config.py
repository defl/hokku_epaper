"""ScreenImageConfig: round-trip, cache_slug stability, and orientation separation.

Unit tests (always run).

Slow visual-rendering test is marked ``time_intensive`` — see the bottom of
this file. Run with: pytest -m time_intensive
"""
from __future__ import annotations

import shutil
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from hokku_server.display import TOTAL_BYTES
from hokku_server.dither_config import DitherConfig
from hokku_server.dither_streaming_numba import NumbaStreamingDither
from hokku_server.image_abc import preview_png_from_panel_bytes
from hokku_server.image_config import ImageConfig, _image_config_from_dict
from hokku_server.image_renderer import ImageRenderer, open_image_for_render
from hokku_server.presets import DEFAULT_PRESET, PRESET_IMAGE_CONFIGS
from hokku_server.screen_image_config import ScreenImageConfig, _screen_image_config_from_dict

from tests._helpers import is_oversize_fixture


# ── helpers ───────────────────────────────────────────────────────────────────

def _noop_image_config() -> ImageConfig:
    """Identity-ish ImageConfig with the noop ditherer — fast for rendering tests."""
    base = PRESET_IMAGE_CONFIGS[DEFAULT_PRESET]
    return replace(
        base,
        prepare_autocontrast_cutoff=0.0,
        prepare_gamma=1.0,
        prepare_brightness=1.0,
        prepare_contrast=1.0,
        prepare_usm_amount=0,
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


def _make_screen_cfg(orientation: str = "landscape") -> ScreenImageConfig:
    return ScreenImageConfig(
        image_config=_noop_image_config(),
        orientation=orientation,  # type: ignore[arg-type]
    )


# ── unit tests ────────────────────────────────────────────────────────────────

def test_roundtrip_landscape():
    cfg = _make_screen_cfg("landscape")
    d = asdict(cfg)
    restored = _screen_image_config_from_dict(d)
    assert restored == cfg


def test_roundtrip_portrait():
    cfg = _make_screen_cfg("portrait")
    d = asdict(cfg)
    restored = _screen_image_config_from_dict(d)
    assert restored == cfg


def test_cache_slug_stable():
    cfg = _make_screen_cfg()
    assert cfg.cache_slug() == cfg.cache_slug()
    assert cfg.cache_slug() == _screen_image_config_from_dict(asdict(cfg)).cache_slug()


def test_cache_slug_changes_on_orientation():
    landscape = _make_screen_cfg("landscape")
    portrait = _make_screen_cfg("portrait")
    assert landscape.cache_slug() != portrait.cache_slug()


def test_cache_slug_changes_on_image_config():
    a = _make_screen_cfg()
    b = ScreenImageConfig(
        image_config=replace(a.image_config, prepare_brightness=0.5),
        orientation=a.orientation,
    )
    assert a.cache_slug() != b.cache_slug()


def test_same_image_config_different_orientation_different_slug():
    """Two ScreenImageConfigs identical except orientation have distinct slugs."""
    ic = _noop_image_config()
    ls = ScreenImageConfig(image_config=ic, orientation="landscape")
    pt = ScreenImageConfig(image_config=ic, orientation="portrait")
    # Same image_config slug but different orientation → different combined slug.
    assert ic.cache_slug() == ls.image_config.cache_slug()
    assert ic.cache_slug() == pt.image_config.cache_slug()
    assert ls.cache_slug() != pt.cache_slug()


def test_cache_slug_length():
    assert len(_make_screen_cfg().cache_slug()) == 14


# ── slow visual rendering test ────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEST_IMAGES_DIR = _REPO_ROOT / "images" / "test"
_BUILD_DIR = _REPO_ROOT / "build" / "test_screen_image"
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".heic", ".heif"}


@pytest.fixture(scope="module", autouse=False)
def _wipe_build_dir():
    if _BUILD_DIR.exists():
        shutil.rmtree(_BUILD_DIR)
    _BUILD_DIR.mkdir(parents=True, exist_ok=True)


@pytest.mark.time_intensive
def test_visual_render_all_test_images(_wipe_build_dir):
    """Render every test image with a noop ditherer and save to build/.

    Output files:
      build/test_screen_image/<stem>__noop.png
      build/test_screen_image/<stem>_original<ext>
    """
    def render_panel_bytes(img, cfg, orientation, crop_to_fill_threshold=0.0):
        return ImageRenderer(NumbaStreamingDither()).render_panel_bytes(img, cfg, orientation, crop_to_fill_threshold)

    test_images = sorted(
        p for p in _TEST_IMAGES_DIR.iterdir()
        if p.suffix.lower() in _IMAGE_EXTS
        and not is_oversize_fixture(p)
    )
    assert test_images, f"No test images found in {_TEST_IMAGES_DIR}"

    noop_cfg = _noop_image_config()
    screen_cfg = ScreenImageConfig(image_config=noop_cfg, orientation="landscape")

    for src in test_images:
        dest_original = _BUILD_DIR / f"{src.stem}_original{src.suffix}"
        shutil.copy(src, dest_original)

        with open_image_for_render(src) as img:
            panel_bytes = render_panel_bytes(img, screen_cfg.image_config, screen_cfg.orientation)

        assert len(panel_bytes) == TOTAL_BYTES, (
            f"{src.name}: expected {TOTAL_BYTES} bytes, got {len(panel_bytes)}"
        )

        preview = preview_png_from_panel_bytes(panel_bytes, screen_cfg.orientation)
        out_png = _BUILD_DIR / f"{src.stem}__noop.png"
        out_png.write_bytes(preview)
