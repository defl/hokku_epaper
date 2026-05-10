"""Layer-C subprocess RSS helper for face-detector measurements.

Mirrors ``_memory_helpers.peak_rss_subprocess`` but the child runs a
detector instantiation + ``has_face`` call instead of a panel render.
"""
from __future__ import annotations

import os
import pickle
import subprocess
import sys
import time
from pathlib import Path

import psutil

# The child receives a pickled (detector_name, image_path) tuple via stdin,
# imports webserver, instantiates the requested detector, signals READY,
# then runs has_face once on the image after the parent green-lights it.
_CHILD_DRIVER = r"""
import sys, pickle, psutil
sys.stdin = sys.stdin.buffer if hasattr(sys.stdin, 'buffer') else sys.stdin
payload = pickle.load(sys.stdin)
detector_name = payload['detector_name']
image_path = payload['image_path']
me = psutil.Process()
rss_python_only = int(me.memory_info().rss)
if detector_name == 'yunet_opencv':
    from hokku_server.face_detect_yunet_opencv import OpenCVYuNetFaceDetector
    detector = OpenCVYuNetFaceDetector()
elif detector_name == 'haar_opencv':
    from hokku_server.face_detect_haar_opencv import OpenCVHaarFaceDetector
    detector = OpenCVHaarFaceDetector()
elif detector_name == 'yunet_onnx':
    from hokku_server.face_detect_yunet_onnx import ONNXYuNetFaceDetector
    detector = ONNXYuNetFaceDetector()
else:
    raise SystemExit(f'unknown detector: {detector_name}')
rss_after_init = int(me.memory_info().rss)
sys.stdout.write('READY %d %d\n' % (rss_python_only, rss_after_init))
sys.stdout.flush()
sys.stdin.read(1)
result = detector.has_face(image_path)
rss_final = int(me.memory_info().rss)
sys.stdout.write('OK %d %d\n' % (int(bool(result)), rss_final))
sys.stdout.flush()
"""


def peak_rss_subprocess_face_detect(
    detector_name: str,
    image_path: Path | str,
    *,
    sample_ms: float = 5.0,
    timeout_s: float = 60.0,
) -> dict:
    """Run ``has_face`` on *image_path* under *detector_name* in a fresh
    subprocess. Returns a dict with these keys (all bytes except
    ``detected``):

      - ``rss_python_only``: child's RSS before any detector import.
      - ``rss_after_init``: RSS after the detector module was imported
        and the detector was instantiated (model loaded).
      - ``rss_after_has_face``: RSS reported by the child immediately
        after has_face returned.
      - ``peak_observed``: max RSS the parent observed by polling at
        ``sample_ms`` intervals between READY and exit.
      - ``detected``: bool result of has_face.

    All four RSS numbers are absolute. The most useful headline numbers
    are ``rss_after_init`` (cost of having this detector loaded — what
    a Pi pays even when idle) and ``peak_observed`` (worst case during
    inference — what a Pi pays during a sync batch).
    """
    payload = pickle.dumps({
        "detector_name": detector_name,
        "image_path": str(image_path),
    })
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
        proc.stdin.write(payload)
        proc.stdin.flush()
        ready_line = proc.stdout.readline().decode("utf-8", errors="replace").strip()
        if not ready_line.startswith("READY"):
            err = proc.stderr.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"child failed during import/instantiation: {err}")
        # Parse "READY <rss_python_only> <rss_after_init>".
        parts = ready_line.split()
        rss_python_only = int(parts[1])
        rss_after_init = int(parts[2])
        try:
            child_ps = psutil.Process(proc.pid)
            peak = max(rss_after_init, int(child_ps.memory_info().rss))
        except psutil.NoSuchProcess:
            peak = rss_after_init
        proc.stdin.write(b"\n")
        proc.stdin.flush()
        deadline = time.monotonic() + timeout_s
        interval = max(0.001, sample_ms / 1000.0)
        while True:
            if proc.poll() is not None:
                break
            if time.monotonic() > deadline:
                proc.kill()
                raise TimeoutError(f"has_face subprocess exceeded {timeout_s}s")
            try:
                rss = int(child_ps.memory_info().rss)
                if rss > peak:
                    peak = rss
            except psutil.NoSuchProcess:
                break
            time.sleep(interval)
        out_tail = proc.stdout.read().decode("utf-8", errors="replace")
        err_tail = proc.stderr.read().decode("utf-8", errors="replace")
        proc.wait(timeout=5)
        if proc.returncode != 0:
            raise RuntimeError(
                f"child has_face failed (rc={proc.returncode}):\n"
                f"stdout={out_tail!r}\nstderr={err_tail!r}"
            )
        # Parse "OK <0|1> <rss_after_has_face>".
        detected = False
        rss_after_has_face = peak
        for line in out_tail.splitlines():
            if line.startswith("OK "):
                line_parts = line.split()
                detected = bool(int(line_parts[1]))
                if len(line_parts) >= 3:
                    rss_after_has_face = int(line_parts[2])
                break
        return {
            "rss_python_only": rss_python_only,
            "rss_after_init": rss_after_init,
            "rss_after_has_face": rss_after_has_face,
            "peak_observed": max(peak, rss_after_has_face),
            "detected": detected,
        }
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
