"""Unit tests for ImageManager.estimate_remaining_seconds().

All tests inject synthetic ImageRecord instances directly — no real file I/O
or conversions needed, so the suite is fast.

Key invariants verified:
- Returns None when idle or when no timing data is available yet.
- Rate (seconds/byte) is derived from total_time / total_bytes across all
  converted images, not a per-image average.
- Estimate for each pending image scales linearly with its size.
- Images with status "ok" or "failed" are not counted in the remaining sum.
- Multiple converted images correctly refine the fitted rate.
"""
from __future__ import annotations

import time
from dataclasses import replace

import pytest

from webserver.app_config import AppConfig
from webserver.image_manager import ConversionProgress, ImageRecord, SingleThreadedImageManager


# ── helpers ───────────────────────────────────────────────────────────────────

def _rec(
    name: str,
    size_bytes: int,
    status: str = "ok",
    conversion_seconds: float | None = None,
    width: int | None = None,
    height: int | None = None,
) -> ImageRecord:
    """Minimal ImageRecord for estimator tests."""
    return ImageRecord(
        name=name,
        name_hash=name[:14].ljust(14, "0"),
        original_sha1="aabbcc",
        original_size_bytes=size_bytes,
        original_mtime=time.time(),
        added_at=time.time(),
        convert_status=status,          # type: ignore[arg-type]
        convert_error=None,
        screen_image_config_slug="slug" if status == "ok" else None,
        last_conversion_seconds=conversion_seconds,
        image_width=width,
        image_height=height,
    )


def _manager_with(app_config: AppConfig, records: list[ImageRecord], progress: ConversionProgress) -> SingleThreadedImageManager:
    """Build an ImageManager and inject synthetic records + progress."""
    mgr = SingleThreadedImageManager(app_config)
    mgr._records = {r.name: r for r in records}
    mgr._progress = progress
    return mgr


def _idle() -> ConversionProgress:
    return ConversionProgress(current_name=None, done=0, total=0)


def _converting(total: int, done: int = 0) -> ConversionProgress:
    return ConversionProgress(current_name="img.png", done=done, total=total)


# ── no estimate cases ─────────────────────────────────────────────────────────

def test_returns_none_when_idle(app_config: AppConfig):
    """Nothing pending → None."""
    mgr = _manager_with(app_config, [], _idle())
    assert mgr.estimate_remaining_seconds() is None


def test_returns_none_when_all_done(app_config: AppConfig):
    """Batch finished (done == total) → None."""
    records = [_rec("a.png", 1000, "ok", conversion_seconds=2.0)]
    mgr = _manager_with(app_config, records, _converting(total=1, done=1))
    assert mgr.estimate_remaining_seconds() is None


def test_returns_none_when_no_timing_data(app_config: AppConfig):
    """Pending images exist but no image has been timed yet → None."""
    records = [_rec("a.png", 5000, "pending")]
    mgr = _manager_with(app_config, records, _converting(total=1))
    assert mgr.estimate_remaining_seconds() is None


# ── basic rate calculation ────────────────────────────────────────────────────

def test_single_converted_single_pending(app_config: AppConfig):
    """Rate from one converted image applied to one pending image."""
    # 2000 px converted in 4.0 s → rate = 0.002 s/px
    # Pending: 1000 px → estimate = 2.0 s
    records = [
        _rec("done.png",    1000, "ok",      conversion_seconds=4.0, width=2000, height=1),
        _rec("pending.png", 1000, "pending",                          width=1000, height=1),
    ]
    mgr = _manager_with(app_config, records, _converting(total=1))
    assert mgr.estimate_remaining_seconds() == pytest.approx(2.0)


# ── size proportionality ──────────────────────────────────────────────────────

