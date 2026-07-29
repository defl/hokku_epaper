"""Cgroup-aware detection of the memory and CPU actually available to us.

``psutil.virtual_memory()`` and ``os.cpu_count()`` report the *host* machine,
not the limit of the cgroup the process runs in. Under
``docker run --memory=300m --cpus=2`` on a 24-core / 2 GB host they still say
"24 cores, 2 GB" — so the server sizes its worker pool and decode budget to the
host and gets OOM-killed the moment it actually uses that memory. The same blind
spot hides a Kubernetes/systemd ``MemoryMax=`` limit.

The cgroup pseudo-files DO carry the real limit, so we read them and take the
minimum with what psutil/os report:

  memory  cgroup v2: /sys/fs/cgroup/memory.max            ("max" = unlimited)
          cgroup v1: /sys/fs/cgroup/memory/memory.limit_in_bytes (huge = unlimited)
  cpu     cgroup v2: /sys/fs/cgroup/cpu.max               ("max <period>" = unlimited)
          cgroup v1: /sys/fs/cgroup/cpu/cpu.cfs_quota_us / cpu.cfs_period_us (-1 = unlimited)

All reads are best-effort: on a bare host (or Windows/macOS, where these paths
don't exist) the cgroup source is simply absent and we fall back to psutil/os.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import psutil

# cgroup v1 stores "unlimited" as a near-INT64_MAX sentinel (commonly
# 0x7FFFFFFFFFFFF000). Anything at/above this is treated as "no limit".
_CGROUP_V1_UNLIMITED = 0x7FFFFFFFFFFF000


def _read_int(path: str) -> int | None:
    """Read a single integer from a cgroup file, or None if absent/unreadable."""
    try:
        text = Path(path).read_text().strip()
    except (OSError, ValueError):
        return None
    if not text or text == "max":  # cgroup v2 "unlimited"
        return None
    try:
        return int(text.split()[0])
    except (ValueError, IndexError):
        return None


def _cgroup_memory_limit_bytes() -> int | None:
    """The cgroup memory ceiling in bytes, or None if unlimited/unavailable."""
    # cgroup v2
    v2 = _read_int("/sys/fs/cgroup/memory.max")
    if v2 is not None:
        return v2
    # cgroup v1
    v1 = _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if v1 is not None and v1 < _CGROUP_V1_UNLIMITED:
        return v1
    return None


def _cgroup_cpu_limit() -> float | None:
    """The cgroup CPU ceiling as a fractional core count, or None if unlimited."""
    # cgroup v2: "<quota> <period>" (or "max <period>")
    try:
        text = Path("/sys/fs/cgroup/cpu.max").read_text().strip()
        quota_s, period_s = text.split()[:2]
        if quota_s != "max":
            quota, period = int(quota_s), int(period_s)
            if quota > 0 and period > 0:
                return quota / period
        else:
            return None  # explicitly unlimited
    except (OSError, ValueError):
        pass
    # cgroup v1
    quota = _read_int("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    period = _read_int("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if quota is not None and quota > 0 and period:
        return quota / period
    return None


def detect_memory_limit_bytes() -> int:
    """Total memory this process may actually use — the min of host RAM and any
    cgroup memory limit. This is the number to budget against, not
    ``psutil.virtual_memory().total``.
    """
    host = int(psutil.virtual_memory().total)
    cg = _cgroup_memory_limit_bytes()
    return min(host, cg) if cg is not None else host


def detect_cpu_limit() -> int:
    """Whole CPUs this process may actually use — the min of the online CPU
    count, the scheduler affinity mask, and any cgroup CPU quota. At least 1.

    ``os.cpu_count()`` and ``sched_getaffinity`` both miss a ``--cpus`` (CFS
    quota) limit; the cgroup ``cpu.max`` file is the only place it shows up.
    """
    counts: list[int] = []
    n = os.cpu_count()
    if n:
        counts.append(n)
    # sched_getaffinity is Linux-only (absent on Windows/macOS); fetch via getattr
    # so static analysis doesn't flag the platform-specific attribute.
    sched_getaffinity = getattr(os, "sched_getaffinity", None)
    if sched_getaffinity is not None:
        try:
            counts.append(len(sched_getaffinity(0)))
        except OSError:
            pass
    cg = _cgroup_cpu_limit()
    if cg is not None:
        # Round up: 1.5 cores of quota still lets us keep ~2 threads busy.
        counts.append(max(1, math.ceil(cg)))
    return max(1, min(counts)) if counts else 1
