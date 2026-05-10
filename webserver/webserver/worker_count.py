"""Resolve the configured image-worker count to an actual integer.

Auto mode (configured == 0):
    workers = min(cpu_count - 1, (available_ram - 250 MB) // 50 MB)
    clamped to at least 1.

Serial mode (configured == 1):
    always returns 1 (legacy behaviour, default).

Manual mode (configured >= 2):
    returns configured verbatim; user's responsibility to have enough RAM.
"""
from __future__ import annotations

import os

import psutil


def resolve_worker_count(configured: int) -> int:
    """Map a configured worker count to the actual number to use.

    Parameters
    ----------
    configured:
        0  → auto-detect based on CPU count and available RAM.
        1  → serial (legacy default).
        N  → use N workers; the caller is responsible for having enough RAM.

    Returns
    -------
    int >= 1
    """
    if configured >= 1:
        return configured

    # Auto mode: pick the lower of (cores − 1) and what RAM can support.
    cpu_workers = max(1, (os.cpu_count() or 2) - 1)

    avail = psutil.virtual_memory().available
    _OS_RESERVE = 250 * 1024 * 1024   # 250 MB kept for OS / other processes
    _PER_WORKER  =  50 * 1024 * 1024  # 50 MB additional RSS per forked worker
    ram_workers = max(1, (avail - _OS_RESERVE) // _PER_WORKER)

    return max(1, min(cpu_workers, int(ram_workers)))
