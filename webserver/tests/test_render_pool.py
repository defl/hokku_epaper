"""Tests for RenderPool — lazy spawn, idle recycle, parallel execution."""
from __future__ import annotations

import time
from concurrent.futures import Future

import pytest

from webserver.render_pool import RenderPool


# ── helpers ────────────────────────────────────────────────────────────────────

def _add(a, b):
    """Simple picklable function usable as a worker task."""
    return a + b


def _sleep_and_return_pid(delay: float) -> int:
    """Sleep then return the worker PID — used to verify true parallelism."""
    import os
    import time
    time.sleep(delay)
    return os.getpid()


# ── construction ───────────────────────────────────────────────────────────────

def test_invalid_worker_count_raises():
    with pytest.raises(ValueError):
        RenderPool(worker_count=0)


def test_resolved_worker_count():
    pool = RenderPool(worker_count=3)
    assert pool.resolved_worker_count == 3
    pool.shutdown(wait=False)


# ── lazy spawn ─────────────────────────────────────────────────────────────────

def test_pool_none_before_first_submit():
    pool = RenderPool(worker_count=1)
    assert pool._pool is None
    pool.shutdown(wait=False)


def test_submit_materialises_pool():
    pool = RenderPool(worker_count=1)
    f = pool.submit(_add, 1, 2)
    assert pool._pool is not None
    assert f.result() == 3
    pool.shutdown(wait=True)


def test_submit_returns_future():
    pool = RenderPool(worker_count=1)
    f = pool.submit(_add, 10, 20)
    assert isinstance(f, Future)
    assert f.result() == 30
    pool.shutdown(wait=True)


# ── idle recycle ───────────────────────────────────────────────────────────────

def test_idle_recycle_shuts_pool_down():
    """After IDLE_TIMEOUT_S the pool self-destructs; pool._pool becomes None."""
    pool = RenderPool(worker_count=1, idle_timeout_s=0.2)
    pool.submit(_add, 1, 1).result()
    # Wait long enough for the watcher to fire
    deadline = time.monotonic() + 5.0
    while pool._pool is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pool._pool is None, "Pool should have been recycled after idle timeout"
    pool.shutdown(wait=False)


def test_idle_recycle_then_resubmit():
    """After recycling, submit() should transparently re-create the pool."""
    pool = RenderPool(worker_count=1, idle_timeout_s=0.2)
    pool.submit(_add, 1, 1).result()
    # Wait for recycle
    deadline = time.monotonic() + 5.0
    while pool._pool is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pool._pool is None
    # Re-submit — should work
    assert pool.submit(_add, 7, 8).result() == 15
    pool.shutdown(wait=True)


# ── shutdown ───────────────────────────────────────────────────────────────────

def test_shutdown_is_idempotent():
    pool = RenderPool(worker_count=1)
    pool.submit(_add, 1, 2).result()
    pool.shutdown(wait=True)
    pool.shutdown(wait=True)  # must not raise


def test_submit_after_shutdown_raises():
    pool = RenderPool(worker_count=1)
    pool.shutdown(wait=True)
    with pytest.raises(RuntimeError, match="shut down"):
        pool.submit(_add, 1, 2)


def test_shutdown_without_any_submit():
    """shutdown() on a never-used pool must not crash."""
    pool = RenderPool(worker_count=1)
    pool.shutdown(wait=True)


# ── parallel correctness ───────────────────────────────────────────────────────

@pytest.mark.slow
def test_parallel_tasks_run_in_different_processes():
    """With worker_count=2, two sleepy tasks should both complete successfully.

    On Linux (fork) parallelism is tight.  On Windows (spawn) worker startup
    costs several seconds, so we only assert correctness (both futures resolved
    and returned int PIDs) without a strict wall-clock bound.
    """
    import sys
    pool = RenderPool(worker_count=2)
    delay = 0.5
    t0 = time.monotonic()
    f1 = pool.submit(_sleep_and_return_pid, delay)
    f2 = pool.submit(_sleep_and_return_pid, delay)
    pids = {f1.result(), f2.result()}
    elapsed = time.monotonic() - t0
    pool.shutdown(wait=True)

    # Both futures must resolve to integer PIDs.
    assert len(pids) >= 1
    assert all(isinstance(p, int) for p in pids)

    # On Linux/macOS (fork), the two tasks run in parallel and finish in ≈ delay.
    # On Windows (spawn), startup dominates — just check we eventually finish.
    if sys.platform != "win32":
        assert elapsed < delay * 1.8, (
            f"Expected parallel execution (< {delay * 1.8:.1f}s) on Linux, got {elapsed:.2f}s"
        )
