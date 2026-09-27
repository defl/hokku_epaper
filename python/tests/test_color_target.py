"""The colour-calibration target must survive the render pipeline pixel-exactly.

Measurements taken off the glass are only meaningful if the panel is showing
exactly what ``tools/color_target.py`` designed:

  * an ink patch must be 100 % that ink — a single stray pixel of another ink
    inside the meter's aperture biases the anchor we are about to write into
    ``display.py``;
  * a ramp patch must hold its intended black-ink area fraction to the pixel,
    because that fraction *is* the x-axis of the tone-response curve.

The pipeline has plenty of stages that would quietly violate both (resampling,
autocontrast, gamma, CLAHE, unsharp mask, error diffusion). These tests pin the
``calibration_raw`` preset as a true passthrough, so a future pipeline change
that would corrupt the target fails here instead of producing plausible-looking
but wrong measurements.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_streaming_numba import NumbaStreamingDither
from hokku.webserver.image_renderer import ImageRenderer
from hokku.webserver.orientation import Orientation
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from color_target import (
    BLACK_IDX,
    RAMP_EIGHTHS,
    WHITE_IDX,
    build_index_raster,
    index_raster_to_png,
    plan_patches,
)

_MODELS = ["bigme_f7", "huessen_epf1301"]


def _target_for(model: str):
    """(display, patches, intended index raster, PNG) for *model*."""
    display = DISPLAY_REGISTRY[model]
    patches = plan_patches(
        n_ink=int(display.palette_measured_rgb.shape[0]),
        include_ramp=True,
        panel_w=display.panel_w,
        panel_h=display.panel_h,
        cols=5,
        rows=3,
        min_gutter=8,
    )
    idx = build_index_raster(patches, display.panel_w, display.panel_h)
    return display, patches, idx, index_raster_to_png(idx, display)


@pytest.mark.parametrize("model", _MODELS)
def test_ramp_patches_have_exact_black_coverage(model: str) -> None:
    """Each ramp patch holds exactly k/8 black pixels — the curve's x-axis."""
    _display, patches, idx, _png = _target_for(model)
    ramp = [p for p in patches if p.kind == "ramp"]
    assert len(ramp) == len(RAMP_EIGHTHS)
    for p in ramp:
        region = idx[p.y : p.y + p.h, p.x : p.x + p.w]
        # Only the two neutral inks may appear in a ramp patch.
        assert set(np.unique(region)) <= {BLACK_IDX, WHITE_IDX}
        actual = np.count_nonzero(region == BLACK_IDX) / region.size
        assert actual == pytest.approx(p.black_fraction, abs=1e-9), (
            f"{p.name}: intended {p.black_fraction}, raster has {actual}"
        )


@pytest.mark.parametrize("model", _MODELS)
def test_ink_patches_are_a_single_ink(model: str) -> None:
    display, patches, idx, _png = _target_for(model)
    seen = set()
    for p in (q for q in patches if q.kind == "ink"):
        region = idx[p.y : p.y + p.h, p.x : p.x + p.w]
        assert np.unique(region).tolist() == [p.ink_index], f"{p.name} is not a flat ink patch"
        seen.add(p.ink_index)
    # Every ink in the palette gets an anchor patch, or the calibration run
    # would leave some entry of display.py un-measured.
    assert seen == set(range(int(display.palette_measured_rgb.shape[0])))


@pytest.mark.parametrize("model", _MODELS)
def test_patches_do_not_overlap(model: str) -> None:
    _display, patches, _idx, _png = _target_for(model)
    for a, b in ((a, b) for i, a in enumerate(patches) for b in patches[i + 1 :]):
        separated = a.x + a.w <= b.x or b.x + b.w <= a.x or a.y + a.h <= b.y or b.y + b.h <= a.y
        assert separated, f"{a.name} overlaps {b.name}"


@pytest.mark.parametrize("model", _MODELS)
def test_render_pipeline_reproduces_the_target_exactly(model: str) -> None:
    """The whole point: PNG → real renderer → the very indices we designed.

    This is the test that makes the measurements trustworthy. It runs the
    production ``ImageRenderer`` with the ``calibration_raw`` preset over the
    generated PNG and demands a bit-exact match against the intended raster.
    """
    display, _patches, idx, png = _target_for(model)
    renderer = ImageRenderer(NumbaStreamingDither(display), display=display)
    # The raster is authored in panel-memory coordinates. On a rotated panel
    # that memory is portrait, so the target must be fed as PORTRAIT for the
    # canvas to line up 1:1 and skip the -90° rotate; on a natively-landscape
    # panel (F7) memory and visible geometry are the same.
    orientation = Orientation.PORTRAIT if display.panel_rotated else Orientation.LANDSCAPE
    rendered = renderer.render_indices(
        png,
        PRESET_IMAGE_CONFIGS["calibration_raw"],
        orientation,
        display.panel_w,
        display.panel_h,
        crop_to_fill_threshold=0.0,
    )

    assert rendered.shape == idx.shape
    mismatches = int(np.count_nonzero(rendered != idx))
    assert mismatches == 0, (
        f"{model}: {mismatches} of {idx.size} pixels changed in the render pipeline "
        f"({100 * mismatches / idx.size:.4f}%) — the calibration target is being "
        f"altered, so measurements taken from it would be wrong"
    )


@pytest.mark.parametrize("model", _MODELS)
def test_panel_byte_round_trip(model: str) -> None:
    """Packing to wire bytes and back is lossless for the target."""
    display, _patches, idx, _png = _target_for(model)
    raw = display.indices_to_panel_bytes(idx)
    assert len(raw) == display.total_bytes
    np.testing.assert_array_equal(display.panel_bytes_to_indices(raw), idx)
