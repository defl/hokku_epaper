"""Process-level memory ceiling for the dither pipeline.

The streaming render pipeline is designed to fit each render in ~50 MB. This
module provides a *hard* guarantee: an allocation that exceeds the budget
raises ``MemoryError`` rather than triggering the OOM killer.

The mechanism is ``setrlimit(RLIMIT_AS)`` — the kernel refuses to map any
new pages once the process's virtual address space exceeds the soft limit.
Available on Linux and macOS; on Windows ``RLIMIT_AS`` doesn't exist so the
context manager is a no-op (the streaming design still keeps RSS bounded,
but exceeding the budget would manifest as system swap pressure / OOM,
not a clean exception).

Note: ``RLIMIT_AS`` is **process-wide**, not per-thread. To enforce a
per-thread budget when running multiple workers in parallel, set the limit
to ``N × per_thread_budget + baseline_overhead`` — that's the responsibility
of the worker-pool driver, not this module.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator


def _resource_module():
    """Return the ``resource`` module if available on this platform, else None."""
    if sys.platform == "win32":
        return None
    try:
        import resource
        return resource
    except ImportError:
        return None


def supported() -> bool:
    """True iff RLIMIT_AS is available on this platform."""
    return _resource_module() is not None


def baseline_rss_bytes() -> int:
    """Return current RSS in bytes (baseline you'd add to the per-render budget).

    On Linux, ``ru_maxrss`` is in KiB; on macOS it's in bytes — we use psutil
    if available to side-step that footgun.
    """
    try:
        import psutil
        return int(psutil.Process().memory_info().rss)
    except ImportError:
        res = _resource_module()
        if res is None:
            return 0
        # Linux ru_maxrss is KiB; we just return that as a rough fallback.
        return int(res.getrusage(res.RUSAGE_SELF).ru_maxrss * 1024)


@contextmanager
def memory_limit(max_address_space_bytes: int) -> Iterator[None]:
    """Cap the process's virtual address space to *max_address_space_bytes*.

    Inside the block, any allocation that would push virtual memory above
    the cap raises ``MemoryError``. On exit, the previous limit is restored.

    No-op on platforms without ``RLIMIT_AS``.

    Caller's responsibility to size the limit including all process-wide
    overhead (Python heap, imported modules, cached LUTs, etc.) plus the
    per-render headroom you want.
    """
    res = _resource_module()
    if res is None:
        yield
        return
    try:
        prev = res.getrlimit(res.RLIMIT_AS)
    except (ValueError, OSError):
        yield
        return
    try:
        res.setrlimit(res.RLIMIT_AS, (int(max_address_space_bytes), prev[1]))
    except (ValueError, OSError):
        # Some kernels reject setrlimit when the soft limit is below the
        # current RSS. Don't fail the render; the streaming design still
        # keeps memory bounded, just without the hard guarantee.
        yield
        return
    try:
        yield
    finally:
        try:
            res.setrlimit(res.RLIMIT_AS, prev)
        except (ValueError, OSError):
            pass
