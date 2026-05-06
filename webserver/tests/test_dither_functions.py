"""Unit tests for each public helper in ``webserver.dither``."""
from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import numpy as np
import pytest
from PIL import Image

from webserver.display_constants import FULL_W, PANEL_H
from webserver.dither import (
    DitherConfig,
    LutName,
    adaptive_saturate,
    atkinson_dither,
    build_rgb_lut,
    build_rgb_lut_hue_aware,
    dither,
    floyd_steinberg_dither,
    linear_to_xyz,
    lut_and_scale_for_dither_config,
    rgb_to_lab,
    srgb_to_linear,
    stucki_dither,
    xyz_to_lab,
    _cached_euclidean_lut,
    _error_diffusion_workspace,
)
from webserver.image import PRESET_IMAGE_CONFIGS, compress_dynamic_range

_EUCLIDEAN_LUT, _EUCLIDEAN_GRID_SCALE = _cached_euclidean_lut()


def test_srgb_to_linear_scalar_and_array() -> None:
    out = srgb_to_linear(128.0)
    assert out.shape == ()
    assert 0.2 < float(out) < 0.25
    arr = srgb_to_linear(np.array([0.0, 255.0], dtype=np.float64))
    assert arr.shape == (2,)
    assert arr[0] == pytest.approx(0.0)
    assert arr[1] == pytest.approx(1.0)


def test_linear_to_xyz_shape() -> None:
    rgb = np.zeros((2, 3), dtype=np.float64)
    xyz = linear_to_xyz(rgb)
    assert xyz.shape == (2, 3)


def test_xyz_to_lab_shape() -> None:
    xyz = np.array([[0.1, 0.2, 0.3]], dtype=np.float64)
    lab = xyz_to_lab(xyz)
    assert lab.shape == (1, 3)


def test_rgb_to_lab_matches_chain() -> None:
    rgb = np.array([[[255.0, 0.0, 0.0]]], dtype=np.float64)
    lab = rgb_to_lab(rgb)
    assert lab.shape == (1, 1, 3)
    chain = xyz_to_lab(linear_to_xyz(srgb_to_linear(rgb)))
    np.testing.assert_allclose(lab, chain, rtol=1e-5)


def test_compress_dynamic_range_dtype_and_bounds() -> None:
    img = np.full((4, 4, 3), 128.0, dtype=np.float32)
    out = compress_dynamic_range(
        img,
        scale_chroma=False,
        adaptive_vivid=False,
        vivid_chroma_low=5.0,
        vivid_chroma_high=15.0,
    )
    assert out.dtype == np.float32
    assert out.shape == img.shape
    assert out.min() >= 0 and out.max() <= 255


def test_adaptive_saturate_dtype_and_bounds() -> None:
    img = np.full((4, 4, 3), 128.0, dtype=np.float64)
    out = adaptive_saturate(img, 1.2, 5.0, 15.0)
    assert out.dtype == np.float32
    assert out.shape == img.shape


def test_build_rgb_lut_cube_and_scale() -> None:
    lut, scale = build_rgb_lut()
    assert lut.shape == (32, 32, 32)
    assert lut.dtype == np.uint8
    assert np.all(lut < 6)
    assert scale == pytest.approx(256 / 32)


def test_build_rgb_lut_hue_aware_cube() -> None:
    lut, scale = build_rgb_lut_hue_aware(95.0, 8.0)
    assert lut.shape == (32, 32, 32)
    assert lut.dtype == np.uint8
    assert np.all(lut < 6)
    assert scale == pytest.approx(256 / 32)


def test_euclidean_and_hue_luts_resolve_via_config() -> None:
    lut_e, sc_e = _EUCLIDEAN_LUT, _EUCLIDEAN_GRID_SCALE
    assert lut_e.shape == (32, 32, 32)
    assert sc_e > 0
    cfg = DitherConfig(
        algorithm="atkinson",
        lut_name="hue_aware",
        serpentine=False,
        hue_cutoff_deg=95.0,
        neutral_chroma=8.0,
    )
    lut_h, sc_h = lut_and_scale_for_dither_config(cfg)
    assert lut_h.shape == (32, 32, 32)
    assert sc_h == sc_e


