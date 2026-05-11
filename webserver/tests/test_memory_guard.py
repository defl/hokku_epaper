"""Tests for memory_guard.memory_limit().

On Linux/macOS the context manager enforces a hard RLIMIT_AS ceiling;
allocating past it raises MemoryError.  On Windows it is a no-op — the
test verifies it is at least importable and doesn't error.
"""
from __future__ import annotations

import sys

import numpy as np
import psutil
import pytest

from hokku_server.memory_guard import memory_limit, supported


def test_supported_matches_platform() -> None:
    if sys.platform == "win32":
        assert not supported()
    else:
        assert supported()


def test_noop_on_windows() -> None:
    """On Windows, memory_limit must not raise under any allocation."""
    if sys.platform != "win32":
        pytest.skip("Windows-only path")
    # 1 MB limit — would fail if enforced; should be a no-op.
    with memory_limit(1 * 1024 * 1024):
        arr = np.zeros(10 * 1024 * 1024, dtype=np.uint8)  # 10 MB
    assert arr is not None


@pytest.mark.skipif(sys.platform == "win32", reason="RLIMIT_AS not available on Windows")
def test_large_alloc_within_limit_succeeds() -> None:
    """An allocation that fits within the limit must succeed."""


    baseline = psutil.Process().memory_info().vms
    # Allow 200 MB above current virtual size.
    limit = baseline + 200 * 1024 * 1024
    with memory_limit(limit):
        arr = np.zeros(1 * 1024 * 1024, dtype=np.uint8)  # 1 MB — well within budget
    assert len(arr) > 0


@pytest.mark.skipif(sys.platform == "win32", reason="RLIMIT_AS not available on Windows")
def test_excessive_alloc_raises_memory_error() -> None:
    """An allocation that exceeds the virtual-address ceiling must raise MemoryError."""


    baseline = psutil.Process().memory_info().vms
    # Grant only 5 MB above current virtual size — far too little for a 100 MB array.
    tight_limit = baseline + 5 * 1024 * 1024
    with pytest.raises((MemoryError, np.core._exceptions._ArrayMemoryError)):
        with memory_limit(tight_limit):
            _arr = np.zeros(100 * 1024 * 1024, dtype=np.uint8)  # 100 MB


@pytest.mark.skipif(sys.platform == "win32", reason="RLIMIT_AS not available on Windows")
def test_limit_restored_after_context() -> None:
    """After exiting the context manager the old RLIMIT_AS must be restored."""
    import resource

    before = resource.getrlimit(resource.RLIMIT_AS)


    baseline = psutil.Process().memory_info().vms
    tight = baseline + 5 * 1024 * 1024
    try:
        with memory_limit(tight):
            pass
    except Exception:
        pass

    after = resource.getrlimit(resource.RLIMIT_AS)
    assert after == before, f"limit not restored: {before!r} → {after!r}"
