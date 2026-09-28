"""Tests for _try_read_image_dims against real image files.

images/test/  — all valid; must return (w, h, None) with positive dimensions.
images/bad/   — all corrupt/truncated; must return (None, None, <error>).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import ExifTags, Image

from hokku.webserver.image_manager_abstract import AbstractImageManager
from tests._helpers import is_oversize_fixture

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEST_DIR = _REPO_ROOT / "images" / "test"
_BAD_DIR = _REPO_ROOT / "images" / "bad"

_IMAGE_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".webp",
    ".tiff",
    ".heic",
    ".heif",
    ".gif",
    ".avif",
    ".jxl",
    ".svg",
}


_test_images = sorted(
    p for p in _TEST_DIR.iterdir() if p.suffix.lower() in _IMAGE_EXTS and not is_oversize_fixture(p)
)
_bad_images = sorted(p for p in _BAD_DIR.iterdir() if p.suffix.lower() in _IMAGE_EXTS)


@pytest.mark.parametrize("path", _test_images, ids=lambda p: p.name)
def test_valid_image_returns_dimensions(path: Path):
    w, h, err = AbstractImageManager._try_read_image_dims(path)
    assert err is None, f"Expected success for {path.name}, got error: {err}"
    assert w is not None and w > 0, f"Expected positive width for {path.name}, got {w}"
    assert h is not None and h > 0, f"Expected positive height for {path.name}, got {h}"


@pytest.mark.parametrize(
    ("exif_orientation", "expected"),
    [
        (None, (400, 200)),
        (1, (400, 200)),
        (2, (400, 200)),
        (3, (400, 200)),
        (4, (400, 200)),
        (5, (200, 400)),
        (6, (200, 400)),
        (7, (200, 400)),
        (8, (200, 400)),
    ],
)
def test_dims_honour_exif_orientation(tmp_path: Path, exif_orientation, expected):
    """Issue #40: a phone portrait is stored landscape + an EXIF rotate tag.

    The recorded dims must be the displayed ones, or native_orientation calls
    a portrait photo landscape.
    """
    path = tmp_path / "phone.jpg"
    exif = Image.Exif()
    if exif_orientation is not None:
        exif[ExifTags.Base.Orientation] = exif_orientation
    Image.new("RGB", (400, 200)).save(path, exif=exif)

    w, h, err = AbstractImageManager._try_read_image_dims(path)
    assert err is None
    assert (w, h) == expected


def test_png_exif_orientation_in_header_is_honoured(tmp_path: Path):
    """PNG carries EXIF in an eXIf chunk; one ahead of IDAT is read header-only."""
    path = tmp_path / "rotated.png"
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = 6
    Image.new("RGB", (400, 200)).save(path, exif=exif)

    w, h, err = AbstractImageManager._try_read_image_dims(path)
    assert err is None
    assert (w, h) == (200, 400)


@pytest.mark.parametrize("path", _bad_images, ids=lambda p: p.name)
def test_bad_image_returns_error(path: Path):
    w, h, err = AbstractImageManager._try_read_image_dims(path)
    assert err is not None, f"Expected an error for {path.name}, but got dims {w}×{h}"
    assert w is None and h is None, f"Expected no dims for {path.name}, got {w}×{h}"
