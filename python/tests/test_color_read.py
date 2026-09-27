"""The colorimeter normalisation maths must recover a known answer.

``color_read`` turns raw instrument tristimulus values â€” carrying an unknown
lamp brightness and colour cast â€” into D65 Lab. If that chain is wrong the
numbers still *look* plausible, and the error lands silently in
``palette_measured_rgb``. So the round trip is tested against a synthetic
measurement session whose true answer is known by construction: take a
pretend panel, simulate what a meter would report under a coloured lamp, and
require the analysis to give the panel back.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_streaming import rgb_to_lab, xyz_to_lab

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from color_read import (
    D65_XYZ,
    analyse,
    delta_e76,
    lab_to_xyz,
    normalise,
    parse_reading,
)
from color_target import (
    INK_NAMES,
    actual_black_fraction,
    build_index_raster,
    plan_patches,
)

_MODEL = "bigme_f7"

# An arbitrary lamp: 137Ã— brighter than the reference scale and distinctly
# warm, so a normalisation that forgets either the scale or the cast fails.
LAMP = np.array([137.0 * 1.09, 137.0 * 1.00, 137.0 * 0.72])

# A real-ish white reference standard: bright, very slightly warm â€” not the
# perfect diffuser, so the test also catches ignoring the reference's own Lab.
WHITE_REF_LAB = np.array([96.5, -0.4, 1.2])


def _simulate(lab_true: np.ndarray) -> np.ndarray:
    """What a meter reports for a sample of *lab_true* under LAMP."""
    return lab_to_xyz(lab_true) * LAMP


def test_lab_to_xyz_inverts_xyz_to_lab() -> None:
    for lab in ([50.0, 20.0, -30.0], [96.5, -0.4, 1.2], [12.0, 1.0, -2.0], [0.0, 0.0, 0.0]):
        np.testing.assert_allclose(xyz_to_lab(lab_to_xyz(np.array(lab))), lab, atol=1e-6)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("24.31 25.55 21.37", [24.31, 25.55, 21.37]),
        ("24.31,25.55,21.37", [24.31, 25.55, 21.37]),
        ("  24.31\t25.55\t21.37  ", [24.31, 25.55, 21.37]),
        # A whole spotread line: the XYZ triple wins, the D50 Lab is ignored.
        (
            "Result is XYZ: 24.316536 25.550340 21.371308, D50 Lab: 57.56 -1.95 8.29",
            [24.316536, 25.550340, 21.371308],
        ),
        ("-1.5e-2 0.5 1", [-0.015, 0.5, 1.0]),
    ],
)
def test_parse_reading_accepts_the_formats_an_operator_will_paste(text, expected) -> None:
    parsed = parse_reading(text)
    assert parsed is not None, f"failed to parse {text!r}"
    np.testing.assert_allclose(parsed, expected)


@pytest.mark.parametrize("text", ["", "   ", "no numbers here", "1 2"])
def test_parse_reading_rejects_junk(text: str) -> None:
    assert parse_reading(text) is None


def test_normalise_removes_lamp_scale_and_cast() -> None:
    """A sample measured under LAMP comes back at its true D65 XYZ."""
    sample_lab = np.array([42.0, 18.0, -25.0])
    recovered = normalise(_simulate(sample_lab), _simulate(WHITE_REF_LAB), WHITE_REF_LAB)
    assert delta_e76(xyz_to_lab(recovered), sample_lab) < 0.01


def test_panel_white_fallback_forces_white_neutral() -> None:
    """The documented degradation actually is what the docstring claims."""
    tinted_white = np.array([78.0, -3.5, -1.2])  # a real panel white, blue-grey
    recovered = normalise(_simulate(tinted_white), _simulate(tinted_white), None)
    lab = xyz_to_lab(recovered)
    np.testing.assert_allclose(lab, [100.0, 0.0, 0.0], atol=1e-6)


def _manifest() -> dict:
    """The manifest color_target.py would write for this model."""
    display = DISPLAY_REGISTRY[_MODEL]
    patches = plan_patches(
        n_ink=6,
        include_ramp=True,
        panel_w=display.panel_w,
        panel_h=display.panel_h,
        cols=5,
        rows=3,
        min_gutter=8,
    )
    idx = build_index_raster(patches, display.panel_w, display.panel_h)
    manifest = {
        "model": _MODEL,
        "ink_names": list(INK_NAMES),
        "palette_measured_rgb": display.palette_measured_rgb.tolist(),
        "patches": [
            {
                "order": p.order,
                "name": p.name,
                "kind": p.kind,
                "ink_index": p.ink_index,
                "black_fraction": p.black_fraction,
                "row": p.row,
                "col": p.col,
                "x": p.x,
                "y": p.y,
                "w": p.w,
                "h": p.h,
                "actual_black_fraction": actual_black_fraction(idx, p),
                "center_px": list(p.center),
            }
            for p in patches
        ],
    }
    return manifest


def _reflectance_linear_ramp(
    manifest: dict, true_labs: dict[int, np.ndarray]
) -> dict[float, np.ndarray]:
    """A ramp that mixes exactly linearly in luminance â€” the physics baseline."""
    y_black = lab_to_xyz(true_labs[0])[1]
    y_white = lab_to_xyz(true_labs[1])[1]
    out = {}
    for p in manifest["patches"]:
        if p["kind"] == "ramp":
            f = p["actual_black_fraction"]
            out[f] = xyz_to_lab(D65_XYZ * (f * y_black + (1 - f) * y_white))
    return out


def _readings(
    manifest: dict, true_labs: dict[int, np.ndarray], ramp_labs: dict[float, np.ndarray]
) -> dict[str, list[float]]:
    """Simulate what the meter reports for every patch in the manifest."""
    readings = {"white_ref": _simulate(WHITE_REF_LAB).tolist()}
    for p in manifest["patches"]:
        if p["kind"] == "ink":
            readings[str(p["order"])] = _simulate(true_labs[p["ink_index"]]).tolist()
        else:
            readings[str(p["order"])] = _simulate(ramp_labs[p["actual_black_fraction"]]).tolist()
    return readings


def test_analyse_recovers_the_true_ink_anchors() -> None:
    """End to end: simulated session in, correct palette out."""
    display = DISPLAY_REGISTRY[_MODEL]
    # "Truth" = the repo's current values shifted by a known amount, so the
    # reported Î”E-vs-repo is a quantity we can predict rather than eyeball.
    true_labs = {
        i: rgb_to_lab(display.palette_measured_rgb[i]) + np.array([2.0, 1.0, -1.5])
        for i in range(6)
    }
    manifest = _manifest()
    readings = _readings(manifest, true_labs, _reflectance_linear_ramp(manifest, true_labs))

    result = analyse(manifest, readings, WHITE_REF_LAB)

    for ink in range(6):
        a = result["anchors"][ink]
        assert a is not None, f"ink {ink} not recovered"
        assert delta_e76(a["lab"], true_labs[ink]) < 0.05, f"{a['name']} anchor is wrong"
        # Black and white are metered twice; the repeats must agree.
        if ink in (0, 1):
            assert a["n"] == 2
            assert a["spread_de"] < 0.01
        # The shift we injected is 2/1/-1.5 â†’ Î”E76 = sqrt(4+1+2.25).
        assert a["shift_de"] == pytest.approx(np.sqrt(7.25), abs=0.05)


def test_analyse_flags_a_panel_that_mixes_linearly_in_reflectance() -> None:
    """A ramp built to be reflectance-linear must report as such."""
    display = DISPLAY_REGISTRY[_MODEL]
    true_labs = {i: rgb_to_lab(display.palette_measured_rgb[i]) for i in range(6)}
    manifest = _manifest()
    readings = _readings(manifest, true_labs, _reflectance_linear_ramp(manifest, true_labs))

    result = analyse(manifest, readings, WHITE_REF_LAB)
    ramp = result["ramp"]
    assert len(ramp) == 7
    # By construction the measurement sits on the reflectance-linear
    # prediction, so that residual is ~0 ...
    for r in ramp:
        assert abs(r["measured_l"] - r["predicted_l_reflectance"]) < 0.05
    # ... while the sRGB-linear prediction the renderer implicitly assumes is
    # a genuinely different curve. If these two ever coincided, the reported
    # "error" column would be measuring nothing.
    assert max(abs(r["error_vs_srgb"]) for r in ramp) > 1.0


def test_analyse_survives_a_partial_session() -> None:
    """Skipped patches degrade the report, they don't crash it."""
    display = DISPLAY_REGISTRY[_MODEL]
    true_labs = {i: rgb_to_lab(display.palette_measured_rgb[i]) for i in range(6)}
    manifest = _manifest()
    readings = _readings(manifest, true_labs, _reflectance_linear_ramp(manifest, true_labs))
    # Drop every green and blue reading.
    dropped = {
        str(p["order"])
        for p in manifest["patches"]
        if p["kind"] == "ink" and p["ink_index"] in (4, 5)
    }
    readings = {k: v for k, v in readings.items() if k not in dropped}

    result = analyse(manifest, readings, WHITE_REF_LAB)
    assert result["anchors"][4] is None
    assert result["anchors"][5] is None
    assert result["anchors"][2] is not None
    assert len(result["ramp"]) == 7