def test_error_diffusion_workspace_from_pil_and_array() -> None:
    lut = _EUCLIDEAN_LUT
    img = Image.new("RGB", (3, 4), color=(100, 50, 200))
    pixels, h, w, result_idx, pal_rgb, lut_max = _error_diffusion_workspace(
        img, lut, _EUCLIDEAN_GRID_SCALE,
    )
    assert h == 4 and w == 3
    assert pixels.shape == (4, 3, 3)
    assert result_idx.shape == (4, 3)
    assert lut_max == 31

    arr = np.zeros((2, 2, 3), dtype=np.uint8)
    _, h2, w2, _, _, _ = _error_diffusion_workspace(arr, lut, _EUCLIDEAN_GRID_SCALE)
    assert h2 == 2 and w2 == 2


def test_floyd_steinberg_small_serpentine_false() -> None:
    canvas = np.full((5, 5, 3), 200.0, dtype=np.float32)
    idx = floyd_steinberg_dither(canvas, _EUCLIDEAN_LUT, _EUCLIDEAN_GRID_SCALE, False)
    assert idx.shape == (5, 5)
    assert idx.dtype == np.uint8


def test_floyd_steinberg_serpentine_can_change_pattern() -> None:
    rng = np.random.default_rng(0)
    canvas = (rng.random((8, 8, 3)) * 255).astype(np.float32)
    a = floyd_steinberg_dither(canvas, _EUCLIDEAN_LUT, _EUCLIDEAN_GRID_SCALE, False)
    b = floyd_steinberg_dither(canvas, _EUCLIDEAN_LUT, _EUCLIDEAN_GRID_SCALE, True)
    assert not np.array_equal(a, b)


def test_atkinson_small() -> None:
    canvas = np.full((4, 4, 3), 150.0, dtype=np.float32)
    idx = atkinson_dither(canvas, _EUCLIDEAN_LUT, _EUCLIDEAN_GRID_SCALE, False)
    assert idx.shape == (4, 4)


def test_stucki_small() -> None:
    canvas = np.full((4, 4, 3), 150.0, dtype=np.float32)
    idx = stucki_dither(canvas, _EUCLIDEAN_LUT, _EUCLIDEAN_GRID_SCALE, False)
    assert idx.shape == (4, 4)


def _full_panel_canvas(fill: tuple[int, int, int] = (90, 120, 140)) -> Image.Image:
    return Image.new("RGB", (FULL_W, PANEL_H), fill)


@pytest.mark.time_intensive
@pytest.mark.parametrize("preset_name", sorted(PRESET_IMAGE_CONFIGS.keys()))
def test_dither_each_preset_full_panel(preset_name: str) -> None:
    preset = PRESET_IMAGE_CONFIGS[preset_name]
    image_cfg = replace(preset, dither=replace(preset.dither, serpentine=False))
    canvas = _full_panel_canvas()
    arr = np.asarray(canvas, dtype=np.float32)
    compressed = compress_dynamic_range(
        arr,
        scale_chroma=image_cfg.scale_chroma,
        adaptive_vivid=image_cfg.adaptive_vivid,
        vivid_chroma_low=image_cfg.vivid_chroma_low,
        vivid_chroma_high=image_cfg.vivid_chroma_high,
    )
    result = dither(Image.fromarray(compressed.astype(np.uint8)), image_cfg.dither)
    assert result.shape == (PANEL_H, FULL_W)
    assert result.dtype == np.uint8
    assert result.min() >= 0 and result.max() <= 5


def test_dither_unknown_algorithm_raises() -> None:
    base = PRESET_IMAGE_CONFIGS["floyd_steinberg"]
    bad_dither = replace(base.dither, algorithm=cast(Any, "not_an_algo"))
    with pytest.raises(ValueError, match="Unknown algorithm"):
        dither(_full_panel_canvas(), bad_dither)


def test_dither_unknown_lut_raises() -> None:
    base = PRESET_IMAGE_CONFIGS["floyd_steinberg"]
    bad_dither = replace(base.dither, lut_name=cast(LutName, "no_such_lut"))
    with pytest.raises(ValueError, match="Unknown lut_name"):
        dither(_full_panel_canvas(), bad_dither)
