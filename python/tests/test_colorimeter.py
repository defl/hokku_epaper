"""Instrument-side maths for panel colour measurement.

Two things here are easy to get wrong in ways that produce plausible but wrong
palette values, so both are pinned:

  * **White point.** Reflective spectrophotometers report D50 (a print
    convention); this codebase is sRGB/D65 throughout. Feeding D50 XYZ into
    ``xyz_to_lab`` silently shifts every ink anchor, and nothing downstream
    would flag it.
  * **Scale.** ArgyllCMS reports reflective XYZ as percentages (white ~80-100)
    while the codebase works with Y = 1.0 for a perfect diffuser. Getting this
    wrong by 100x is obvious; getting it wrong on *some* readings is not.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from hokku.webserver.dither_streaming import xyz_to_lab

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from colorimeter import (
    D50_XYZ,
    D65_XYZ,
    bradford_adapt,
    detect_xyz_scale,
    parse_xyz,
)


def test_adapting_a_white_point_to_itself_is_identity() -> None:
    for w in (D50_XYZ, D65_XYZ):
        np.testing.assert_allclose(bradford_adapt(w, w, w), w, atol=1e-12)


def test_source_white_maps_exactly_onto_destination_white() -> None:
    """The defining property of a chromatic adaptation."""
    np.testing.assert_allclose(bradford_adapt(D50_XYZ, D50_XYZ, D65_XYZ), D65_XYZ, atol=1e-12)
    np.testing.assert_allclose(bradford_adapt(D65_XYZ, D65_XYZ, D50_XYZ), D50_XYZ, atol=1e-12)


def test_adaptation_is_invertible() -> None:
    sample = np.array([0.2, 0.18, 0.09])
    there = bradford_adapt(sample, D50_XYZ, D65_XYZ)
    back = bradford_adapt(there, D65_XYZ, D50_XYZ)
    np.testing.assert_allclose(back, sample, atol=1e-12)


@pytest.mark.parametrize("level", [0.05, 0.2, 0.6, 1.0])
def test_a_d50_neutral_stays_neutral_after_adaptation(level: float) -> None:
    """A grey under D50 must still read as grey in D65 Lab.

    This is the check that actually catches a missing adaptation: skip it and
    the panel's neutral inks pick up a visible yellow-blue cast in a* / b*.
    """
    lab = xyz_to_lab(bradford_adapt(D50_XYZ * level, D50_XYZ, D65_XYZ))
    assert abs(lab[1]) < 1e-6, f"a* drifted: {lab}"
    assert abs(lab[2]) < 1e-6, f"b* drifted: {lab}"


def test_skipping_adaptation_would_visibly_shift_a_neutral() -> None:
    """Guards the guard: prove the adaptation is not a no-op worth deleting."""
    naive = xyz_to_lab(D50_XYZ * 0.6)  # D50 XYZ fed straight in, unadapted
    assert abs(naive[2]) > 3.0, (
        f"expected a large b* error without adaptation, got {naive} — if this "
        f"fails the test above proves nothing"
    )


@pytest.mark.parametrize(
    "readings,expected",
    [
        ([np.array([76.0, 80.0, 85.0])], 100.0),  # Argyll percent scale
        ([np.array([0.76, 0.80, 0.85])], 1.0),  # unit scale
        ([np.array([0.4, 0.5, 0.6]), np.array([76.0, 80.0, 85.0])], 100.0),
        ([], 1.0),
    ],
)
def test_scale_detection(readings, expected) -> None:
    assert detect_xyz_scale(readings) == expected


def test_scale_is_decided_by_the_whole_set_not_per_reading() -> None:
    """A dark patch reads below 2 on either scale.

    Deciding per reading would classify black as unit-scale and white as
    percent-scale in the same session, so the set is judged together.
    """
    black = np.array([0.9, 1.0, 1.1])  # dark patch, percent scale
    white = np.array([76.0, 80.0, 85.0])
    assert detect_xyz_scale([black, white]) == 100.0
    assert detect_xyz_scale([black]) == 1.0  # alone, genuinely ambiguous


@pytest.mark.parametrize(
    "text,expected",
    [
        ("24.31 25.55 21.37", [24.31, 25.55, 21.37]),
        ("24.31,25.55,21.37", [24.31, 25.55, 21.37]),
        (
            "Result is XYZ: 24.316536 25.550340 21.371308, D50 Lab: 57.56 -1.95 8.29",
            [24.316536, 25.550340, 21.371308],
        ),
    ],
)
def test_parse_xyz_takes_xyz_and_ignores_the_instrument_lab(text, expected) -> None:
    """The Lab on a spotread line is against the instrument's white point.

    We do our own adaptation, so that value must never be picked up.
    """
    parsed = parse_xyz(text)
    assert parsed is not None
    np.testing.assert_allclose(parsed, expected)


@pytest.mark.parametrize("text", ["", "   ", "no numbers here", "1 2"])
def test_parse_xyz_rejects_junk(text: str) -> None:
    assert parse_xyz(text) is None
