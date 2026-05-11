"""Memory-budget tests for the dither pipeline.

These tests are slow (subprocess spawn + full render per case). They are
marked ``time_intensive`` so the default suite stays fast.

Run with:
    pytest webserver/tests/test_memory_budget.py -m time_intensive -s

The headline test is ``test_full_render_peak_under_50mb`` — a single
panel render must fit within 50 MB of the child's baseline RSS.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import psutil
import pytest
from PIL import Image

from hokku_server.dither_abc import _DEFAULT_STRIPE_H as DEFAULT_STRIPE_H
from hokku_server.image_config import ImageConfig
from hokku_server.image_renderer import compress_dynamic_range, open_image_for_render
from hokku_server.memory_guard import memory_limit, supported as memguard_supported
from hokku_server.presets import PRESET_IMAGE_CONFIGS
from tests._memory_helpers import (
    peak_python_heap,
    peak_rss_subprocess,
)


# Default image config used across all peak tests (real Floyd-Steinberg).
def _real_cfg() -> ImageConfig:
    return PRESET_IMAGE_CONFIGS["floyd_steinberg_hue_aware"]


# Test images: project-bundled real photos.
_TEST_IMAGES = Path(__file__).resolve().parent.parent.parent / "images" / "test"

REAL_IMAGES = [
    "Robert_De_Niro_KVIFF_portrait.jpg",      # portrait
    "Fitz_Roy_1.jpg",                          # landscape
    "Forest_road_Slavne_2017_BW_G9.jpg",       # B&W, 4500×2850
]


@pytest.fixture(scope="module")
def huge_jpeg(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 6000×4000 RGB JPEG with structured content (not a flat colour).

    Generated once per session so we can measure peak on a deliberately
    oversized source without committing a 5 MB binary to git.
    """
    out = tmp_path_factory.mktemp("huge") / "huge.jpg"
    rng = np.random.default_rng(seed=42)
    # Block-noise pattern compresses but isn't trivial.
    h, w = 4000, 6000
    arr = rng.integers(0, 255, size=(h // 8, w // 8, 3), dtype=np.uint8)
    img = Image.fromarray(arr).resize((w, h), Image.NEAREST)
    img.save(out, "JPEG", quality=85)
    return out


# ──────────────────────────────────────────────────────────────────────
# Layer C — subprocess RSS sampling (the headline assertion)
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.time_intensive
@pytest.mark.parametrize("image_name", REAL_IMAGES)
def test_full_render_peak_under_50mb(image_name: str) -> None:
    """A single panel render's RSS delta must be ≤ 50 MB."""
    image_path = _TEST_IMAGES / image_name
    assert image_path.is_file(), f"Test image missing from repo: {image_path}"
    delta, baseline = peak_rss_subprocess(image_path, cfg=_real_cfg())
    delta_mb = delta / (1024 * 1024)
    print(f"\n  {image_name}: render peak = {delta_mb:.1f} MB "
          f"(baseline {baseline / 1024 / 1024:.1f} MB)")
    assert delta < 50 * 1024 * 1024, (
        f"render of {image_name} consumed {delta_mb:.1f} MB peak — "
        f"budget is 50 MB"
    )


@pytest.mark.time_intensive
def test_full_render_huge_jpeg_under_50mb(huge_jpeg: Path) -> None:
    """A 6000×4000 source JPEG must also fit in 50 MB."""
    delta, baseline = peak_rss_subprocess(huge_jpeg, cfg=_real_cfg())
    delta_mb = delta / (1024 * 1024)
    print(f"\n  6000x4000 JPEG: render peak = {delta_mb:.1f} MB "
          f"(baseline {baseline / 1024 / 1024:.1f} MB)")
    assert delta < 50 * 1024 * 1024, (
        f"huge-JPEG render consumed {delta_mb:.1f} MB peak — budget is 50 MB"
    )


@pytest.mark.time_intensive
def test_full_render_huge_png_rejected_by_cap() -> None:
    """A 10 000 × 10 000 PNG is rejected by the decompression-bomb cap.

    Without streaming PNG decode in PIL, the full ~300 MB uint8 buffer would
    otherwise materialise before ``thumbnail`` can shrink it — fatal on a
    Pi. ``MAX_IMAGE_PIXELS`` now rejects such files at header read, so RSS
    growth stays minimal and ``open_image_for_render`` raises ``ValueError``.
    """


    image_path = _TEST_IMAGES / "synth_black_10000x10000.png"
    assert image_path.is_file(), f"Test image missing from repo: {image_path}"
    with pytest.raises((ValueError, Image.DecompressionBombError)):
        open_image_for_render(image_path)


# ──────────────────────────────────────────────────────────────────────
# Layer A — tracemalloc on individual pipeline functions
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.time_intensive
def test_compress_dynamic_range_peak_under_1mb_per_row() -> None:
    """Per-row DRC (the actual production unit) must allocate < 1 MB Python heap.

    Streaming dither calls ``compress_dynamic_range`` once per panel row via
    the ``prep_row`` callback, so the meaningful unit is a single 3200-pixel
    row. Float64 intermediates would blow this; float32 should keep us well
    under 1 MB even with the function's transient buffers.
    """

    row = np.random.default_rng(0).integers(
        0, 256, size=(1, 3200, 3), dtype=np.uint8
    ).astype(np.float32)
    peak = peak_python_heap(
        compress_dynamic_range,
        row,
        scale_chroma=False, adaptive_vivid=False,
        vivid_chroma_low=10.0, vivid_chroma_high=40.0,
    )
    peak_mb = peak / (1024 * 1024)
    print(f"\n  DRC 3200×1 row peak (Python heap) = {peak_mb:.3f} MB")
    assert peak < 1 * 1024 * 1024, (
        f"DRC peak Python heap = {peak_mb:.3f} MB on a single row; "
        f"target is < 1 MB so per-row DRC stays free of float64 regressions"
    )


@pytest.mark.time_intensive
def test_compress_dynamic_range_peak_under_30mb_per_stripe() -> None:
    """A 100-row DRC stripe — the actual production batch size — must peak
    well under 30 MB Python heap.  Float64 anywhere in DRC's intermediates
    would push this above the 50 MB end-to-end budget.

    Note: ``compress_dynamic_range`` itself currently allocates several
    transient ~3.8 MB float32 buffers (lab, chroma, t, factor, xyz_out, etc.)
    that bring the per-stripe peak to ~25-30 MB.  That's fine for the
    end-to-end budget — the streaming dither holds at most one cached
    stripe at a time.
    """


    stripe = np.random.default_rng(0).integers(
        0, 256, size=(DEFAULT_STRIPE_H, 3200, 3), dtype=np.uint8
    )
    peak = peak_python_heap(
        compress_dynamic_range,
        stripe.astype(np.float32),
        scale_chroma=False, adaptive_vivid=False,
        vivid_chroma_low=10.0, vivid_chroma_high=40.0,
    )
    peak_mb = peak / (1024 * 1024)
    print(f"\n  DRC 3200×{DEFAULT_STRIPE_H} stripe peak (Python heap) = {peak_mb:.2f} MB")
    assert peak < 30 * 1024 * 1024, (
        f"DRC peak Python heap = {peak_mb:.2f} MB on a {DEFAULT_STRIPE_H}-row stripe; "
        f"target is < 30 MB. Likely a float64 regression in the colour math."
    )


# ──────────────────────────────────────────────────────────────────────
# RLIMIT_AS hard-guard sanity checks
# ──────────────────────────────────────────────────────────────────────

def test_memory_guard_no_op_when_unsupported_does_not_raise() -> None:
    """The context manager must always be safe to enter regardless of OS.

    On Windows ``memory_limit`` is a no-op; on Linux/macOS it sets RLIMIT_AS.
    Either way, entering and exiting must work cleanly.
    """
    with memory_limit(2 * 1024 * 1024 * 1024):  # 2 GiB — far above anything
        pass


@pytest.mark.skipif(not memguard_supported(), reason="RLIMIT_AS not on this OS")
def test_memory_guard_raises_memory_error_when_exceeded() -> None:
    """When the cap is set absurdly low, a large allocation must raise MemoryError.

    Validates the hard-guarantee semantics on platforms that support it.
    """

    cur = int(psutil.Process().memory_info().rss)
    # Cap 1 MB above current RSS — any meaningful new allocation should fail.
    cap = cur + 1 * 1024 * 1024
    with pytest.raises(MemoryError):
        with memory_limit(cap):
            # Try to allocate 50 MB — must fail.
            _waste = bytearray(50 * 1024 * 1024)  # noqa: F841