def test_larger_image_gets_proportionally_larger_estimate(app_config: AppConfig):
    """Two pending images: the 4× larger one must get a 4× larger estimate."""
    # Establish rate: 1000 px → 1.0 s → 0.001 s/px
    records = [
        _rec("done.png",  1000, "ok",      conversion_seconds=1.0, width=1000, height=1),
        _rec("small.png",  500, "pending",                          width=500,  height=1),
        _rec("large.png", 2000, "pending",                          width=2000, height=1),
    ]
    mgr_small = _manager_with(app_config, [records[0], records[1]], _converting(total=1))
    mgr_large = _manager_with(app_config, [records[0], records[2]], _converting(total=1))
    eta_small = mgr_small.estimate_remaining_seconds()
    eta_large = mgr_large.estimate_remaining_seconds()
    assert eta_small == pytest.approx(0.5)
    assert eta_large == pytest.approx(2.0)
    assert eta_large / eta_small == pytest.approx(4.0)


def test_estimates_sum_across_pending_images(app_config: AppConfig):
    """Total estimate is the per-image sum, not an average."""
    # Rate: 1000 px → 2.0 s → 0.002 s/px
    # Pending: 500 px (1.0 s) + 3000 px (6.0 s) = 7.0 s total
    records = [
        _rec("done.png",  1000, "ok",      conversion_seconds=2.0, width=1000, height=1),
        _rec("a.png",      500, "pending",                          width=500,  height=1),
        _rec("b.png",     3000, "pending",                          width=3000, height=1),
    ]
    mgr = _manager_with(app_config, records, _converting(total=2))
    assert mgr.estimate_remaining_seconds() == pytest.approx(7.0)


# ── rate from multiple converted images ───────────────────────────────────────

def test_rate_is_total_time_over_total_pixels(app_config: AppConfig):
    """Rate = Σtime / Σpixels — not average of per-image rates.

    Two converted images with very different sizes:
      small:  100 px → 0.1 s  (per-image rate: 0.001 s/px)
      large: 9900 px → 9.9 s  (per-image rate: 0.001 s/px)
    Σtime=10.0 s, Σpx=10000 → rate = 0.001 s/px.
    Pending 5000 px → 5.0 s.
    """
    records = [
        _rec("s.png", 1000, "ok",      conversion_seconds=0.1, width=100,  height=1),
        _rec("l.png", 1000, "ok",      conversion_seconds=9.9, width=9900, height=1),
        _rec("p.png", 1000, "pending",                          width=5000, height=1),
    ]
    mgr = _manager_with(app_config, records, _converting(total=1))
    assert mgr.estimate_remaining_seconds() == pytest.approx(5.0)


def test_rate_weighted_by_pixels_not_count(app_config: AppConfig):
    """Σtime/Σpx differs from mean(time/px) when pixel counts vary.

    Image A:  100 px → 1.0 s  → individual rate 0.010 s/px
    Image B:  900 px → 0.9 s  → individual rate 0.001 s/px
    mean(individual rates) = 0.0055 s/px  ← WRONG
    Σtime/Σpx = 1.9/1000 = 0.0019 s/px   ← correct (pixel-weighted)

    Pending 1000 px should use the pixel-weighted rate → 1.9 s.
    """
    records = [
        _rec("a.png", 1000, "ok",      conversion_seconds=1.0, width=100,  height=1),
        _rec("b.png", 1000, "ok",      conversion_seconds=0.9, width=900,  height=1),
        _rec("p.png", 1000, "pending",                          width=1000, height=1),
    ]
    mgr = _manager_with(app_config, records, _converting(total=1))
    assert mgr.estimate_remaining_seconds() == pytest.approx(1.9)


# ── non-pending statuses are ignored ─────────────────────────────────────────

def test_failed_images_not_counted_in_estimate(app_config: AppConfig):
    """Failed images (no dims, no timing) are excluded from both rate and sum."""
    records = [
        _rec("done.png",   1000, "ok",     conversion_seconds=2.0, width=1000, height=1),
        _rec("broken.png", 5000, "failed"),
        _rec("p.png",      1000, "pending",                         width=1000, height=1),
    ]
    mgr = _manager_with(app_config, records, _converting(total=1))
    # Rate = 0.002 s/px; pending 1000 px → 2.0 s
    assert mgr.estimate_remaining_seconds() == pytest.approx(2.0)


