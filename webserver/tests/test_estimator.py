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
from webserver.image_manager import ConversionProgress, ImageManager, ImageRecord


# ── helpers ───────────────────────────────────────────────────────────────────

def _rec(
    name: str,
    size_bytes: int,
    status: str = "ok",
    conversion_seconds: float | None = None,
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
    )


def _manager_with(app_config: AppConfig, records: list[ImageRecord], progress: ConversionProgress) -> ImageManager:
    """Build an ImageManager and inject synthetic records + progress."""
    mgr = ImageManager(app_config)
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
    # 2000 bytes converted in 4.0 s → rate = 0.002 s/byte
    # Pending: 1000 bytes → estimate = 2.0 s
    records = [
        _rec("done.png",    2000, "ok",      conversion_seconds=4.0),
        _rec("pending.png", 1000, "pending"),
    ]
    mgr = _manager_with(app_config, records, _converting(total=1))
    eta = mgr.estimate_remaining_seconds()
    assert eta == pytest.approx(2.0)


# ── size proportionality ──────────────────────────────────────────────────────

def test_larger_image_gets_proportionally_larger_estimate(app_config: AppConfig):
    """Two pending images: the 4× larger one must get a 4× larger estimate."""
    # Establish rate: 1000 bytes → 1.0 s → 0.001 s/byte
    records = [
        _rec("done.png",  1000, "ok",      conversion_seconds=1.0),
        _rec("small.png",  500, "pending"),
        _rec("large.png", 2000, "pending"),
    ]
    mgr = _manager_with(app_config, records, _converting(total=2))
    # Can't inspect per-image breakdowns directly, but the ratio must hold.
    # Build individual managers to isolate each pending image.
    mgr_small = _manager_with(app_config, [records[0], records[1]], _converting(total=1))
    mgr_large = _manager_with(app_config, [records[0], records[2]], _converting(total=1))
    eta_small = mgr_small.estimate_remaining_seconds()
    eta_large = mgr_large.estimate_remaining_seconds()
    assert eta_small == pytest.approx(0.5)
    assert eta_large == pytest.approx(2.0)
    assert eta_large / eta_small == pytest.approx(4.0)


def test_estimates_sum_across_pending_images(app_config: AppConfig):
    """Total estimate is the per-image sum, not an average."""
    # Rate: 1000 bytes → 2.0 s → 0.002 s/byte
    # Pending: 500 bytes (1.0 s) + 3000 bytes (6.0 s) = 7.0 s total
    records = [
        _rec("done.png",  1000, "ok",      conversion_seconds=2.0),
        _rec("a.png",      500, "pending"),
        _rec("b.png",     3000, "pending"),
    ]
    mgr = _manager_with(app_config, records, _converting(total=2))
    assert mgr.estimate_remaining_seconds() == pytest.approx(7.0)


# ── rate from multiple converted images ───────────────────────────────────────

def test_rate_is_total_time_over_total_bytes(app_config: AppConfig):
    """Rate = Σtime / Σbytes — not average of per-image rates.

    Two converted images with very different sizes:
      small:  100 bytes → 0.1 s  (per-image rate: 0.001 s/byte)
      large: 9900 bytes → 9.9 s  (per-image rate: 0.001 s/byte)
    Σtime=10.0 s, Σbytes=10000 bytes → rate = 0.001 s/byte.
    Pending 5000 bytes → 5.0 s.

    A naive average-of-rates would give the same answer here, so we also
    test with deliberately unequal per-image rates below.
    """
    records = [
        _rec("s.png",  100, "ok",      conversion_seconds=0.1),
        _rec("l.png", 9900, "ok",      conversion_seconds=9.9),
        _rec("p.png", 5000, "pending"),
    ]
    mgr = _manager_with(app_config, records, _converting(total=1))
    assert mgr.estimate_remaining_seconds() == pytest.approx(5.0)


def test_rate_weighted_by_size_not_count(app_config: AppConfig):
    """Σtime/Σbytes differs from mean(time/size) when sizes vary.

    Image A: 100 bytes → 1.0 s  → individual rate 0.010 s/byte
    Image B: 900 bytes → 0.9 s  → individual rate 0.001 s/byte
    mean(individual rates) = 0.0055 s/byte  ← WRONG
    Σtime/Σbytes = 1.9/1000 = 0.0019 s/byte  ← correct (size-weighted)

    Pending 1000 bytes should use the size-weighted rate → 1.9 s.
    """
    records = [
        _rec("a.png",  100, "ok",      conversion_seconds=1.0),
        _rec("b.png",  900, "ok",      conversion_seconds=0.9),
        _rec("p.png", 1000, "pending"),
    ]
    mgr = _manager_with(app_config, records, _converting(total=1))
    assert mgr.estimate_remaining_seconds() == pytest.approx(1.9)


# ── non-pending statuses are ignored ─────────────────────────────────────────

def test_failed_images_not_counted_in_estimate(app_config: AppConfig):
    """Failed images have no last_conversion_seconds and are not pending."""
    records = [
        _rec("done.png",   1000, "ok",     conversion_seconds=2.0),
        _rec("broken.png", 5000, "failed"),
        _rec("p.png",      1000, "pending"),
    ]
    mgr = _manager_with(app_config, records, _converting(total=1))
    # Only p.png (1000 bytes) at 0.002 s/byte → 2.0 s
    assert mgr.estimate_remaining_seconds() == pytest.approx(2.0)


def test_ok_images_not_double_counted(app_config: AppConfig):
    """Already-converted images contribute to rate but not to the remaining sum."""
    records = [
        _rec("done1.png", 1000, "ok", conversion_seconds=1.0),
        _rec("done2.png", 1000, "ok", conversion_seconds=1.0),
        _rec("p.png",     1000, "pending"),
    ]
    mgr = _manager_with(app_config, records, _converting(total=1))
    # Rate = 2.0 s / 2000 bytes = 0.001 s/byte; pending = 1000 bytes → 1.0 s
    assert mgr.estimate_remaining_seconds() == pytest.approx(1.0)
