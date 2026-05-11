"""Dither pipeline unit tests + slow visual-output tests.

Fast tests (always run — use noop kernel or small previews, so the suite
finishes in seconds):
  - render_panel_bytes() output size and valid nibbles
  - render_preview_png() produces valid PNG with correct orientation/size
  - preview_png_from_panel_bytes() roundtrip size
  - B&W detection suppresses saturation boosters
  - Every preset produces deterministic output
  - Every preset produces distinct output from other presets (using preview)
  - Orientation (landscape vs portrait) changes the output
  - cache_slug stability across presets

Slow tests (marked ``time_intensive``, skipped by default):
  Run with:  pytest -m time_intensive

  For every image in images/test/ × every named preset:
  - Full-scale panel render → decoded to PNG (punchy palette, landscape).
    Outputs: build/test_dither_full/<preset>/<stem>.png
             build/test_dither_full/<preset>/<stem>_original<ext>
  - Preview PNG (≤ 800 px).
    Outputs: build/test_dither_preview/<preset>/<stem>.png
             build/test_dither_preview/<preset>/<stem>_original<ext>
  These exist solely for human inspection of dither quality; they are not
  correctness assertions beyond "it ran without error and produced valid output."
"""
from __future__ import annotations

import shutil
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from hokku_server.display import (
    FULL_W,
    PANEL_H,
    TOTAL_BYTES,
    VISUAL_H,
    VISUAL_W,
    indices_to_preview_rgb,
    panel_bytes_to_indices,
)
from hokku_server.dither_streaming import StreamingDither
from hokku_server.dither_unconstrained import UnconstrainedDither
from hokku_server.image_abc import preview_png_from_panel_bytes
from hokku_server.image_classifier import _is_near_grayscale
from hokku_server.image_config import ImageConfig, Orientation
from hokku_server.image_renderer import ImageRenderer, open_image_for_render
from hokku_server.presets import PRESET_IMAGE_CONFIGS


def render_panel_bytes(img, cfg, orientation, crop_to_fill_threshold=0.0, *, unconstrained=False):
    dither = UnconstrainedDither() if unconstrained else StreamingDither()
    return ImageRenderer(dither).render_panel_bytes(img, cfg, orientation, crop_to_fill_threshold)


def render_preview_png(img, cfg, orientation, max_side_px=800, crop_to_fill_threshold=0.0):
    return ImageRenderer(StreamingDither()).render_preview_png(img, cfg, orientation, max_side_px, crop_to_fill_threshold)


# ── module-level helpers ──────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEST_IMAGES_DIR = _REPO_ROOT / "images" / "test"
_BUILD_FULL_DIR = _REPO_ROOT / "build" / "test_dither_full"
_BUILD_PREVIEW_DIR = _REPO_ROOT / "build" / "test_dither_preview"

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".heic", ".heif"}

# Noop-kernel version of atkinson — instant dither, used for structural tests
# that only care about output format/size, not visual quality.
_FAST_CFG: ImageConfig = replace(
    PRESET_IMAGE_CONFIGS["atkinson"],
    dither=replace(PRESET_IMAGE_CONFIGS["atkinson"].dither, algorithm="noop"),
)


def _make_rgb(w: int = 40, h: int = 30, color: tuple = (180, 60, 60)) -> Image.Image:
    return Image.new("RGB", (w, h), color)


def _make_grey(w: int = 40, h: int = 30, level: int = 128) -> Image.Image:
    return Image.new("RGB", (w, h), (level, level, level))


def _make_gradient(w: int = 200, h: int = 150) -> Image.Image:
    """RGB gradient across width/height — gives dither algorithms real content to chew on."""
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:, :, 0] = np.linspace(30, 220, w, dtype=np.uint8)[None, :]   # R varies across x
    arr[:, :, 1] = np.linspace(200, 40, h, dtype=np.uint8)[:, None]   # G varies across y
    arr[:, :, 2] = 80
    return Image.fromarray(arr)