def test_ok_images_not_double_counted(app_config: AppConfig):
    """Already-converted images contribute to rate but not to the remaining sum."""
    records = [
        _rec("done1.png", 1000, "ok", conversion_seconds=1.0, width=1000, height=1),
        _rec("done2.png", 1000, "ok", conversion_seconds=1.0, width=1000, height=1),
        _rec("p.png",     1000, "pending",                     width=1000, height=1),
    ]
    mgr = _manager_with(app_config, records, _converting(total=1))
    # Rate = 2.0 s / 2000 px = 0.001 s/px; pending 1000 px → 1.0 s
    assert mgr.estimate_remaining_seconds() == pytest.approx(1.0)


def test_pending_without_dims_returns_none(app_config: AppConfig):
    """Pending images without pixel dimensions will never convert → None."""
    records = [
        _rec("done.png",    1000, "ok",      conversion_seconds=2.0, width=1000, height=1),
        _rec("pending.png", 1000, "pending"),  # no dims — failed to open
    ]
    mgr = _manager_with(app_config, records, _converting(total=1))
    assert mgr.estimate_remaining_seconds() is None


def test_no_converted_with_dims_returns_none(app_config: AppConfig):
    """No timing data from pixel-dimensioned images → None (no bytes fallback)."""
    records = [
        _rec("done.png",    1000, "ok",      conversion_seconds=4.0),  # old row, no dims
        _rec("pending.png", 1000, "pending",                            width=500, height=1),
    ]
    mgr = _manager_with(app_config, records, _converting(total=1))
    assert mgr.estimate_remaining_seconds() is None


# ── pixel-based estimation ────────────────────────────────────────────────────

def test_pixel_rate_used_when_dims_available(app_config: AppConfig):
    """When pixel dimensions are known, rate is seconds/pixel not seconds/byte."""
    # 100×100 px converted in 1.0 s → rate = 0.0001 s/px
    # Pending: 200×200 px → 0.0001 × 40000 = 4.0 s
    records = [
        _rec("done.png",    5000, "ok",      conversion_seconds=1.0, width=100, height=100),
        _rec("pending.png", 9000, "pending",                          width=200, height=200),
    ]
    mgr = _manager_with(app_config, records, _converting(total=1))
    assert mgr.estimate_remaining_seconds() == pytest.approx(4.0)


def test_pixel_estimate_scales_with_pixel_count_not_file_size(app_config: AppConfig):
    """Two pending images with the same file size but different resolutions
    get different estimates when pixel dimensions are known."""
    # Rate: 1000×1000 px → 2.0 s → 0.000002 s/px
    # Small: 500×500 (250 000 px) → 0.5 s  (same file size as large)
    # Large: 2000×500 (1 000 000 px) → 2.0 s
    records = [
        _rec("ref.png",   8000, "ok",      conversion_seconds=2.0, width=1000, height=1000),
        _rec("small.png", 4000, "pending",                          width=500,  height=500),
        _rec("large.png", 4000, "pending",                          width=2000, height=500),
    ]
    mgr_small = _manager_with(app_config, [records[0], records[1]], _converting(total=1))
    mgr_large = _manager_with(app_config, [records[0], records[2]], _converting(total=1))
    eta_small = mgr_small.estimate_remaining_seconds()
    eta_large = mgr_large.estimate_remaining_seconds()
    assert eta_small == pytest.approx(0.5)
    assert eta_large == pytest.approx(2.0)
    # Ratio matches pixel ratio (1M / 250K = 4×), not file size ratio (1×)
    assert eta_large / eta_small == pytest.approx(4.0)


def test_pixel_sum_across_multiple_pending(app_config: AppConfig):
    """Total estimate sums per-image pixel estimates."""
    # Rate: 1000×1000 px → 1.0 s → 0.000001 s/px
    # Pending A: 200×300 (60 000 px) → 0.06 s
    # Pending B: 1000×500 (500 000 px) → 0.5 s
    # Total: 0.56 s
    records = [
        _rec("ref.png", 1000, "ok",      conversion_seconds=1.0, width=1000, height=1000),
        _rec("a.png",   2000, "pending",                          width=200,  height=300),
        _rec("b.png",   8000, "pending",                          width=1000, height=500),
    ]
    mgr = _manager_with(app_config, records, _converting(total=2))
    assert mgr.estimate_remaining_seconds() == pytest.approx(0.56)


