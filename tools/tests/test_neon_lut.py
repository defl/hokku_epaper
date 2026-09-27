"""The neon LUT must calm saturated non-skin colour and nothing else.

Plain desaturation washed out faces; the whole point of this LUT is that skin
and neutrals come through exactly as the shipped correction renders them.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import neon_lut
from gamut_clamp import lab_to_rgb
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.image_renderer import ImageRenderer


@pytest.fixture(scope="module")
def shipped():
    path = DISPLAY_REGISTRY["huessen_epf1301"].correction_lut_path
    assert path is not None, "the huessen panel ships a correction LUT"
    return np.load(path).astype(np.float32)


def _through(lut, lab):
    rgb = lab_to_rgb(np.asarray(lab, dtype=np.float64)).astype(np.float32)
    return ImageRenderer.apply_correction_lut(rgb, lut)


# CIELAB (L, a, b): a mid skin tone at hue ~45°, chroma ~22
SKIN = [[62.0, 15.5, 15.5]]
GREY = [[50.0, 1.0, -1.0]]
NEON_GREEN = [[70.0, -45.0, 50.0]]
SKY_BLUE = [[60.0, -10.0, -45.0]]


@pytest.mark.parametrize("colour", [SKIN, GREY])
def test_skin_and_neutrals_render_exactly_as_shipped(shipped, colour):
    lut = neon_lut.build(shipped, 0.7, protect_skin=True)
    np.testing.assert_allclose(_through(lut, colour), _through(shipped, colour), atol=0.5)


@pytest.mark.parametrize("colour", [NEON_GREEN, SKY_BLUE])
def test_saturated_non_skin_colours_are_calmed(shipped, colour):
    lut = neon_lut.build(shipped, 0.7, protect_skin=True)
    assert np.abs(_through(lut, colour) - _through(shipped, colour)).max() > 5.0


def test_the_control_does_touch_skin(shipped):
    """Without protection the same strength moves skin — what the control tests."""
    lut = neon_lut.build(shipped, 0.7, protect_skin=False)
    assert np.abs(_through(lut, SKIN) - _through(shipped, SKIN)).max() > 1.0


def test_saturated_reds_in_skin_hues_are_still_calmed(shipped):
    """Goggles and crowns share skin's hue but not its chroma."""
    goggles = [[45.0, 45.0, 40.0]]  # hue ~42°, chroma ~60
    lut = neon_lut.build(shipped, 0.7, protect_skin=True)
    assert np.abs(_through(lut, goggles) - _through(shipped, goggles)).max() > 5.0
