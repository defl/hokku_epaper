"""The autocontrast switch must change only what it claims, and always restore.

Per-channel autocontrast is a colour cast generator: a photograph whose blue
channel spans a narrower range than red and green gets blue stretched on its own.
`preserve_tone` must keep the channels' balance; the switch is a swap of a PIL
module attribute, so it must be undone even on error.
"""

import os
import sys

import numpy as np
import pytest
from PIL import Image, ImageOps

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import hokku.webserver.image_abc as image_abc
from prepare_variants import autocontrast_as


def _warm_photo() -> Image.Image:
    """Full-range red and green, blue confined to 60-140: a warm cast source."""
    y, x = np.mgrid[0:64, 0:64]
    r = 255 * x / 63
    g = 255 * y / 63
    b = 60 + 80 * (x + y) / 126
    return Image.fromarray(np.stack([r, g, b], -1).astype(np.uint8))


def _blue_share(img: Image.Image) -> float:
    a = np.asarray(img, dtype=np.float64)
    return float(a[..., 2].sum() / a.sum())


def test_per_channel_stretch_shifts_the_colour_balance():
    img = _warm_photo()
    assert abs(_blue_share(ImageOps.autocontrast(img)) - _blue_share(img)) > 0.02


def test_preserve_tone_keeps_the_balance():
    img = _warm_photo()
    with autocontrast_as("preserve_tone"):
        out = image_abc.ImageOps.autocontrast(img, cutoff=0.5)
    assert abs(_blue_share(out) - _blue_share(img)) < 0.01


def test_off_is_identity():
    img = _warm_photo()
    with autocontrast_as("off"):
        out = image_abc.ImageOps.autocontrast(img, cutoff=0.5)
    np.testing.assert_array_equal(np.asarray(out), np.asarray(img))


@pytest.mark.parametrize("mode", [None, "per_channel"])
def test_production_modes_leave_pil_alone(mode):
    real = image_abc.ImageOps.autocontrast
    with autocontrast_as(mode):
        assert image_abc.ImageOps.autocontrast is real


def test_switch_is_always_undone():
    real = image_abc.ImageOps.autocontrast
    with pytest.raises(RuntimeError), autocontrast_as("off"):
        raise RuntimeError("boom")
    assert image_abc.ImageOps.autocontrast is real
