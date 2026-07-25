"""Memory measurement helpers for the dither-pipeline budget tests.

Three layers of measurement:

* ``peak_python_heap(fn, *args, **kw)`` — uses ``tracemalloc``. Captures
  Python-side allocations only (numpy buffers count, libjpeg / libpng
  C buffers do NOT).  Deterministic. Useful for asserting that a single
  pipeline function doesn't internally allocate a giant buffer, but
  understates real RSS, so should not be used for end-to-end claims.

* ``peak_rss_sampled(fn, *args, sample_ms=5)`` — runs *fn* in the
  current process; a background thread polls ``psutil.memory_info().rss``
  and returns ``(peak_delta_bytes, peak_absolute_bytes)``.  Catches
  everything (numpy, PIL C, decoder libs).  Has sampling jitter; allocations
  shorter than the sample interval can be missed.

* ``peak_rss_subprocess(image_path, render_kwargs)`` — spawns a fresh
  Python subprocess that does *only* the render call, then exits.
  Parent polls the child's RSS via ``psutil`` until exit, returns the
  peak.  Eliminates pytest / interpreter / cached-LUT contamination.
  This is the headline measurement; expect ~1–2 s overhead per call.
"""

from __future__ import annotations

import os
import pickle
import subprocess
import sys
import threading
import time
import tracemalloc
from pathlib import Path
from typing import Any, Callable

import psutil


def peak_python_heap(fn: Callable, *args: Any, **kwargs: Any) -> int:
    """Run *fn* under ``tracemalloc`` and return peak Python-heap bytes."""
    tracemalloc.start()
    try:
        fn(*args, **kwargs)
        _, peak = tracemalloc.get_traced_memory()
        return int(peak)
    finally:
        tracemalloc.stop()


def peak_rss_sampled(
    fn: Callable,
    *args: Any,
    sample_ms: float = 5.0,
    **kwargs: Any,
) -> tuple[int, int]:
    """Run *fn* in this process; sample RSS at sample_ms intervals.

    Returns (peak_delta_bytes, peak_absolute_bytes).  ``peak_delta_bytes``
    is peak − baseline measured immediately before *fn* starts.
    """
    p = psutil.Process()
    baseline = int(p.memory_info().rss)
    peak = [baseline]
    stop = threading.Event()

    def watch() -> None:
        # Tight loop: as fast as the OS lets us. sample_ms is a sleep cap.
        interval = max(0.001, sample_ms / 1000.0)
        while not stop.is_set():
            try:
                rss = int(p.memory_info().rss)
            except psutil.NoSuchProcess:
                return
            if rss > peak[0]:
                peak[0] = rss
            time.sleep(interval)

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    try:
        fn(*args, **kwargs)
    finally:
        stop.set()
        t.join(timeout=1.0)
    return peak[0] - baseline, peak[0]


# Child-process drivers. Each reads a pickled payload from stdin, does ONE unit
# of work (a full render, or just the decode step), and exits. We only need the
# side effect — peak RSS while the work runs — so the printed result is
# discarded. Both share the same handshake with the parent (see
# ``_spawn_and_sample_rss``): emit ``READY`` after imports, block on one stdin
# byte, then do the work. That lets the parent snapshot the baseline RSS *after*
# imports but *before* any work-driven allocation.
#
# Preamble shared by every driver: imports + the READY/wait handshake, so the
# individual drivers below only add their one unit of work. Keeping it in one
# place means the handshake protocol can't drift between drivers.
_CHILD_PREAMBLE = r"""
import os, sys, pickle
from pathlib import Path
sys.stdin = sys.stdin.buffer if hasattr(sys.stdin, 'buffer') else sys.stdin
payload = pickle.load(sys.stdin)
image_path = Path(payload['image_path'])  # open_image_for_render wants a Path
render_kwargs = payload.get('render_kwargs', {})
from pillow_heif import register_heif_opener
register_heif_opener()
from hokku.webserver.image_renderer import ImageRenderer, open_image_for_render

def _peak_rss_bytes():
    # True high-water mark of resident memory — no sampling race, so it catches
    # a sub-millisecond decode spike that RSS polling would miss. Windows:
    # peak_wset; Linux: ru_maxrss (KB); macOS: ru_maxrss (bytes).
    try:
        import psutil
        pk = getattr(psutil.Process().memory_info(), 'peak_wset', None)
        if pk:
            return int(pk)
    except Exception:
        pass
    try:
        import resource
        ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(ru) * (1024 if sys.platform.startswith('linux') else 1)
    except Exception:
        return 0

def _ready_then_wait():
    # Report the post-import high-water mark so the parent can subtract it — the
    # delta is then the work's own peak, excluding numba/cv2 import cost.
    sys.stdout.write('READY %d\n' % _peak_rss_bytes()); sys.stdout.flush()
    sys.stdin.read(1)
"""

