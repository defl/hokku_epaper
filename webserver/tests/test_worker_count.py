"""Unit tests for worker_count.resolve_worker_count()."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from webserver.worker_count import resolve_worker_count


# ── configured >= 1 returns literally ─────────────────────────────────────────

def test_serial_returns_1():
    assert resolve_worker_count(1) == 1


def test_explicit_2_returns_2():
    assert resolve_worker_count(2) == 2


def test_explicit_high_returns_same():
    assert resolve_worker_count(16) == 16


# ── auto mode (configured == 0) ───────────────────────────────────────────────

def _mock_psutil(available_bytes: int):
    """Return a context-manager patch that sets psutil available RAM."""
    vm = MagicMock()
    vm.available = available_bytes
    return patch("webserver.worker_count.psutil.virtual_memory", return_value=vm)


def test_auto_normal_host():
    """4 cores, 8 GB free → min(3, 153) = 3."""
    with patch("os.cpu_count", return_value=4), _mock_psutil(8 * 1024**3):
        assert resolve_worker_count(0) == 3


def test_auto_ram_capped():
    """4 cores, only 190 MB free → (190-100)//30 = 3 → min(3, 3) = 3."""
    with patch("os.cpu_count", return_value=4), _mock_psutil(190 * 1024 * 1024):
        assert resolve_worker_count(0) == 3


def test_auto_ram_very_low_clamps_to_1():
    """100 MB free (0 MB headroom) → ram_workers clamped to 1 → 1."""
    with patch("os.cpu_count", return_value=4), _mock_psutil(100 * 1024 * 1024):
        assert resolve_worker_count(0) == 1


def test_auto_single_core_clamps_to_1():
    """cpu_count == 1 → cpu_workers = max(1, 0) = 1."""
    with patch("os.cpu_count", return_value=1), _mock_psutil(8 * 1024**3):
        assert resolve_worker_count(0) == 1


def test_auto_none_cpu_count():
    """os.cpu_count() may return None on exotic systems; treat as 2."""
    with patch("os.cpu_count", return_value=None), _mock_psutil(8 * 1024**3):
        result = resolve_worker_count(0)
        assert result >= 1  # max(1, 2-1) = 1


def test_auto_result_always_positive():
    """resolve_worker_count(0) must never return 0 or less."""
    with patch("os.cpu_count", return_value=2), _mock_psutil(1):
        assert resolve_worker_count(0) >= 1
