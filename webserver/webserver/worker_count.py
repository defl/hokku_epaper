"""Resolve the configured image-worker count to an actual integer.

Auto mode (configured == 0):
    workers = min(cpu_count - 1, (available_ram - 100 MB) // 30 MB)
    clamped to at least 1.

    Constants tuned for the thread-pool model: threads share the Python
    interpreter, so the incremental RSS per extra worker is ~14 MB on a
    Pi Zero 2 W under load (measured: 166 MB idle → 209 MB with 3 workers).
    The old 50 MB / 250 MB figures were for the process-pool model where
    each worker forked the full interpreter.

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
    _OS_RESERVE = 100 * 1024 * 1024   # 100 MB headroom for OS + Flask
    _PER_WORKER =  30 * 1024 * 1024   # ~30 MB incremental RSS per thread
    ram_workers = max(1, (avail - _OS_RESERVE) // _PER_WORKER)

    return max(1, min(cpu_workers, int(ram_workers)))
