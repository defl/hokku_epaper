"""Face detection tests using YuNet on real images from images/test/.

These tests are NOT marked time_intensive — YuNet runs in tens of milliseconds
per image, so the suite stays fast.

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

import pytest

from webserver.face_detect import has_face

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


@pytest.mark.parametrize("filename", _PORTRAITS)
def test_portrait_detected(filename: str):
    path = _IMAGES / filename
    assert path.exists(), f"Test image missing: {path}"
    assert has_face(path) is True, f"Expected face detected in {filename}"


@pytest.mark.parametrize("filename", _NON_PORTRAITS)
def test_non_portrait_not_detected(filename: str):
    path = _IMAGES / filename
    assert path.exists(), f"Test image missing: {path}"
    assert has_face(path) is False, f"Expected no face in {filename}"


def test_missing_file_returns_false(tmp_path: Path):
    assert has_face(tmp_path / "nonexistent.jpg") is False