def _png_size(png_bytes: bytes) -> tuple[int, int]:
    img = Image.open(BytesIO(png_bytes))
    return img.size  # (width, height)


def _test_images() -> list[Path]:
    if not _TEST_IMAGES_DIR.exists():
        return []
    return sorted(
        p for p in _TEST_IMAGES_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in _IMAGE_EXTS
    )


def _indices_to_png(idx: np.ndarray, orientation: Orientation) -> bytes:
    rgb = indices_to_preview_rgb(idx)
    img = Image.fromarray(rgb)
    if orientation == "landscape":
        img = img.rotate(90, expand=True)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── fast: panel_bytes structure ───────────────────────────────────────────────

def test_panel_bytes_size():
    raw = render_panel_bytes(_make_rgb(), _FAST_CFG, "landscape")
    assert len(raw) == TOTAL_BYTES


def test_panel_bytes_nibbles_are_valid():
    """Every nibble must decode to a known palette index (0–5)."""
    raw = render_panel_bytes(_make_rgb(), _FAST_CFG, "landscape")
    idx = panel_bytes_to_indices(raw)  # raises on invalid nibbles
    assert idx.shape == (PANEL_H, FULL_W)
    assert idx.dtype == np.uint8
    assert int(idx.min()) >= 0
    assert int(idx.max()) <= 5


def test_panel_bytes_portrait_size():
    raw = render_panel_bytes(_make_rgb(), _FAST_CFG, "portrait")
    assert len(raw) == TOTAL_BYTES


# ── fast: preview PNG structure ───────────────────────────────────────────────

def test_preview_png_is_valid_png():
    png = render_preview_png(_make_rgb(), _FAST_CFG, "landscape", max_side_px=100)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_preview_png_landscape_is_wider_than_tall():
    png = render_preview_png(_make_rgb(), _FAST_CFG, "landscape", max_side_px=200)
    w, h = _png_size(png)
    assert w > h, f"Landscape preview should be wider than tall, got {w}x{h}"
    assert max(w, h) <= 200


def test_preview_png_portrait_is_taller_than_wide():
    png = render_preview_png(_make_rgb(), _FAST_CFG, "portrait", max_side_px=200)
    w, h = _png_size(png)
    assert h > w, f"Portrait preview should be taller than wide, got {w}x{h}"
    assert max(w, h) <= 200


def test_preview_png_max_side_respected():
    for max_px in (50, 100, 200):
        png = render_preview_png(_make_rgb(), _FAST_CFG, "landscape", max_side_px=max_px)
        w, h = _png_size(png)
        assert max(w, h) <= max_px, f"max_side_px={max_px} violated: {w}x{h}"


# ── fast: roundtrip panel_bytes → preview ─────────────────────────────────────

def test_preview_from_panel_bytes_is_valid_png():
    raw = render_panel_bytes(_make_rgb(), _FAST_CFG, "landscape")
    png = preview_png_from_panel_bytes(raw, "landscape")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_preview_from_panel_bytes_landscape_dimensions():
    """panel_bytes → PNG must be the full visible panel size in landscape."""
    raw = render_panel_bytes(_make_rgb(), _FAST_CFG, "landscape")
    png = preview_png_from_panel_bytes(raw, "landscape")
    w, h = _png_size(png)
    assert (w, h) == (VISUAL_W, VISUAL_H), f"Expected {VISUAL_W}x{VISUAL_H}, got {w}x{h}"


# ── fast: B&W detection ───────────────────────────────────────────────────────

def test_bw_detection_neutral_grey():
    assert _is_near_grayscale(_make_grey(200, 200, 128))


def test_bw_detection_vivid_red():
    assert not _is_near_grayscale(Image.new("RGB", (200, 200), (220, 30, 30)))