# Full-render driver: decode + render_panel_bytes (the end-to-end pipeline peak).
_CHILD_DRIVER = (
    _CHILD_PREAMBLE
    + r"""
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_streaming_numba import NumbaStreamingDither
_HUESSEN = DISPLAY_REGISTRY["huessen_epf1301"]
def render_panel_bytes(img, cfg, orientation, crop_to_fill_threshold=0.0):
    return ImageRenderer(NumbaStreamingDither(_HUESSEN), _HUESSEN).render_panel_bytes(img, cfg, orientation, crop_to_fill_threshold)
cfg = render_kwargs['cfg']
orientation = render_kwargs.get('orientation', 'landscape')
crop_to_fill = float(render_kwargs.get('crop_to_fill_threshold', 0.0))
_ready_then_wait()
img = open_image_for_render(image_path)
out = render_panel_bytes(img, cfg, orientation, crop_to_fill)
sys.stdout.write('OK %d %d\n' % (_peak_rss_bytes(), len(out)))
sys.stdout.flush()
"""
)

# Decode-only driver: just open_image_for_render (the source-decode peak in
# isolation — this is where a big JPEG / HEIF panorama used to spike the RSS).
_DECODE_CHILD_DRIVER = (
    _CHILD_PREAMBLE
    + r"""
_ready_then_wait()
img = open_image_for_render(image_path)
sys.stdout.write('OK %d %d %d\n' % (_peak_rss_bytes(), img.size[0], img.size[1]))
sys.stdout.flush()
"""
)


