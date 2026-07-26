"""Derive the image decode budget and render worker count from a memory budget.

This is the single policy layer that turns "how much memory may I use" into the
two numbers the pipeline actually needs:

  * ``decode_budget_pixels`` — the largest un-draftable source we will decode.
    A JPEG shrink-on-loads via draft(), so this caps HEIF/PNG/... full-res
    decodes. Above it, an image is refused at ingest (marked FAILED) instead of
    OOM-killing the server.
  * ``worker_count`` — how many renders may run concurrently.

The memory budget itself comes from :mod:`resource_limits` (cgroup-aware, so a
``docker --memory`` / systemd ``MemoryMax`` / k8s limit is respected), or from
an explicit ``memory_budget_mb`` config cap.

Calibration
-----------
The constants are measured on real hardware (see docs / the e2e memory probes):

  * A warmed server sits at ~290-330 MB RSS (numba JIT + OpenCV + LUTs + the
    lazily-loaded YuNet face model). ``BASELINE_RSS_BYTES`` is the fixed cost
    subtracted before any per-image work.
  * An un-draftable full-res decode costs ~8 B/px (a 38.9 MP HEIF peaked
    ~308 MB). ``DECODE_BYTES_PER_PIXEL``.
  * Each concurrent render transiently peaks ~220 MB over baseline (decode +
    preprocess). ``PER_RENDER_BYTES`` — unchanged from the previous heuristic.

``BASELINE_RSS_BYTES`` is deliberately set so the formula reproduces the proven
Pi Zero 2 W operating point — 464 MB physical → ~16 MP decode budget, 1 worker —
so this change does not regress the appliance. Bigger boxes scale up: a 1 GB+
machine gets the full 40 MP cap and renders a 38.9 MP panorama that the old
hardcoded 16 MP budget wrongly rejected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from hokku.webserver.image_renderer import MAX_IMAGE_PIXELS
from hokku.webserver.resource_limits import detect_cpu_limit, detect_memory_limit_bytes

logger = logging.getLogger(__name__)

_MB = 1024 * 1024

#: Fixed warmed-process overhead subtracted before per-image budgeting.
#: Calibrated to reproduce the Pi's 16 MP @ 464 MB operating point:
#: (464 - 336) MB / 8 B-px ≈ 16 MP. Also ≈ the measured amd64 warmed RSS
#: (~290-330 MB) plus a small margin.
BASELINE_RSS_BYTES = 336 * _MB

#: Transient RAM peak per concurrent render (decode + preprocess). Empirical.
PER_RENDER_BYTES = 220 * _MB

#: Worst-case (HEIF) full-resolution decode cost, bytes per pixel.
DECODE_BYTES_PER_PIXEL = 8

#: The decode budget can never exceed the decompression-bomb cap.
MAX_DECODE_BUDGET_PIXELS = MAX_IMAGE_PIXELS

#: Below this the box can't reliably decode+render even a normal photo; we log a
#: prominent under-provisioned warning (the server still runs and refuses what
#: it can't handle instead of crashing).
MIN_HEALTHY_DECODE_PIXELS = 4_000_000

#: The smallest explicit ``memory_budget_mb`` cap we accept. Below this the
#: derived decode budget collapses toward zero — the server would refuse every
#: image and churn (re-convert-all after a cache clear can then OOM a tight
#: box). An explicit cap under this is rejected at save time; a stale/hand-edited
#: one is coerced back to auto. 0 (auto) is always allowed.
MIN_MEMORY_BUDGET_MB = (
    BASELINE_RSS_BYTES + MIN_HEALTHY_DECODE_PIXELS * DECODE_BYTES_PER_PIXEL
) // _MB


def memory_budget_too_low(memory_budget_mb: int) -> bool:
    """True for an explicit cap below the usable floor (0 = auto is never too low)."""
    return 0 < memory_budget_mb < MIN_MEMORY_BUDGET_MB


@dataclass(frozen=True)
class ResourceBudget:
    """The resolved resource picture for the running process."""

    memory_bytes: int  #: effective memory budget (min of cap and detected)
    memory_auto: bool  #: True if auto-detected, False if an explicit cap
    cpu_count: int  #: cgroup-aware usable CPU count
    decode_budget_pixels: int  #: max un-draftable source we will decode
    worker_count: int  #: concurrent renders allowed
    under_provisioned: bool  #: memory below the healthy floor

    def log_line(self) -> str:
        src = "auto" if self.memory_auto else "cap"
        return (
            f"Resource budget: memory={self.memory_bytes / _MB:.0f} MB ({src}), "
            f"cpu={self.cpu_count}, decode_budget={self.decode_budget_pixels:,} px, "
            f"workers={self.worker_count}"
            + (" [UNDER-PROVISIONED]" if self.under_provisioned else "")
        )


def effective_memory_bytes(memory_budget_mb: int) -> tuple[int, bool]:
    """Resolve the memory budget in bytes and whether it was auto-detected.

    ``memory_budget_mb == 0`` → auto (cgroup-aware detection).
    ``memory_budget_mb >= MIN_MEMORY_BUDGET_MB`` → explicit cap, clamped to
    physically-detected RAM so a too-high value can't invite the OOM killer.
    A cap below the floor (e.g. a stale/hand-edited value) is refused and
    coerced back to auto, so the server never runs in the starved decode-budget-0
    state — the save path (api_config_post) rejects such a value up front.
    """
    detected = detect_memory_limit_bytes()
    if memory_budget_mb and memory_budget_mb >= MIN_MEMORY_BUDGET_MB:
        return min(memory_budget_mb * _MB, detected), False
    if memory_budget_too_low(memory_budget_mb):
        logger.warning(
            "memory_budget_mb=%d is below the %d MB floor — ignoring it and using "
            "auto-detection instead.",
            memory_budget_mb,
            MIN_MEMORY_BUDGET_MB,
        )
    return detected, True


def resolve_worker_count(*, memory_bytes: int, cpu_count: int, decode_budget_pixels: int) -> int:
    """Concurrent-render count — always derived, never manually set.

    Decode is serialized (one at a time via ``_DECODE_LOCK``), so the memory
    worst case is: baseline + one full-size decode in flight + the remaining
    workers doing lighter post-decode renders. We reserve baseline plus one
    worst-case decode, then fit as many additional ``PER_RENDER_BYTES`` renders
    as remain — capped by the usable CPU count, always at least 1. This couples
    the worker count to the decode budget so a big decode budget doesn't let
    parallelism OOM the box.
    """
    decode_reserve = decode_budget_pixels * DECODE_BYTES_PER_PIXEL
    usable = memory_bytes - BASELINE_RSS_BYTES - decode_reserve
    additional = max(0, int(usable // PER_RENDER_BYTES))
    return max(1, min(cpu_count, 1 + additional))


def resolve_decode_budget_pixels(memory_bytes: int) -> int:
    """Largest un-draftable source we can afford to fully decode, in pixels.

    ``(budget - baseline) / bytes-per-pixel``, clamped to [0, bomb cap]. Zero
    means "refuse every decode" — the safe behaviour on a box too small to
    render anything, so it stays up instead of crash-looping on OOM.
    """
    usable = memory_bytes - BASELINE_RSS_BYTES
    if usable <= 0:
        return 0
    return min(MAX_DECODE_BUDGET_PIXELS, int(usable // DECODE_BYTES_PER_PIXEL))


def compute_budget(memory_budget_mb: int) -> ResourceBudget:
    """Resolve the full resource picture from the single memory-budget knob."""
    memory_bytes, auto = effective_memory_bytes(memory_budget_mb)
    cpu_count = detect_cpu_limit()
    decode = resolve_decode_budget_pixels(memory_bytes)
    workers = resolve_worker_count(
        memory_bytes=memory_bytes,
        cpu_count=cpu_count,
        decode_budget_pixels=decode,
    )
    under = decode < MIN_HEALTHY_DECODE_PIXELS
    return ResourceBudget(
        memory_bytes=memory_bytes,
        memory_auto=auto,
        cpu_count=cpu_count,
        decode_budget_pixels=decode,
        worker_count=workers,
        under_provisioned=under,
    )
