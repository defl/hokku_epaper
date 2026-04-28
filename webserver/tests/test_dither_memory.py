"""Peak-memory regression tests for the dither pipeline.

Two tiers of coverage, because a single 1200×1600 render on a full-size
source takes 60–180 s in Python:

1. **Broad matrix** (default suite): every (image × preset) combination,
   but rendered into a half-resolution 600×800 canvas. Each render finishes
   in seconds, so the full 18-case matrix completes in a couple of minutes.
   The 25 MB ceiling corresponds to the full-resolution 100 MB target
   (peak scales ~linearly with canvas area: 600×800 is 1/4 of 1200×1600).

2. **Full-resolution smoke** (marked @pytest.mark.memory so it's opt-out):
   one render at production canvas for one image × each preset, verifying
   the < 100 MB ceiling holds at the real buffer size. Runs in ~10 min.

tracemalloc captures NumPy buffer allocations because NumPy's data buffers
route through PyMem_RawMalloc, which tracemalloc hooks. It does NOT capture
libjpeg/libpng internal buffers during Image.open() → load; for the JPEGs
in images/test/ that's negligible compared to the NumPy/PIL working set.

To skip the slow full-resolution smoke tests:
    pytest -m 'not memory'
"""
from __future__ import annotations

import copy
import sys
import time
import tracemalloc
from pathlib import Path
from unittest.mock import patch

import pytest
import numpy as np
from PIL import Image, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import webserver  # type: ignore


TEST_IMAGE_DIR = Path(__file__).resolve().parents[2] / "images" / "test"

# Half-resolution canvas: 600×800 vs production 1200×1600 (1/4 area, 1/4 memory).
HALF_CANVAS_W = webserver.FULL_W // 2      # 600
HALF_CANVAS_H = webserver.PANEL_H // 2     # 800

# Matrix ceiling at half-resolution, scaled from the 100 MB production target.
# Peak scales roughly linearly with canvas area; 100 MB / 4 = 25 MB, plus a
# small constant overhead for non-canvas allocations → 30 MB with headroom.
HALF_CANVAS_CEILING_MB = 30

# Production-resolution ceiling (used by the slower smoke test).
FULL_CANVAS_CEILING_MB = 100


def _test_images():
    if not TEST_IMAGE_DIR.is_dir():
        return []
    return sorted(TEST_IMAGE_DIR.glob("*.jpg"))


def _preset_names():
    return list(webserver.DITHER_PRESETS.keys())


def _render_half_canvas(img_path, preset_name):
    """Run the production pipeline into a 600×800 canvas and return the peak
    tracemalloc allocation (MB). Skips the panel-encoding step since the half-
    canvas shape doesn't match the panel buffer layout — we only care about
    peak memory, not the final 960K binary."""
    dither_cfg = copy.deepcopy(webserver.DITHER_PRESETS[preset_name]["dither"])

    # Load + EXIF-orient + JPEG-draft downsample (mirrors _convert_image).
    img = Image.open(img_path)
    max_side = 3 * max(webserver.FULL_W, webserver.PANEL_H)
    try:
        img.draft(None, (max_side, max_side))
    except Exception:
        pass
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)

    if tracemalloc.is_tracing():
        tracemalloc.stop()
    tracemalloc.start()
    tracemalloc.reset_peak()
    try:
        effective_cfg, _ = webserver._maybe_apply_bw_fallback(dither_cfg, img)
        cfg = {**webserver.DEFAULT_CONFIG, "dither": effective_cfg,
               "orientation": "landscape"}
        with patch.object(webserver, "_config", cfg):
            webserver._run_dither_pipeline(
                img, effective_cfg, "landscape",
                canvas_w=HALF_CANVAS_W, canvas_h=HALF_CANVAS_H,
            )
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak / (1024 * 1024)


@pytest.mark.skipif(not _test_images(), reason="images/test/ not checked out")
@pytest.mark.parametrize("preset_name", _preset_names())
@pytest.mark.parametrize("image_path", _test_images(), ids=lambda p: p.name)
def test_half_canvas_peak_under_ceiling(image_path, preset_name):
    """Every (image × preset) combination must render at half-resolution
    600×800 with tracemalloc peak < 30 MB. At full production resolution
    (1200×1600) the peak scales roughly 4× — i.e. under the 100 MB target
    that guards against OOM on the Pi Zero 2 W (512 MB RAM total).

    If a new image or preset pushes this close to the ceiling, investigate
    — don't just raise the threshold."""
    peak_mb = _render_half_canvas(image_path, preset_name)
    assert peak_mb < HALF_CANVAS_CEILING_MB, (
        f"{image_path.name} / {preset_name}: peak {peak_mb:.1f} MB at "
        f"600×800 canvas exceeds ceiling {HALF_CANVAS_CEILING_MB} MB. "
        f"At production 1200×1600 canvas this would likely exceed "
        f"{FULL_CANVAS_CEILING_MB} MB and risk OOM on the Pi.")


def _measure_full_convert_image(img_path, preset_name):
    """Full-resolution `_convert_image` call with tracemalloc active."""
    dither_cfg = copy.deepcopy(webserver.DITHER_PRESETS[preset_name]["dither"])
    cfg = {**webserver.DEFAULT_CONFIG, "dither": dither_cfg, "orientation": "landscape"}
    with patch.object(webserver, "_config", cfg):
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        tracemalloc.start()
        tracemalloc.reset_peak()
        try:
            raw, _preview = webserver._convert_image(img_path)
        finally:
            _cur, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
    return peak / (1024 * 1024), len(raw)


@pytest.mark.memory
@pytest.mark.skipif(not _test_images(), reason="images/test/ not checked out")
@pytest.mark.parametrize("preset_name", _preset_names())
def test_full_resolution_peak_under_100mb(preset_name):
    """Production-canvas smoke test. Runs _convert_image at 1200×1600 on
    the single largest test image for each preset, verifying peak stays
    under 100 MB. Takes ~60–180 s per preset; skip with `-m 'not memory'`.

    One image is enough because peak is dominated by the fixed-size
    post-composite canvas buffers (Lab/xyz transforms), not by the
    source image size — verified by the half-canvas matrix above which
    tests every (image, preset) combination.
    """
    paths = _test_images()
    # Largest source file; gets the most aggressive JPEG-draft path so we
    # also exercise that. Fall back gracefully if the file set changes.
    biggest = max(paths, key=lambda p: p.stat().st_size)
    peak_mb, raw_len = _measure_full_convert_image(biggest, preset_name)
    assert raw_len == webserver.TOTAL_BYTES, (
        f"{biggest.name} / {preset_name}: raw output len {raw_len} != "
        f"{webserver.TOTAL_BYTES}")
    assert peak_mb < FULL_CANVAS_CEILING_MB, (
        f"{biggest.name} / {preset_name}: peak {peak_mb:.1f} MB at full "
        f"1200×1600 canvas exceeds ceiling {FULL_CANVAS_CEILING_MB} MB. "
        f"This would OOM the Pi Zero 2 W. Fix the pipeline — don't "
        f"raise the ceiling.")