def _spawn_and_sample_rss(
    driver: str,
    payload: bytes,
    *,
    sample_ms: float,
    timeout_s: float,
) -> tuple[int, int, int]:
    """Spawn *driver* in a fresh interpreter, do the READY handshake, and sample
    the child's RSS from just-before the work starts until it exits.

    Returns ``(delta_sampled, delta_hwm, baseline)`` in bytes, all relative to
    the child's post-import RSS: ``delta_sampled`` is the parent-polled peak
    (misses sub-sample spikes), ``delta_hwm`` is the child's true peak
    high-water mark (never misses). Callers pick. Shared by
    :func:`peak_rss_subprocess` and :func:`peak_rss_decode_subprocess` so the
    handshake/sampling logic lives once.
    """
    # Inherit the current python; the test runner already configured the venv.
    proc = subprocess.Popen(
        [sys.executable, "-c", driver],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ},
        cwd=str(Path(__file__).resolve().parent.parent),  # python/ (so `hokku` is importable)
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    try:
        # Hand the payload to the child.
        proc.stdin.write(payload)
        proc.stdin.flush()
        # Wait for READY from the child (imports + payload unpickled). The child
        # appends its post-import RSS high-water mark so we can subtract import
        # cost from the work's peak.
        ready_line = proc.stdout.readline()
        if not ready_line.startswith(b"READY"):
            err = proc.stderr.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"child failed during import: {err}")
        try:
            baseline_hwm = int(ready_line.split()[1])
        except (IndexError, ValueError):
            baseline_hwm = 0
        # NOW snapshot baseline RSS — after imports, before the work.
        try:
            child_ps = psutil.Process(proc.pid)
            baseline = int(child_ps.memory_info().rss)
            peak = baseline
        except psutil.NoSuchProcess:
            raise RuntimeError("child died before work started") from None
        # Tell the child to proceed.
        proc.stdin.write(b"\n")
        proc.stdin.flush()
        # Sample until the child exits.
        deadline = time.monotonic() + timeout_s
        interval = max(0.001, sample_ms / 1000.0)
        while True:
            if proc.poll() is not None:
                break
            if time.monotonic() > deadline:
                proc.kill()
                raise TimeoutError(f"work subprocess exceeded {timeout_s}s")
            try:
                rss = int(child_ps.memory_info().rss)
                if rss > peak:
                    peak = rss
            except psutil.NoSuchProcess:
                break
            time.sleep(interval)
        # Drain stdout/stderr and ensure the child has been reaped before we read
        # returncode (psutil.NoSuchProcess can fire slightly before Popen has
        # collected the exit status).
        out_tail = proc.stdout.read().decode("utf-8", errors="replace")
        err_tail = proc.stderr.read().decode("utf-8", errors="replace")
        proc.wait(timeout=5)
        if proc.returncode != 0:
            raise RuntimeError(
                f"child work failed (rc={proc.returncode}):\n"
                f"stdout={out_tail!r}\nstderr={err_tail!r}"
            )
        # Two measures, returned so each caller picks what suits it:
        #  * delta_sampled — parent-polled RSS delta; good for a long-running
        #    render where sampling reliably catches the peak (peak_rss_subprocess).
        #  * delta_hwm — the child's true peak high-water mark; the only thing
        #    that catches a sub-millisecond decode spike (peak_rss_decode_*).
        final_hwm = 0
        for tok in out_tail.split():
            if tok.startswith("OK"):
                continue
            try:
                final_hwm = int(tok)
            except ValueError:
                continue
            break
        delta_hwm = final_hwm - baseline_hwm if final_hwm and baseline_hwm else 0
        delta_sampled = peak - baseline
        return delta_sampled, delta_hwm, baseline
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def peak_rss_subprocess(
    image_path: Path | str,
    *,
    cfg: Any,
    orientation: str = "landscape",
    crop_to_fill_threshold: float = 0.0,
    sample_ms: float = 5.0,
    timeout_s: float = 120.0,
) -> tuple[int, int]:
    """Render *image_path* in a fresh subprocess; return (peak, baseline) RSS bytes.

    ``baseline`` is the child's RSS just after import + before the render.
    ``peak`` is the maximum RSS observed during the render.

    The child uses ``ImageRenderer.render_panel_bytes`` with the supplied
    ImageConfig.  The difference (peak − baseline) is what the pipeline alone
    consumed; that's the number that needs to fit in 50 MB.
    """
    payload = pickle.dumps(
        {
            "image_path": str(image_path),
            "render_kwargs": {
                "cfg": cfg,
                "orientation": orientation,
                "crop_to_fill_threshold": crop_to_fill_threshold,
            },
        }
    )
    # Use the larger of parent-sampled and the child's HWM. Sampling alone can
    # miss a peak that lives between polls (it was reporting ~0 MB for fast
    # renders); the HWM never misses it. Callers compare against a Linux-
    # calibrated budget, so they gate the assertion to Linux (peak_wset on
    # Windows reads higher than the Pi's ru_maxrss).
    delta_sampled, delta_hwm, baseline = _spawn_and_sample_rss(
        _CHILD_DRIVER, payload, sample_ms=sample_ms, timeout_s=timeout_s
    )
    return max(delta_sampled, delta_hwm), baseline


def peak_rss_decode_subprocess(
    image_path: Path | str,
    *,
    sample_ms: float = 2.0,
    timeout_s: float = 60.0,
) -> tuple[int, int]:
    """Run ONLY ``open_image_for_render(image_path)`` in a fresh subprocess.

    Returns ``(peak − baseline, baseline)`` RSS bytes, where the delta is the
    peak RAM the *decode step alone* consumed. This isolates the source-decode
    spike — a big JPEG or a HEIF panorama used to materialise several full-frame
    buffers here (decode + EXIF-transpose copy + convert copy) and tip the Pi
    into the OOM killer. The default sample interval is tight (2 ms) because a
    decode-and-shrink is short-lived; a coarse sampler could miss the spike.
    """
    payload = pickle.dumps({"image_path": str(image_path)})
    # A decode-and-shrink is short-lived; the peak_wset/ru_maxrss HWM is the only
    # reliable catch. Fall back to the sampled delta if the child couldn't report
    # a HWM (e.g. a stripped-down platform).
    delta_sampled, delta_hwm, baseline = _spawn_and_sample_rss(
        _DECODE_CHILD_DRIVER, payload, sample_ms=sample_ms, timeout_s=timeout_s
    )
    return max(delta_sampled, delta_hwm), baseline
