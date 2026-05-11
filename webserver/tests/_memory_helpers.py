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
    fn: Callable, *args: Any, sample_ms: float = 5.0, **kwargs: Any,
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


# Child-process driver used by peak_rss_subprocess.  The child reads a
# pickled (image_path, render_kwargs) tuple from stdin, performs ONE
# render, and prints the resulting panel-byte length to stdout.  We only
# need the side effect (peak memory while rendering) — the result itself
# is discarded by the parent.
_CHILD_DRIVER = r"""
import os, sys, pickle
sys.stdin = sys.stdin.buffer if hasattr(sys.stdin, 'buffer') else sys.stdin
payload = pickle.load(sys.stdin)
image_path = payload['image_path']
render_kwargs = payload.get('render_kwargs', {})
from pillow_heif import register_heif_opener
register_heif_opener()
from hokku_server.dither_streaming import StreamingDither
from hokku_server.image_renderer import ImageRenderer, open_image_for_render

def render_panel_bytes(img, cfg, orientation, crop_to_fill_threshold=0.0):
    return ImageRenderer(StreamingDither()).render_panel_bytes(img, cfg, orientation, crop_to_fill_threshold)
from hokku_server.image_config import ImageConfig
from hokku_server.dither_config import DitherConfig
cfg = render_kwargs['cfg']
orientation = render_kwargs.get('orientation', 'landscape')
crop_to_fill = float(render_kwargs.get('crop_to_fill_threshold', 0.0))
# Block until parent says go (lets parent attach RSS sampler before allocations start).
sys.stdout.write('READY\n')
sys.stdout.flush()
sys.stdin.read(1)
img = open_image_for_render(image_path)
out = render_panel_bytes(img, cfg, orientation, crop_to_fill)
sys.stdout.write('OK %d\n' % len(out))
sys.stdout.flush()
"""


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

    The child uses ``hokku_server.image.render_panel_bytes`` with the supplied
    ImageConfig.  The RHS of the difference (peak − baseline) is what the
    pipeline alone consumed; that's the number that needs to fit in 50 MB.
    """
    payload = pickle.dumps({
        "image_path": str(image_path),
        "render_kwargs": {
            "cfg": cfg,
            "orientation": orientation,
            "crop_to_fill_threshold": crop_to_fill_threshold,
        },
    })
    # Inherit the current python; the test runner already configured the venv.
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD_DRIVER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ},
        cwd=str(Path(__file__).resolve().parent.parent),  # webserver/
    )
    assert proc.stdin is not None and proc.stdout is not None
    try:
        # Hand the payload to the child.
        proc.stdin.write(payload)
        proc.stdin.flush()
        # Wait for READY from the child (imports + cfg unpickled).
        ready_line = proc.stdout.readline()
        if not ready_line.startswith(b"READY"):
            err = proc.stderr.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"child failed during import: {err}")
        # NOW snapshot baseline RSS — after imports, before the render.
        try:
            child_ps = psutil.Process(proc.pid)
            baseline = int(child_ps.memory_info().rss)
            peak = baseline
        except psutil.NoSuchProcess:
            raise RuntimeError("child died before render started")
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
                raise TimeoutError(f"render subprocess exceeded {timeout_s}s")
            try:
                rss = int(child_ps.memory_info().rss)
                if rss > peak:
                    peak = rss
            except psutil.NoSuchProcess:
                break
            time.sleep(interval)
        # Drain stdout/stderr and ensure the child has been reaped before
        # we read returncode (psutil.NoSuchProcess can fire slightly before
        # subprocess.Popen has collected the exit status).
        out_tail = proc.stdout.read().decode("utf-8", errors="replace")
        err_tail = proc.stderr.read().decode("utf-8", errors="replace")
        proc.wait(timeout=5)
        if proc.returncode != 0:
            raise RuntimeError(
                f"child render failed (rc={proc.returncode}):\n"
                f"stdout={out_tail!r}\nstderr={err_tail!r}"
            )
        return peak - baseline, baseline
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
