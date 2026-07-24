"""Resolve the configured image-worker count to an actual integer.

Auto mode (configured == 0):
    workers = min(cpu_count - 1, (available_ram - 100 MB) // 220 MB)
    clamped to at least 1.

    The sizing constant is the *active-render peak*, not the idle-thread
    overhead. Threads share the interpreter, so an extra idle worker only adds
    ~14 MB — but that's the wrong number to budget against. When N workers all
    render at once, each independently decodes + preprocesses (face detect,
    orientation crop) a full-resolution source before the streaming dither, and
    that transient peak is ~150-220 MB per concurrent render for a large photo.
    Budgeting on the old ~30 MB idle figure let a 464 MB Pi Zero 2 W pick 3
    workers, and three large images landing together OOM-killed the server. At
    220 MB/worker a 464 MB board resolves to 1 (serial); roomier machines still
    parallelise up to cpu_count - 1.

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
    _OS_RESERVE = 100 * 1024 * 1024  # 100 MB headroom for OS + Flask
    _PER_WORKER = 220 * 1024 * 1024  # active-render peak (decode+preprocess) per concurrent render
    ram_workers = max(1, (avail - _OS_RESERVE) // _PER_WORKER)

    return max(1, min(cpu_workers, int(ram_workers)))
