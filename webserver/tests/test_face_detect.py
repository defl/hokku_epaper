"""Face detection contract tests — yunet_opencv only.

Tests are NOT marked time_intensive — face detection runs in tens to
hundreds of milliseconds per image.

Portraits (expect True):
  - Actress_Anna_Unterberger-2.jpg
  - Robert_De_Niro_KVIFF_portrait.jpg
  - Wayuu_woman_with_sad_face_in_the_market_buying.jpg

Non-portraits (expect False):
  - Albi_Panorama_Sunset_Panini_General.jpg
  - Albrecht_Duerer_Hare_1502_Google_Art_Project.jpg
  - Fitz_Roy_1.jpg
  - Forest_road_Slavne_2017_BW_G9.jpg
  - RGB_corner_gradient_bilinear_1200.png
  - grayscale_linear_bar_1200x300.png
  - tree.heic
"""
from __future__ import annotations

from pathlib import Path

import cv2  # hard dependency — must be installed (opencv-python-headless>=4.7)
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


@pytest.fixture(scope="module")
def face_detector():
    from hokku_server.face_detect_yunet_opencv import OpenCVYuNetFaceDetector
    return OpenCVYuNetFaceDetector()


@pytest.mark.parametrize("filename", _PORTRAITS)
def test_portrait_detected(face_detector, filename: str):
    path = _IMAGES / filename
    if not path.exists():
        pytest.skip(f"Test image missing: {path}")
    assert face_detector.has_face(path) is True, (
        f"yunet_opencv should detect a face in {filename}"
    )


@pytest.mark.parametrize("filename", _NON_PORTRAITS)
def test_non_portrait_not_detected(face_detector, filename: str):
    path = _IMAGES / filename
    if not path.exists():
        pytest.skip(f"Test image missing: {path}")
    assert face_detector.has_face(path) is False, (
        f"yunet_opencv false-positively detected a face in {filename}"
    )


def test_missing_file_returns_false(face_detector, tmp_path: Path):
    assert face_detector.has_face(tmp_path / "nonexistent.jpg") is False
