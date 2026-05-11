"""Smoke tests for NumbaStreamingDither and NumbaUnconstrainedDither.

numba is a hard dependency (see pyproject.toml). Import failure is a real error.

Parity tests against the pure-Python reference implementations (StreamingDither,
UnconstrainedDither) live in test_dither_quality.py, marked time_intensive.
"""
from __future__ import annotations

import numba  # hard dep — must be installed
import numpy as np
import pytest

from hokku_server.dither_config import DitherConfig
from hokku_server.dither_streaming_numba import NumbaStreamingDither
from hokku_server.dither_unconstrained_numba import NumbaUnconstrainedDither
from hokku_server.display import PALETTE_MEASURED_RGB


def _fs_cfg() -> DitherConfig:
    return DitherConfig(
        algorithm="floyd_steinberg",
        lut_name="euclidean",
        serpentine=False,
        hue_cutoff_deg=95.0,
        neutral_chroma=8.0,
    )


def _synth(h: int = 64, w: int = 64) -> np.ndarray:
    """Synthetic H×W×3 uint8 gradient image."""
    rng = np.random.default_rng(42)
    return rng.integers(0, 256, (h, w, 3), dtype=np.uint8)


def _numba_classes():
    return [
        pytest.param(NumbaStreamingDither, id="numba_streaming"),
        pytest.param(NumbaUnconstrainedDither, id="numba_unconstrained"),
    ]


@pytest.mark.parametrize("cls", _numba_classes())
def test_instantiation_succeeds(cls) -> None:
    """Both Numba dithers must construct without raising when numba is available."""
    d = cls()
    assert d is not None


@pytest.mark.parametrize("cls", _numba_classes())
def test_dither_output_shape_and_dtype(cls) -> None:
    d = cls()
    canvas = _synth(64, 64).astype(np.float32)
    result = d.dither(canvas, _fs_cfg())
    assert result.shape == (64, 64)
    assert result.dtype == np.uint8


@pytest.mark.parametrize("cls", _numba_classes())
def test_dither_output_valid_palette_indices(cls) -> None:
    n_palette = len(PALETTE_MEASURED_RGB)
    d = cls()
    canvas = _synth(64, 64).astype(np.float32)
    result = d.dither(canvas, _fs_cfg())
    assert int(result.min()) >= 0
    assert int(result.max()) < n_palette


@pytest.mark.parametrize("algorithm", ["floyd_steinberg", "atkinson", "stucki"])
@pytest.mark.parametrize("cls", _numba_classes())
def test_all_algorithms_produce_valid_output(cls, algorithm: str) -> None:
    cfg = DitherConfig(
        algorithm=algorithm,
        lut_name="euclidean",
        serpentine=False,
        hue_cutoff_deg=95.0,
        neutral_chroma=8.0,
    )
    d = cls()
    canvas = _synth(48, 48).astype(np.float32)
    result = d.dither(canvas, cfg)
    assert result.shape == (48, 48)
    assert result.dtype == np.uint8


@pytest.mark.parametrize("cls", _numba_classes())
def test_dither_with_prep_applies_preprocessing(cls) -> None:
    """dither_with_prep output must differ when prep_stripe adds a constant offset."""
    cfg = _fs_cfg()
    d = cls()
    canvas = _synth(64, 64)

    def identity_prep(stripe):
        return stripe.astype(np.float32)

    def offset_prep(stripe):
        out = stripe.astype(np.float32)
        out = np.clip(out + 50.0, 0, 255)
        return out

    result_plain = d.dither_with_prep(canvas, cfg, identity_prep)
    result_offset = d.dither_with_prep(canvas, cfg, offset_prep)
    assert not np.array_equal(result_plain, result_offset), (
        "prep_stripe had no effect — dither_with_prep is not applying preprocessing"
    )


def test_numba_streaming_and_numba_unconstrained_agree() -> None:
    """NumbaStreamingDither and NumbaUnconstrainedDither must produce
    bit-identical results on the same float32 canvas (same algorithm, same LUT,
    no preprocessing)."""
    cfg = _fs_cfg()
    canvas = _synth(32, 32).astype(np.float32)

    streaming_result = NumbaStreamingDither().dither(canvas, cfg)
    unconstrained_result = NumbaUnconstrainedDither().dither(canvas, cfg)

    np.testing.assert_array_equal(
        streaming_result, unconstrained_result,
        err_msg="NumbaStreamingDither and NumbaUnconstrainedDither diverged on identical input",
    )
