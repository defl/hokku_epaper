"""Smoke tests for ImageRenderer and the AbstractImageRenderer interface."""
from __future__ import annotations

from dataclasses import replace

import numba  # hard dep — must be installed
import numpy as np
import pytest
from PIL import Image

from hokku_server.display import PALETTE_MEASURED_RGB
from hokku_server.dither_config import DitherConfig
from hokku_server.dither_streaming import StreamingDither
from hokku_server.dither_streaming_numba import NumbaStreamingDither
from hokku_server.dither_unconstrained import UnconstrainedDither
from hokku_server.dither_unconstrained_numba import NumbaUnconstrainedDither
from hokku_server.image_config import ImageConfig
from hokku_server.image_renderer import ImageRenderer
from hokku_server.presets import PRESET_IMAGE_CONFIGS


def _dither_params():
    return [
        pytest.param(NumbaStreamingDither(), id="numba_streaming"),
        pytest.param(NumbaUnconstrainedDither(), id="numba_unconstrained"),
    ]


def _synth_img(w: int = 64, h: int = 64) -> Image.Image:
    rng = np.random.default_rng(0)
    arr = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
    return Image.fromarray(arr, mode="RGB")


def _noop_cfg() -> ImageConfig:
    base = PRESET_IMAGE_CONFIGS["floyd_steinberg"]
    return replace(
        base,
        prepare_autocontrast_cutoff=0.0,
        prepare_gamma=1.0,
        prepare_brightness=1.0,
        prepare_contrast=1.0,
        prepare_usm_amount=0,
        color_enhance=1.0,
        use_adaptive_saturate=False,
        adaptive_vivid=False,
        scale_chroma=False,
        dither=DitherConfig(
            algorithm="noop",
            lut_name="euclidean",
            serpentine=False,
            hue_cutoff_deg=95.0,
            neutral_chroma=8.0,
        ),
    )


# ── Construction ──────────────────────────────────────────────────────────────

def test_dither_stored_is_numba_streaming() -> None:
    r = ImageRenderer(NumbaStreamingDither())
    assert isinstance(r.dither, NumbaStreamingDither)


def test_explicit_dither_stored() -> None:
    d = NumbaUnconstrainedDither()
    r = ImageRenderer(d)
    assert r.dither is d


# ── render_indices ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("dither", _dither_params())
@pytest.mark.parametrize("orientation", ["portrait", "landscape"])
def test_render_indices_shape(dither, orientation: str) -> None:
    r = ImageRenderer(dither)
    img = _synth_img(60, 80)
    cfg = _noop_cfg()
    canvas_w, canvas_h = 48, 64
    idx = r.render_indices(img, cfg, orientation, canvas_w, canvas_h)
    assert idx.shape == (canvas_h, canvas_w)
    assert idx.dtype == np.uint8


@pytest.mark.parametrize("dither", _dither_params())
def test_render_indices_valid_palette_values(dither) -> None:

    n_palette = len(PALETTE_MEASURED_RGB)
    r = ImageRenderer(dither)
    idx = r.render_indices(_synth_img(), _noop_cfg(), "portrait", 32, 32)
    assert int(idx.min()) >= 0
    assert int(idx.max()) < n_palette


# ── render_panel_bytes / render_preview_png ───────────────────────────────────

@pytest.mark.parametrize("dither", _dither_params())
def test_render_preview_png_returns_bytes(dither) -> None:
    r = ImageRenderer(dither)
    img = _synth_img()
    data = r.render_preview_png(img, _noop_cfg(), "portrait", max_side_px=64)
    assert isinstance(data, bytes)
    assert data[:4] == b"\x89PNG"


# ── Strategy equivalence ──────────────────────────────────────────────────────

@pytest.mark.parametrize("dither", _dither_params())
def test_all_strategies_produce_valid_output(dither) -> None:
    """Every strategy must produce valid palette indices — correct shape and dtype."""

    n_palette = len(PALETTE_MEASURED_RGB)
    cfg = _noop_cfg()
    idx = ImageRenderer(dither).render_indices(
        _synth_img(48, 48), cfg, "portrait", 48, 48
    )
    assert idx.shape == (48, 48)
    assert idx.dtype == np.uint8
    assert int(idx.min()) >= 0
    assert int(idx.max()) < n_palette


@pytest.mark.time_intensive
def test_streaming_and_unconstrained_agree_on_preprocessed_canvas() -> None:
    """Pure-Python dithers must produce identical output on the same float32 canvas."""
    cfg = DitherConfig(
        algorithm="floyd_steinberg",
        lut_name="euclidean",
        serpentine=False,
        hue_cutoff_deg=95.0,
        neutral_chroma=8.0,
    )
    canvas = _synth_img(32, 32)
    arr = np.asarray(canvas, dtype=np.float32)

    idx_s = StreamingDither().dither(arr, cfg)
    idx_u = UnconstrainedDither().dither(arr, cfg)
    np.testing.assert_array_equal(
        idx_s, idx_u,
        err_msg="StreamingDither and UnconstrainedDither disagree on the same preprocessed canvas",
    )


@pytest.mark.time_intensive
def test_numba_streaming_and_streaming_agree_on_preprocessed_canvas() -> None:
    """NumbaStreamingDither must match StreamingDither on the same float32 canvas."""
    cfg = DitherConfig(
        algorithm="floyd_steinberg",
        lut_name="euclidean",
        serpentine=False,
        hue_cutoff_deg=95.0,
        neutral_chroma=8.0,
    )
    canvas = _synth_img(32, 32)
    arr = np.asarray(canvas, dtype=np.float32)

    idx_s = StreamingDither().dither(arr, cfg)
    idx_n = NumbaStreamingDither().dither(arr, cfg)
    np.testing.assert_array_equal(
        idx_s, idx_n,
        err_msg="NumbaStreamingDither diverged from StreamingDither on identical preprocessed canvas",
    )
