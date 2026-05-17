"""Tests for _try_read_image_dims against real image files.

images/test/  — all valid; must return (w, h, None) with positive dimensions.
images/bad/   — all corrupt/truncated; must return (None, None, <error>).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hokku_server.image_manager_abstract import AbstractImageManager

from tests._helpers import is_oversize_fixture

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEST_DIR = _REPO_ROOT / "images" / "test"
_BAD_DIR  = _REPO_ROOT / "images" / "bad"

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".heic", ".heif", ".gif", ".avif", ".jxl"}


_test_images = sorted(
    p for p in _TEST_DIR.iterdir()
    if p.suffix.lower() in _IMAGE_EXTS and not is_oversize_fixture(p)
)
_bad_images = sorted(
    p for p in _BAD_DIR.iterdir() if p.suffix.lower() in _IMAGE_EXTS
)


@pytest.mark.parametrize("path", _test_images, ids=lambda p: p.name)
def test_valid_image_returns_dimensions(path: Path):
    w, h, err = AbstractImageManager._try_read_image_dims(path)
    assert err is None, f"Expected success for {path.name}, got error: {err}"
    assert w is not None and w > 0, f"Expected positive width for {path.name}, got {w}"
    assert h is not None and h > 0, f"Expected positive height for {path.name}, got {h}"


@pytest.mark.parametrize("path", _bad_images, ids=lambda p: p.name)
def test_bad_image_returns_error(path: Path):
    w, h, err = AbstractImageManager._try_read_image_dims(path)
    assert err is not None, f"Expected an error for {path.name}, but got dims {w}×{h}"
    assert w is None and h is None, f"Expected no dims for {path.name}, got {w}×{h}"