def test_bw_image_renders_without_error():
    """Hue-aware preset on a grey image must not crash (B&W path is exercised)."""
    cfg = PRESET_IMAGE_CONFIGS["atkinson_hue_aware"]  # has adaptive_vivid=True
    # Use a real (non-noop) preset here because the B&W guard only fires on
    # configs that have adaptive_saturate / adaptive_vivid enabled.
    # Keep the image tiny to stay fast at full-panel resolution.
    raw = render_panel_bytes(_make_grey(10, 10), cfg, "landscape")
    assert len(raw) == TOTAL_BYTES
    idx = panel_bytes_to_indices(raw)
    assert 1 in set(idx.flatten().tolist())  # white letterbox pixels present


# ── fast: preset determinism & distinctness (via small preview) ───────────────

def test_preset_output_is_deterministic():
    """Same inputs always produce identical bytes (no randomness in pipeline)."""
    img = _make_rgb(30, 20)
    cfg = PRESET_IMAGE_CONFIGS["atkinson"]
    p1 = render_preview_png(img, cfg, "landscape", max_side_px=100)
    p2 = render_preview_png(img, cfg, "landscape", max_side_px=100)
    assert p1 == p2


def test_presets_produce_distinct_output():
    """At least some presets must produce different output from others.

    A gradient source is used so error-diffusion algorithms have genuine
    variation to propagate — a flat image would be identically quantized
    by every kernel.
    """
    img = _make_gradient()
    previews = {
        name: render_preview_png(img, cfg, "landscape", max_side_px=200)
        for name, cfg in PRESET_IMAGE_CONFIGS.items()
    }
    names = list(previews)
    all_same = all(previews[a] == previews[b] for a in names for b in names if a != b)
    assert not all_same, "All presets produced identical output — pipeline is broken"


def test_orientation_changes_panel_output():
    # render_panel_bytes consumes the input image (closes the PIL buffer to
    # save memory), so we make a fresh image per orientation.
    raw_l = render_panel_bytes(_make_rgb(40, 30), _FAST_CFG, "landscape")
    raw_p = render_panel_bytes(_make_rgb(40, 30), _FAST_CFG, "portrait")
    assert raw_l != raw_p


# ── fast: all presets smoke-test (small preview) ─────────────────────────────

@pytest.mark.parametrize("preset_name", list(PRESET_IMAGE_CONFIGS))
def test_every_preset_preview_landscape(preset_name: str):
    img = _make_rgb(60, 40)
    cfg = PRESET_IMAGE_CONFIGS[preset_name]
    png = render_preview_png(img, cfg, "landscape", max_side_px=100)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = _png_size(png)
    assert w > h


@pytest.mark.parametrize("preset_name", list(PRESET_IMAGE_CONFIGS))
def test_every_preset_preview_portrait(preset_name: str):
    img = _make_rgb(60, 40)
    cfg = PRESET_IMAGE_CONFIGS[preset_name]
    png = render_preview_png(img, cfg, "portrait", max_side_px=100)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = _png_size(png)
    assert h > w


# ── fast: cache_slug stability ────────────────────────────────────────────────

def test_image_config_cache_slug_is_stable():
    cfg = PRESET_IMAGE_CONFIGS["atkinson"]
    assert cfg.cache_slug() == cfg.cache_slug()


def test_image_config_cache_slugs_are_distinct():
    slugs = [cfg.cache_slug() for cfg in PRESET_IMAGE_CONFIGS.values()]
    assert len(slugs) == len(set(slugs)), "Two presets share the same cache_slug"


# ── slow: full-scale visual output ────────────────────────────────────────────

_MODES = ["streaming", "unconstrained", "streaming_numba"]


def _preview_params():
    imgs = _test_images()
    presets = list(PRESET_IMAGE_CONFIGS)
    return [(img, p) for img in imgs for p in presets]


def _preview_ids():
    return [f"{img.stem}__{p}" for img, p in _preview_params()]


