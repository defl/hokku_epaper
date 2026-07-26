"""Unit tests for resource_budget: deriving decode budget + workers from memory."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hokku.webserver import resource_budget as rb
from hokku.webserver.resource_budget import (
    BASELINE_RSS_BYTES,
    DECODE_BYTES_PER_PIXEL,
    MAX_DECODE_BUDGET_PIXELS,
    MIN_MEMORY_BUDGET_MB,
    PER_RENDER_BYTES,
    compute_budget,
    effective_memory_bytes,
    memory_budget_too_low,
    resolve_decode_budget_pixels,
    resolve_decode_concurrency,
    resolve_worker_count,
)

_MB = 1024 * 1024

# ── worker count ──────────────────────────────────────────────────────────────


def test_workers_pi_zero_2w_is_serial():
    # 464 MB, 16 MP decode reserved (128 MB) → (464-336-128)/220 = 0 → 1 worker.
    assert (
        resolve_worker_count(memory_bytes=464 * _MB, cpu_count=4, decode_budget_pixels=16_000_000)
        == 1
    )


def test_workers_cpu_capped():
    # Plenty of RAM, few CPUs → CPU is the limit.
    assert (
        resolve_worker_count(
            memory_bytes=32 * 1024**3, cpu_count=2, decode_budget_pixels=40_000_000
        )
        == 2
    )


def test_workers_memory_capped():
    # 1 GB, 24 CPUs, 40 MP decode reserved (320 MB): (1024-336-320)/220 = 1
    # additional → 1 base + 1 = 2 workers.
    assert (
        resolve_worker_count(memory_bytes=1024 * _MB, cpu_count=24, decode_budget_pixels=40_000_000)
        == 2
    )


def test_workers_big_decode_reserve_reduces_parallelism():
    # Reserving a big decode budget shrinks the worker count vs a tiny one.
    lots = resolve_worker_count(memory_bytes=4096 * _MB, cpu_count=24, decode_budget_pixels=0)
    fewer = resolve_worker_count(
        memory_bytes=4096 * _MB, cpu_count=24, decode_budget_pixels=40_000_000
    )
    assert fewer < lots


def test_workers_below_baseline_clamps_to_1():
    assert resolve_worker_count(memory_bytes=300 * _MB, cpu_count=8, decode_budget_pixels=0) == 1


def test_workers_always_at_least_1():
    assert resolve_worker_count(memory_bytes=1, cpu_count=1, decode_budget_pixels=0) == 1


# ── decode concurrency (admission slots) ──────────────────────────────────────


def test_decode_concurrency_single_worker_is_serial():
    # The appliance: one worker → one decode at a time, unchanged behaviour.
    assert (
        resolve_decode_concurrency(
            memory_bytes=464 * _MB, worker_count=1, decode_budget_pixels=16_000_000
        )
        == 1
    )


def test_decode_concurrency_cheap_decode_allows_all_workers():
    # When a decode is no costlier than a render (decode_peak <= PER_RENDER),
    # every worker may decode concurrently.
    decode_peak_eq_render = PER_RENDER_BYTES // DECODE_BYTES_PER_PIXEL
    assert (
        resolve_decode_concurrency(
            memory_bytes=64 * 1024**3,
            worker_count=5,
            decode_budget_pixels=decode_peak_eq_render,
        )
        == 5
    )


def test_decode_concurrency_medium_box_allows_two():
    # 1 GB / worker_count 2 / 40 MP: the one reserved decode plus leftover
    # headroom fits a second concurrent decode.
    assert (
        resolve_decode_concurrency(
            memory_bytes=1024 * _MB, worker_count=2, decode_budget_pixels=40_000_000
        )
        == 2
    )


def test_decode_concurrency_never_exceeds_workers():
    k = resolve_decode_concurrency(
        memory_bytes=64 * 1024**3, worker_count=3, decode_budget_pixels=40_000_000
    )
    assert 1 <= k <= 3


@pytest.mark.parametrize("mb", [512, 768, 1024, 2048, 4096, 8192])
def test_compute_budget_decode_slots_are_memory_safe(mb):
    # Provable invariant: the worst case of decode_slots concurrent decodes plus
    # the remaining workers rendering must fit the usable budget. If this ever
    # fails, concurrent decodes could OOM the box.
    with (
        patch.object(rb, "detect_memory_limit_bytes", return_value=mb * _MB),
        patch.object(rb, "detect_cpu_limit", return_value=24),
    ):
        b = compute_budget(0)
    assert 1 <= b.decode_slots <= b.worker_count
    decode_peak = b.decode_budget_pixels * DECODE_BYTES_PER_PIXEL
    usable = b.memory_bytes - BASELINE_RSS_BYTES
    worst = b.decode_slots * decode_peak + (b.worker_count - b.decode_slots) * PER_RENDER_BYTES
    assert worst <= usable


# ── decode budget ─────────────────────────────────────────────────────────────


def test_decode_budget_pi_matches_legacy_16mp():
    # The whole calibration: 464 MB reproduces the proven ~16 MP Pi budget.
    px = resolve_decode_budget_pixels(464 * _MB)
    assert 15_000_000 <= px <= 18_000_000


def test_decode_budget_scales_up_and_caps_at_bomb_limit():
    # A big box gets the full bomb cap, so a 38.9 MP panorama is accepted.
    assert resolve_decode_budget_pixels(4096 * _MB) == MAX_DECODE_BUDGET_PIXELS
    assert MAX_DECODE_BUDGET_PIXELS >= 38_948_846  # Albi HEIF


def test_decode_budget_below_baseline_is_zero():
    # Below the fixed baseline: refuse every decode (stay up, don't OOM).
    assert resolve_decode_budget_pixels(300 * _MB) == 0
    assert resolve_decode_budget_pixels(BASELINE_RSS_BYTES) == 0


def test_decode_budget_monotonic_until_cap():
    a = resolve_decode_budget_pixels(512 * _MB)
    b = resolve_decode_budget_pixels(768 * _MB)
    assert 0 < a < b <= MAX_DECODE_BUDGET_PIXELS


# ── effective memory (auto vs explicit cap) ───────────────────────────────────


def test_effective_memory_auto_uses_detected():
    with patch.object(rb, "detect_memory_limit_bytes", return_value=2000 * _MB):
        val, auto = effective_memory_bytes(0)
        assert auto is True
        assert val == 2000 * _MB


def test_effective_memory_cap_honoured_when_below_detected():
    with patch.object(rb, "detect_memory_limit_bytes", return_value=2000 * _MB):
        val, auto = effective_memory_bytes(512)
        assert auto is False
        assert val == 512 * _MB


def test_effective_memory_cap_clamped_to_detected():
    # A too-high cap can't exceed physical RAM (no OOM invitation).
    with patch.object(rb, "detect_memory_limit_bytes", return_value=1000 * _MB):
        val, auto = effective_memory_bytes(8000)
        assert auto is False
        assert val == 1000 * _MB


# ── too-low budget floor ──────────────────────────────────────────────────────


def test_memory_budget_too_low():
    assert memory_budget_too_low(0) is False  # auto is never "too low"
    assert memory_budget_too_low(200) is True
    assert memory_budget_too_low(MIN_MEMORY_BUDGET_MB) is False
    assert memory_budget_too_low(MIN_MEMORY_BUDGET_MB - 1) is True
    assert memory_budget_too_low(1024) is False


def test_effective_memory_too_low_cap_coerced_to_auto():
    # A stale/hand-edited cap below the floor is ignored → auto, not honoured
    # (else the server would run in the starved decode-budget-0 state).
    with patch.object(rb, "detect_memory_limit_bytes", return_value=2000 * _MB):
        val, auto = effective_memory_bytes(200)
        assert auto is True
        assert val == 2000 * _MB


# ── compute_budget end to end ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "mb, expect_under, expect_albi_ok",
    [
        (300, True, False),
        (464, False, False),
        (1024, False, True),
        (2048, False, True),
    ],
)
def test_compute_budget_operating_points(mb, expect_under, expect_albi_ok):
    # Model boxes that physically HAVE this much memory (auto-detected), not a
    # user cap — a cap below the floor would be coerced to auto.
    with (
        patch.object(rb, "detect_memory_limit_bytes", return_value=mb * _MB),
        patch.object(rb, "detect_cpu_limit", return_value=24),
    ):
        b = compute_budget(0)
    assert b.memory_bytes == mb * _MB
    assert b.under_provisioned is expect_under
    assert (b.decode_budget_pixels >= 38_948_846) is expect_albi_ok
    assert b.worker_count >= 1


def test_compute_budget_auto_uses_detected_memory():
    with (
        patch.object(rb, "detect_memory_limit_bytes", return_value=1024 * _MB),
        patch.object(rb, "detect_cpu_limit", return_value=24),
    ):
        b = compute_budget(0)
    assert b.memory_auto is True
    assert b.memory_bytes == 1024 * _MB
    assert b.worker_count >= 1
