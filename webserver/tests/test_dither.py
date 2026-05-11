"""Dither LUTs, noop kernel, cache_slug stability, and concrete-class smoke tests."""
import importlib

import numpy as np
import pytest

from hokku_server.dither_streaming import (
    PALETTE_LAB,
    _cached_euclidean_lut,
    _cached_hue_aware_lut,
    dither,
    noop_dither,
)
from hokku_server.dither_streaming import StreamingDither
from hokku_server.dither_unconstrained import UnconstrainedDither
from hokku_server.dither_config import DitherConfig

_NUMBA_AVAILABLE = importlib.util.find_spec("numba") is not None


# ── LUT and palette ───────────────────────────────────────────────────────────

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


# ── Concrete class parametrization ────────────────────────────────────────────

def _concrete_classes():
    params = [
        pytest.param(StreamingDither, id="streaming"),
        pytest.param(UnconstrainedDither, id="unconstrained"),
    ]
    if _NUMBA_AVAILABLE:
        from hokku_server.dither_streaming_numba import NumbaDither
        params.append(pytest.param(NumbaDither, id="numba"))
    else:
        params.append(pytest.param(
            None, id="numba",
            marks=pytest.mark.skip(reason="numba not installed"),
        ))
    return params


def _fs_cfg() -> DitherConfig:
    return DitherConfig(
        algorithm="floyd_steinberg",
        lut_name="euclidean",
        serpentine=False,
        hue_cutoff_deg=95.0,
        neutral_chroma=8.0,
    )


def _synth(h: int = 32, w: int = 32) -> np.ndarray:
    rng = np.random.default_rng(7)
    return rng.integers(0, 256, (h, w, 3), dtype=np.uint8).astype(np.float32)


@pytest.mark.parametrize("cls", _concrete_classes())
def test_dither_output_shape_and_dtype(cls) -> None:
    from hokku_server.display import PALETTE_MEASURED_RGB
    d = cls()
    result = d.dither(_synth(32, 32), _fs_cfg())
    assert result.shape == (32, 32)
    assert result.dtype == np.uint8


@pytest.mark.parametrize("cls", _concrete_classes())
def test_dither_output_valid_palette_indices(cls) -> None:
    from hokku_server.display import PALETTE_MEASURED_RGB
    n_palette = len(PALETTE_MEASURED_RGB)
    d = cls()
    result = d.dither(_synth(32, 32), _fs_cfg())
    assert int(result.min()) >= 0
    assert int(result.max()) < n_palette


@pytest.mark.parametrize("algorithm", ["floyd_steinberg", "atkinson", "stucki", "noop"])
@pytest.mark.parametrize("cls", _concrete_classes())
def test_all_algorithms_all_classes(cls, algorithm: str) -> None:
    cfg = DitherConfig(
        algorithm=algorithm,
        lut_name="euclidean",
        serpentine=False,
        hue_cutoff_deg=95.0,
        neutral_chroma=8.0,
    )
    d = cls()
    result = d.dither(_synth(24, 24), cfg)
    assert result.shape == (24, 24)
    assert result.dtype == np.uint8


@pytest.mark.parametrize("cls", _concrete_classes())
def test_dither_with_prep_applies_preprocessing(cls) -> None:
    """dither_with_prep output must differ when prep_stripe adds a constant offset."""
    cfg = _fs_cfg()
    d = cls()
    canvas = np.random.default_rng(42).integers(0, 256, (32, 32, 3), dtype=np.uint8)

    def identity_prep(stripe):
        return stripe.astype(np.float32)

    def offset_prep(stripe):
        out = stripe.astype(np.float32)
        return np.clip(out + 50.0, 0, 255)

    result_plain = d.dither_with_prep(canvas, cfg, identity_prep)
    result_offset = d.dither_with_prep(canvas, cfg, offset_prep)
    assert not np.array_equal(result_plain, result_offset), (
        f"{cls.__name__}: prep_stripe had no effect on dither_with_prep"
    )
