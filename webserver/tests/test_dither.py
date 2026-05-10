"""Dither LUTs, noop kernel, cache_slug stability."""
import numpy as np

from hokku_server.dither import (
    PALETTE_LAB,
    _cached_euclidean_lut,
    _cached_hue_aware_lut,
    dither,
    noop_dither,
)
from hokku_server.dither_config import DitherConfig


def test_palette_lab_shape():
    assert PALETTE_LAB.shape == (6, 3)


def test_euclidean_lut_cube():
    lut, scale = _cached_euclidean_lut()
    assert lut.shape == (32, 32, 32)
    assert lut.dtype == np.uint8
    assert lut.max() <= 5
    assert scale == 8.0  # 256/32


def test_hue_aware_lut_cube():
    lut, scale = _cached_hue_aware_lut(95.0, 8.0)
    assert lut.shape == (32, 32, 32)
    assert lut.max() <= 5


def test_noop_kernel_runs():
    cfg = DitherConfig(
        algorithm="noop",
        lut_name="euclidean",
        serpentine=False,
        hue_cutoff_deg=95.0,
        neutral_chroma=8.0,
    )
    canvas = np.full((20, 20, 3), 128, dtype=np.uint8)
    out = dither(canvas, cfg)
    assert out.shape == (20, 20)
    assert out.dtype == np.uint8
    assert out.max() <= 5


def test_cache_slug_stable_and_distinct():
    a = DitherConfig("atkinson", "euclidean", False, 95.0, 8.0)
    a2 = DitherConfig("atkinson", "euclidean", False, 95.0, 8.0)
    b = DitherConfig("atkinson", "hue_aware", False, 95.0, 8.0)
    assert a.cache_slug() == a2.cache_slug()
    assert a.cache_slug() != b.cache_slug()
