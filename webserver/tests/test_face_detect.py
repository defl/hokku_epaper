"""Face detection contract tests.

Every detector exposes the same ``has_face(path) -> bool`` contract; this
suite exercises all three concretes against a curated pile of portraits
(must detect) and non-portraits (must not detect).

Portraits (expect True for every detector):
  - Actress_Anna_Unterberger-2.jpg
  - Robert_De_Niro_KVIFF_portrait.jpg
  - Wayuu_woman_with_sad_face_in_the_market_buying.jpg

Non-portraits (expect False for every detector unless explicitly allow-listed):
  - Albi_Panorama_Sunset_Panini_General.jpg
  - Albrecht_Duerer_Hare_1502_Google_Art_Project.jpg
  - Fitz_Roy_1.jpg
  - Forest_road_Slavne_2017_BW_G9.jpg
  - RGB_corner_gradient_bilinear_1200.png
  - grayscale_linear_bar_1200x300.png
  - tree.heic

Tests are NOT marked time_intensive — face detection runs in tens to
hundreds of milliseconds per image, so the suite stays fast.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_IMAGES = Path(__file__).resolve().parents[2] / "images" / "test"

_PORTRAITS = [
    "Actress_Anna_Unterberger-2.jpg",
    "Robert_De_Niro_KVIFF_portrait.jpg",
    "Wayuu_woman_with_sad_face_in_the_market_buying.jpg",
]

_NON_PORTRAITS = [
    "Albi_Panorama_Sunset_Panini_General.jpg",
    "Albrecht_Duerer_Hare_1502_Google_Art_Project.jpg",
    "Fitz_Roy_1.jpg",
    "Forest_road_Slavne_2017_BW_G9.jpg",
    "RGB_corner_gradient_bilinear_1200.png",
    "grayscale_linear_bar_1200x300.png",
    "tree.heic",
]


def _build_detector(name: str):
    """Build a detector by name, skipping if its dep isn't installed."""
    if name == "yunet_opencv":
        from webserver.face_detect_yunet_opencv import OpenCVYuNetFaceDetector
        return OpenCVYuNetFaceDetector()
    if name == "haar_opencv":
        from webserver.face_detect_haar_opencv import OpenCVHaarFaceDetector
        return OpenCVHaarFaceDetector()
    if name == "yunet_onnx":
        pytest.importorskip("onnxruntime")
        from webserver.face_detect_yunet_onnx import ONNXYuNetFaceDetector
        return ONNXYuNetFaceDetector()
    raise ValueError(f"unknown detector: {name}")


@pytest.fixture(params=["yunet_opencv", "haar_opencv", "yunet_onnx"])
def face_detector(request):
    return _build_detector(request.param)


@pytest.mark.parametrize("filename", _PORTRAITS)
def test_portrait_detected(face_detector, filename: str):
    path = _IMAGES / filename
    assert path.exists(), f"Test image missing: {path}"
    assert face_detector.has_face(path) is True, (
        f"{type(face_detector).__name__} should detect a face in {filename}"
    )


@pytest.mark.parametrize("filename", _NON_PORTRAITS)
def test_non_portrait_not_detected(face_detector, filename: str):
    path = _IMAGES / filename
    assert path.exists(), f"Test image missing: {path}"
    assert face_detector.has_face(path) is False, (
        f"{type(face_detector).__name__} false-positively detected a face in {filename}"
    )


def test_missing_file_returns_false(face_detector, tmp_path: Path):
    assert face_detector.has_face(tmp_path / "nonexistent.jpg") is False
