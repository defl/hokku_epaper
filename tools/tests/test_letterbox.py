"""The letterbox experiment must differ from production only where there are bars.

If `PhotoFirstRenderer` changed full-frame photos too, a glass A/B would credit
the fix with an effect it did not cause. And the geometry-only switch is a
monkeypatch of a production module global, so it must be undone even on error.
"""

import dataclasses
import os
import sys

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import hokku.webserver.image_abc as image_abc
import letterbox
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_streaming_numba import NumbaStreamingDither
from hokku.webserver.image_renderer import ImageRenderer
from hokku.webserver.orientation import Orientation
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS


@pytest.fixture(scope="module")
def display():
    return DISPLAY_REGISTRY["huessen_epf1301"]


@pytest.fixture(scope="module")
def cfg():
    # Autocontrast with a cutoff and a non-unit contrast: the two stages that
    # read image-wide statistics, so the leak is actually exercised.
    base = PRESET_IMAGE_CONFIGS["default_general"]
    assert base.prepare_autocontrast_cutoff > 0 or base.prepare_contrast != 1.0
    return base


def _photo(w: int, h: int) -> Image.Image:
    """A mid-toned gradient with no pure white, so bars and photo differ."""
    y, x = np.mgrid[0:h, 0:w]
    r = 40 + 140 * x / w
    g = 60 + 100 * y / h
    b = 90 + 60 * (x + y) / (w + h)
    return Image.fromarray(np.stack([r, g, b], -1).astype(np.uint8))


def _prepare(renderer, img, cfg, display):
    return renderer._prepare_canvas(
        img, cfg, Orientation.LANDSCAPE, display.panel_w, display.panel_h
    )


@pytest.mark.parametrize("clahe", [0.0, 1.75])  # repo default, and the live server's
def test_full_frame_is_exactly_production(display, cfg, clahe):
    cfg = dataclasses.replace(cfg, clahe_clip_limit=clahe)
    img = _photo(400, 300)  # the panel's own 4:3, so no padding
    prod, pad = _prepare(
        ImageRenderer(dither=NumbaStreamingDither(display), display=display), img, cfg, display
    )
    mine, pad2 = _prepare(letterbox.photo_first_renderer(display), img, cfg, display)
    assert not pad.any()
    np.testing.assert_array_equal(pad, pad2)
    np.testing.assert_array_equal(np.asarray(prod), np.asarray(mine))


def _visible(arr, display):
    """Undo the panel rotation _prepare_canvas applies after enhancing."""
    return np.rot90(np.asarray(arr), 1) if display.panel_rotated else np.asarray(arr)


def test_letterboxed_photo_is_processed_without_its_bars(display, cfg):
    img = _photo(300, 400)  # portrait on a landscape panel: wide bars
    arr, padding = _prepare(letterbox.photo_first_renderer(display), img, cfg, display)
    vis, pad_vis = _visible(arr, display), _visible(padding, display)
    rect = letterbox.picture_rect(pad_vis)
    assert rect is not None
    x0, y0, x1, y1 = rect
    plain = ImageRenderer(dither=NumbaStreamingDither(display), display=display)
    with letterbox._geometry_only():
        raw, _ = _prepare(plain, img, cfg, display)
    crop = Image.fromarray(np.ascontiguousarray(_visible(raw, display)[y0:y1, x0:x1]))
    alone = np.asarray(image_abc._apply_prepare_enhancements(crop, cfg))
    np.testing.assert_array_equal(vis[y0:y1, x0:x1], alone)
    # and the bars are left white, as production leaves them before dithering
    assert (np.asarray(arr)[padding] == 255).all()


def test_the_leak_it_fixes_is_real(display, cfg):
    """Production's picture area depends on the bars; that is the whole point."""
    img = _photo(300, 400)
    prod, padding = _prepare(
        ImageRenderer(dither=NumbaStreamingDither(display), display=display), img, cfg, display
    )
    mine, _ = _prepare(letterbox.photo_first_renderer(display), img, cfg, display)
    rect = letterbox.picture_rect(padding)
    assert rect is not None
    x0, y0, x1, y1 = rect
    assert not np.array_equal(np.asarray(prod)[y0:y1, x0:x1], mine[y0:y1, x0:x1])


def test_bar_colour_does_not_touch_the_picture(display, cfg):
    """With the photo processed alone, the bars are purely a surround."""
    img = _photo(300, 400)
    white, padding = _prepare(letterbox.photo_first_renderer(display, 1), img, cfg, display)
    black, _ = _prepare(letterbox.photo_first_renderer(display, 0), img, cfg, display)
    np.testing.assert_array_equal(white[~padding], black[~padding])
    assert (black[padding] == 0).all()


@pytest.mark.parametrize("bar_ink", [0, 1])
def test_bars_are_the_requested_ink_after_dithering(display, cfg, bar_ink):
    renderer = letterbox.photo_first_renderer(display, bar_ink)
    w, h = display.panel_w // 8, display.panel_h // 8  # small: this runs the real dither
    idx = renderer.render_indices(_photo(30, 40), cfg, Orientation.LANDSCAPE, w, h)
    padding = renderer._last_padding
    assert padding is not None and padding.any()
    assert (idx[padding] == bar_ink).all()


def test_geometry_switch_is_always_undone():
    real = image_abc._apply_prepare_enhancements
    with pytest.raises(RuntimeError), letterbox._geometry_only():
        assert image_abc._apply_prepare_enhancements is not real
        raise RuntimeError("boom")
    assert image_abc._apply_prepare_enhancements is real
