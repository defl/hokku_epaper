"""Face detection contract tests — yunet_opencv only.

Tests are NOT marked time_intensive — face detection runs in tens to
hundreds of milliseconds per image.

Portraits (expect at least one face detected):
  - Actress_Anna_Unterberger-2.jpg
  - Robert_De_Niro_KVIFF_portrait.jpg
  - Wayuu_woman_with_sad_face_in_the_market_buying.jpg
  - violin_player_tuxedo.jpg  ← multi-face (5 faces in an orchestra)

Non-portraits (expect zero faces detected):
  - Albi_Panorama_Sunset_Panini_General.heif
  - Albrecht_Duerer_Hare_1502_Google_Art_Project.jxl
  - Fitz_Roy_1.avif
  - Forest_road_Slavne_2017_BW_G9.jpg
  - RGB_corner_gradient_bilinear_1200.png
  - grayscale_linear_bar_1200x300.png
  - tree.heic
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hokku_server.bounding_box import BoundingBox
from hokku_server.face_detect_yunet_opencv import OpenCVYuNetFaceDetector

_IMAGES = Path(__file__).resolve().parents[2] / "images" / "test"

_PORTRAITS = [
    "Actress_Anna_Unterberger-2.jpg",
    "Robert_De_Niro_KVIFF_portrait.jpg",
    "Wayuu_woman_with_sad_face_in_the_market_buying.jpg",
    "violin_player_tuxedo.jpg",
]

_NON_PORTRAITS = [
    "Albi_Panorama_Sunset_Panini_General.heif",
    "Albrecht_Duerer_Hare_1502_Google_Art_Project.jxl",
    "Fitz_Roy_1.avif",
    "Forest_road_Slavne_2017_BW_G9.jpg",
    "RGB_corner_gradient_bilinear_1200.png",
    "grayscale_linear_bar_1200x300.png",
    "tree.heic",
]


@pytest.fixture(scope="module")
def face_detector():
    return OpenCVYuNetFaceDetector()


@pytest.mark.parametrize("filename", _PORTRAITS)
def test_portrait_detected(face_detector, filename: str):
    path = _IMAGES / filename
    assert path.exists(), f"Test image missing from repo: {path}"
    assert face_detector.has_face(path) is True, (
        f"yunet_opencv should detect a face in {filename}"
    )


@pytest.mark.parametrize("filename", _NON_PORTRAITS)
def test_non_portrait_not_detected(face_detector, filename: str):
    path = _IMAGES / filename
    assert path.exists(), f"Test image missing from repo: {path}"
    assert face_detector.has_face(path) is False, (
        f"yunet_opencv false-positively detected a face in {filename}"
    )


def test_missing_file_returns_false(face_detector, tmp_path: Path):
    assert face_detector.has_face(tmp_path / "nonexistent.jpg") is False


# ── multi-face detection: violin player image ─────────────────────────────────

def test_violin_image_detects_multiple_faces(face_detector):
    """violin_player_tuxedo.jpg contains several musicians in the background.
    YuNet should return more than one face bbox, exercising the multi-face path."""
    path = _IMAGES / "violin_player_tuxedo.jpg"
    assert path.exists(), f"Test image missing: {path}"
    bboxes = face_detector.detect(path)
    assert len(bboxes) >= 2, (
        f"Expected >=2 faces in the violin player image; got {len(bboxes)}"
    )


def test_violin_image_bboxes_are_valid_bounding_boxes(face_detector):
    """Each returned BoundingBox must be normalised to [0, 1] with positive dims."""
    path = _IMAGES / "violin_player_tuxedo.jpg"
    bboxes = face_detector.detect(path)
    assert bboxes, "Expected at least one face in violin player image"
    for i, b in enumerate(bboxes):
        assert isinstance(b, BoundingBox), f"bbox[{i}] is not a BoundingBox: {b!r}"
        assert 0.0 <= b.x <= 1.0, f"bbox[{i}].x out of range: {b.x}"
        assert 0.0 <= b.y <= 1.0, f"bbox[{i}].y out of range: {b.y}"
        assert b.w > 0.0,         f"bbox[{i}].w must be positive: {b.w}"
        assert b.h > 0.0,         f"bbox[{i}].h must be positive: {b.h}"
        assert b.x + b.w <= 1.0,  f"bbox[{i}] extends beyond right edge: x+w={b.x+b.w}"
        assert b.y + b.h <= 1.0,  f"bbox[{i}] extends beyond bottom edge: y+h={b.y+b.h}"


def test_violin_image_bboxes_do_not_overlap_excessively(face_detector):
    """Sanity check: no two bboxes should be nearly identical (dedup worked)."""
    path = _IMAGES / "violin_player_tuxedo.jpg"
    bboxes = face_detector.detect(path)
    for i, a in enumerate(bboxes):
        for j, b in enumerate(bboxes):
            if i >= j:
                continue
            # Intersection-over-min: large value means near-duplicate
            x_overlap = max(0.0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x))
            y_overlap = max(0.0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
            intersection = x_overlap * y_overlap
            area_min = min(a.w * a.h, b.w * b.h)
            iom = intersection / area_min if area_min > 0 else 0.0
            assert iom < 0.5, (
                f"bbox[{i}] and bbox[{j}] overlap by {iom:.2f} (IoM) — "
                f"likely a duplicate detection: {a}, {b}"
            )