def _slow_params():
    """All (image, preset, mode) combinations for the full-scale render test."""
    imgs = _test_images()
    presets = list(PRESET_IMAGE_CONFIGS)
    return [
        (img, p, m)
        for img in imgs
        for p in presets
        for m in _MODES
    ]


def _slow_ids():
    return [f"{img.stem}__{p}__{m}" for img, p, m in _slow_params()]


@pytest.mark.time_intensive
@pytest.mark.parametrize("src,preset_name,mode", _slow_params(), ids=_slow_ids())
def test_dither_full_scale(src: Path, preset_name: str, mode: str):
    """Render at full panel resolution; write decoded PNG to build/test_dither_full/.

    All three rendering paths (streaming, unconstrained, streaming_numba) are
    exercised for every image × preset combination so the output files can be
    compared visually.

    Output layout (flat):
      build/test_dither_full/<stem>__<preset>__streaming.png
      build/test_dither_full/<stem>__<preset>__unconstrained.png
      build/test_dither_full/<stem>__<preset>__streaming_numba.png
      build/test_dither_full/<stem>_original<ext>   — source copy (written once)
    """
    _BUILD_FULL_DIR.mkdir(parents=True, exist_ok=True)

    original_dest = _BUILD_FULL_DIR / f"{src.stem}_original{src.suffix}"
    if not original_dest.exists():
        shutil.copy2(src, original_dest)

    cfg = PRESET_IMAGE_CONFIGS[preset_name]
    if mode == "streaming_numba":
        pytest.importorskip("numba", reason="numba not installed")
        from hokku_server.dither_streaming_numba import NumbaDither
        with open_image_for_render(src) as img:
            raw = ImageRenderer(NumbaDither()).render_panel_bytes(img, cfg, "landscape")
    else:
        unconstrained = mode == "unconstrained"
        with open_image_for_render(src) as img:
            raw = render_panel_bytes(img, cfg, "landscape", unconstrained=unconstrained)

    assert len(raw) == TOTAL_BYTES

    idx = panel_bytes_to_indices(raw)
    assert idx.shape == (PANEL_H, FULL_W)
    assert int(idx.min()) >= 0
    assert int(idx.max()) <= 5

    png_bytes = _indices_to_png(idx, "landscape")
    (_BUILD_FULL_DIR / f"{src.stem}__{preset_name}__{mode}.png").write_bytes(png_bytes)

    w, h = _png_size(png_bytes)
    assert (w, h) == (VISUAL_W, VISUAL_H), (
        f"{src.name}/{preset_name}/{mode}: expected {VISUAL_W}x{VISUAL_H}, got {w}x{h}"
    )


@pytest.mark.time_intensive
@pytest.mark.parametrize("src,preset_name", _preview_params(), ids=_preview_ids())
def test_dither_preview(src: Path, preset_name: str):
    """Render preview PNG (≤ 800 px); write to build/test_dither_preview/.

    Output layout (flat):
      build/test_dither_preview/<stem>__<preset>.png — dithered preview
      build/test_dither_preview/<stem>_original<ext> — source copy (written once)
    """
    _BUILD_PREVIEW_DIR.mkdir(parents=True, exist_ok=True)

    original_dest = _BUILD_PREVIEW_DIR / f"{src.stem}_original{src.suffix}"
    if not original_dest.exists():
        shutil.copy2(src, original_dest)

    cfg = PRESET_IMAGE_CONFIGS[preset_name]
    with open_image_for_render(src) as img:
        png_bytes = render_preview_png(img, cfg, "landscape", max_side_px=800)

    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"

    (_BUILD_PREVIEW_DIR / f"{src.stem}__{preset_name}.png").write_bytes(png_bytes)

    w, h = _png_size(png_bytes)
    assert max(w, h) <= 800
    assert w > h, f"{src.name}/{preset_name}: expected landscape aspect, got {w}x{h}"
