"""The render core must not assume where black and white sit in the palette.

A screen declares ``black_index`` / ``white_index`` on its Display. These tests
build a Huessen Display whose palette rows (and controller nibbles) are
reordered so black and white are NOT at 0 and 1, and check that it renders the
same picture — and the same wire bytes — as the original ordering.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest
from PIL import Image

from hokku.screens.huessen_epf1301.display import HuessenEpf1301Display
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_streaming import build_rgb_lut_bw, rgb_to_lab
from hokku.webserver.dither_unconstrained import _build_rgb_lut_bw
from hokku.webserver.image_renderer import ImageRenderer, renderer_for_display
from hokku.webserver.orientation import Orientation
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS

_HUESSEN = DISPLAY_REGISTRY["huessen_epf1301"]

# new row i holds old row _PERM[i]: yellow, red, white, blue, green, black.
_PERM = np.array([2, 3, 1, 4, 5, 0])


class _PermutedHuessen(HuessenEpf1301Display):
    model_id = "test_permuted_huessen"
    palette_measured_rgb = HuessenEpf1301Display.palette_measured_rgb[_PERM]
    palette_preview_rgb = HuessenEpf1301Display.palette_preview_rgb[_PERM]
    palette_nibble = HuessenEpf1301Display.palette_nibble[_PERM]
    black_index = 5
    white_index = 2


@pytest.fixture
def permuted(monkeypatch: pytest.MonkeyPatch) -> _PermutedHuessen:
    display = _PermutedHuessen()
    # The palette-LUT and correction-LUT caches look displays up by model_id.
    monkeypatch.setitem(DISPLAY_REGISTRY, display.model_id, display)
    return display


def _test_image() -> Image.Image:
    """A wide image (so a landscape fit leaves letterbox padding) with a grey
    ramp on top and saturated colour bands below."""
    w, h = 300, 100
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[: h // 2] = np.linspace(0, 255, w, dtype=np.uint8)[None, :, None]
    bands = np.array(
        [[220, 30, 30], [30, 160, 40], [30, 60, 200], [240, 220, 40], [128, 128, 128]],
        dtype=np.uint8,
    )
    arr[h // 2 :] = bands[(np.arange(w) * len(bands)) // w][None, :, :]
    return Image.fromarray(arr)


@pytest.mark.parametrize("build", [build_rgb_lut_bw, _build_rgb_lut_bw])
def test_bw_lut_uses_declared_black_and_white(build, permuted) -> None:
    lut, _ = build(permuted)
    assert set(np.unique(lut).tolist()) == {permuted.black_index, permuted.white_index}
    ref_lut, _ = build(_HUESSEN)
    # Same choice of ink at every grid point, just under the new index numbers.
    np.testing.assert_array_equal(_PERM[lut], ref_lut)


def test_palette_derived_drc_range_uses_declared_indices(permuted, monkeypatch) -> None:
    # Force the palette-derived path (Huessen normally carries measured anchors).
    monkeypatch.setattr(_PermutedHuessen, "drc_anchor_l", None)
    monkeypatch.setattr(HuessenEpf1301Display, "drc_anchor_l", None)
    perm_anchors = ImageRenderer(None, permuted)._drc_anchors()  # type: ignore[arg-type]
    ref_anchors = ImageRenderer(None, _HUESSEN)._drc_anchors()  # type: ignore[arg-type]
    assert perm_anchors == ref_anchors


@pytest.mark.parametrize("preset", ["default_general", "default_bw", "atkinson_hue_aware"])
def test_permuted_palette_renders_identically(preset: str, permuted) -> None:
    cfg = replace(PRESET_IMAGE_CONFIGS[preset], dither_noise=0.0)
    canvas_w, canvas_h = 120, 160  # small: this checks index plumbing, not quality
    ref = renderer_for_display(_HUESSEN).render_indices(
        _test_image(), cfg, Orientation.LANDSCAPE, canvas_w, canvas_h
    )
    got = renderer_for_display(permuted).render_indices(
        _test_image(), cfg, Orientation.LANDSCAPE, canvas_w, canvas_h
    )
    # The fit leaves letterbox bands; they must be the declared white.
    assert np.any(got == permuted.white_index)
    np.testing.assert_array_equal(_PERM[got], ref)


def test_permuted_palette_produces_identical_wire_bytes(permuted) -> None:
    """Nibbles were permuted with the rows, so the panel must get the same bytes."""
    cfg = replace(PRESET_IMAGE_CONFIGS["default_general"], dither_noise=0.0)
    ref = renderer_for_display(_HUESSEN).render_panel_bytes(
        _test_image(), cfg, Orientation.LANDSCAPE
    )
    got = renderer_for_display(permuted).render_panel_bytes(
        _test_image(), cfg, Orientation.LANDSCAPE
    )
    assert got == ref


def test_chroma_scaling_follows_the_given_anchors() -> None:
    """scale_chroma shrinks chroma by the anchor range — a narrower panel range
    must desaturate more, not use a fixed reference range."""
    red = np.array([[[220.0, 30.0, 30.0]]], dtype=np.float32)

    def chroma_after(anchor: tuple[float, float]) -> float:
        out = ImageRenderer._drc_cielab_chroma(
            red,
            anchor,
            scale_chroma=True,
            adaptive_vivid=False,
            vivid_chroma_low=5.0,
            vivid_chroma_high=15.0,
        )
        lab = rgb_to_lab(out.astype(np.float64))[0, 0]
        return float(np.hypot(lab[1], lab[2]))

    assert chroma_after((10.0, 40.0)) < chroma_after((10.0, 70.0))
