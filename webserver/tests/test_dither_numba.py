"""Smoke tests for NumbaDither.

Skipped entirely when numba is not installed (pytest.importorskip).
"""
from __future__ import annotations

import numpy as np
import pytest

numba = pytest.importorskip("numba", reason="numba not installed")

from hokku_server.dither_config import DitherConfig
from hokku_server.dither_numba import NumbaDither
from hokku_server.dither_streaming import StreamingDither
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


def test_instantiation_requires_numba() -> None:
    """NumbaDither() must not raise when numba is available."""
    d = NumbaDither()
    assert d is not None


def test_dither_output_shape_and_dtype() -> None:
    d = NumbaDither()
    canvas = _synth(64, 64).astype(np.float32)
    result = d.dither(canvas, _fs_cfg())
    assert result.shape == (64, 64)
    assert result.dtype == np.uint8


def test_dither_output_valid_palette_indices() -> None:
    n_palette = len(PALETTE_MEASURED_RGB)
    d = NumbaDither()
    canvas = _synth(64, 64).astype(np.float32)
    result = d.dither(canvas, _fs_cfg())
    assert int(result.min()) >= 0
    assert int(result.max()) < n_palette


@pytest.mark.parametrize("algorithm", ["floyd_steinberg", "atkinson", "stucki"])
def test_all_algorithms_produce_valid_output(algorithm: str) -> None:
    cfg = DitherConfig(
        algorithm=algorithm,
        lut_name="euclidean",
        serpentine=False,
        hue_cutoff_deg=95.0,
        neutral_chroma=8.0,
    )
    d = NumbaDither()
    canvas = _synth(48, 48).astype(np.float32)
    result = d.dither(canvas, cfg)
    assert result.shape == (48, 48)
    assert result.dtype == np.uint8


def test_dither_with_prep_applies_preprocessing() -> None:
    """dither_with_prep output must differ when prep_stripe adds a constant offset."""
    cfg = _fs_cfg()
    d = NumbaDither()
    canvas = _synth(64, 64)

    def identity_prep(stripe):
        return stripe.astype(np.float32)

    def offset_prep(stripe):
        out = stripe.astype(np.float32)
        out = np.clip(out + 50.0, 0, 255)
        return out

    result_plain = d.dither_with_prep(canvas, cfg, identity_prep)
    result_offset = d.dither_with_prep(canvas, cfg, offset_prep)
    # A constant +50 luminance shift must change at least some palette assignments.
    assert not np.array_equal(result_plain, result_offset), (
        "prep_stripe had no effect — dither_with_prep is not applying preprocessing"
    )


def test_numba_matches_streaming_on_small_image() -> None:
    """NumbaDither and StreamingDither must produce bit-identical results on a
    small synthetic image (same algorithm, same LUT, no preprocessing)."""
    cfg = _fs_cfg()
    canvas = _synth(32, 32).astype(np.float32)

    streaming = StreamingDither().dither(canvas, cfg)
    numba_result = NumbaDither().dither(canvas, cfg)

    np.testing.assert_array_equal(
        streaming, numba_result,
        err_msg="NumbaDither diverged from StreamingDither on identical input",
    )
