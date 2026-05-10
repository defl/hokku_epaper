"""Layer-C peak-RSS measurements for the three face detectors.

Each test spawns a fresh subprocess that imports the detector, instantiates
it (model loaded), and runs ``has_face`` exactly once on a real test image.
The child reports its own RSS at three checkpoints (python-only, after
init, after has_face); the parent additionally polls externally.

Marked ``time_intensive`` because each measurement is a fresh interpreter
spawn (~1–2 s each).

Run with:
    pytest webserver/tests/test_face_detect_memory.py -m time_intensive -s

Numbers feed ``docs/face_detection_memory_usage.md``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests._face_detect_memory_helpers import peak_rss_subprocess_face_detect

_IMAGES = Path(__file__).resolve().parents[2] / "images" / "test"

# A representative portrait that all three detectors agree contains a face.
_PORTRAIT = _IMAGES / "Robert_De_Niro_KVIFF_portrait.jpg"

# Loose ceilings on observed peak total RSS — these are absolute numbers,
# not deltas, because what matters on a 512 MB Pi is "does this fit alongside
# Flask + the dither pipeline". Tighten if measurements move materially.
_PEAK_BUDGETS_MB = {
    "yunet_opencv": 200,
    "haar_opencv":  220,   # detectMultiScale's transient is bigger than expected
    "yunet_onnx":   200,
}


def _mb(b: int) -> float:
    return b / (1024 * 1024)


@pytest.mark.time_intensive
@pytest.mark.parametrize("detector_name", ["yunet_opencv", "haar_opencv", "yunet_onnx"])
def test_detector_peak_rss(detector_name: str):
    if detector_name == "yunet_onnx":
        pytest.importorskip("onnxruntime")
    r = peak_rss_subprocess_face_detect(detector_name, _PORTRAIT)
    print(
        f"\n  {detector_name:14s} on {_PORTRAIT.name}:"
        f"\n    rss_python_only  = {_mb(r['rss_python_only']):6.1f} MB"
        f"\n    rss_after_init   = {_mb(r['rss_after_init']):6.1f} MB"
        f"\n    rss_after_face   = {_mb(r['rss_after_has_face']):6.1f} MB"
        f"\n    peak_observed    = {_mb(r['peak_observed']):6.1f} MB"
        f"\n    detected         = {r['detected']}"
    )
    assert r["detected"], f"{detector_name} should detect the portrait"
    peak_mb = _mb(r["peak_observed"])
    assert peak_mb < _PEAK_BUDGETS_MB[detector_name], (
        f"{detector_name} peak {peak_mb:.1f} MB exceeds "
        f"budget {_PEAK_BUDGETS_MB[detector_name]} MB"
    )
