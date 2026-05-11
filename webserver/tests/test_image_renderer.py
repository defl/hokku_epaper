"""Smoke tests for ImageRenderer and the AbstractImageRenderer interface."""
from __future__ import annotations

import importlib

import numpy as np
import pytest
from PIL import Image

from hokku_server.dither_streaming import StreamingDither
from hokku_server.dither_unconstrained import UnconstrainedDither
from hokku_server.image_config import ImageConfig
from hokku_server.image_renderer import ImageRenderer
from hokku_server.presets import PRESET_IMAGE_CONFIGS

_NUMBA_AVAILABLE = importlib.util.find_spec("numba") is not None


def _dither_params():
    params = [
        pytest.param(StreamingDither(), id="streaming"),
        pytest.param(UnconstrainedDither(), id="unconstrained"),
    ]
    if _NUMBA_AVAILABLE:
        from hokku_server.dither_streaming_numba import NumbaDither
        params.append(pytest.param(NumbaDither(), id="numba"))
    else:
        from hokku_server.dither_abc import AbstractDither
        params.append(pytest.param(
            pytest.mark.skip(reason="numba not installed")(StreamingDither()),
            id="numba",
        ))
    return params


def _synth_img(w: int = 64, h: int = 64) -> Image.Image:
    rng = np.random.default_rng(0)
    arr = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
    return Image.fromarray(arr, mode="RGB")


def _noop_cfg() -> ImageConfig:
    from dataclasses import replace
    from hokku_server.dither_config import DitherConfig
    base = PRESET_IMAGE_CONFIGS["floyd_steinberg"]
    return replace(
        base,
        prepare_autocontrast_cutoff=0.0,
        prepare_gamma=1.0,
        prepare_brightness=1.0,
        prepare_contrast=1.0,
        prepare_sharpness=1.0,
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

def test_default_dither_is_streaming() -> None:
    r = ImageRenderer(StreamingDither())
    assert isinstance(r.dither, StreamingDither)


def test_explicit_dither_stored() -> None:
    d = UnconstrainedDither()
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
    from hokku_server.display import PALETTE_MEASURED_RGB
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
    from hokku_server.display import PALETTE_MEASURED_RGB
    n_palette = len(PALETTE_MEASURED_RGB)
    cfg = _noop_cfg()
    idx = ImageRenderer(dither).render_indices(
        _synth_img(48, 48), cfg, "portrait", 48, 48
    )
    assert idx.shape == (48, 48)
    assert idx.dtype == np.uint8
    assert int(idx.min()) >= 0
    assert int(idx.max()) < n_palette


def test_streaming_and_unconstrained_agree_on_preprocessed_canvas() -> None:
    """Dither classes must produce identical output when given the same float32 canvas.

    We bypass the renderer and call dither() directly so preprocessing is
    identical (none — the canvas is already float32).
    """
    from hokku_server.dither_config import DitherConfig

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


@pytest.mark.skipif(
    not _NUMBA_AVAILABLE,
    reason="numba not installed",
)
def test_numba_and_streaming_agree_on_preprocessed_canvas() -> None:
    """NumbaDither must match StreamingDither on the same float32 canvas."""
    from hokku_server.dither_config import DitherConfig
    from hokku_server.dither_streaming_numba import NumbaDither

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
    idx_n = NumbaDither().dither(arr, cfg)
    np.testing.assert_array_equal(
        idx_s, idx_n,
        err_msg="NumbaDither diverged from StreamingDither on identical preprocessed canvas",
    )
