"""Unit tests for image_quality.image_compare."""
from __future__ import annotations

import numpy as np
import pytest

from hokku_server.display import PALETTE_MEASURED_RGB
from hokku_server.image_quality import image_compare

_EXPECTED_KEYS = frozenset({
    "neutral_leak",
    "sat_hit",
    "overall_dE",
    "overall_dE2000",
    "neutral_blue_fraction",
    "lightness_dE",
    "chroma_dE",
    "hue_error",
    "error_roughness",
    "high_freq_energy_ratio",
})

# ── Shared fixtures ───────────────────────────────────────────────────────────


def _solid(color, h: int = 4, w: int = 4) -> np.ndarray:
    return np.full((h, w, 3), color, dtype=np.uint8)


# Neutrals (Lab chroma ≈ 0)
GREY = _solid([128, 128, 128])
DARK_GREY = _solid([80, 80, 80])
LIGHT_GREY = _solid([200, 200, 200])

# Palette ink solids
WHITE_INK = _solid(PALETTE_MEASURED_RGB[1].astype(np.uint8))
BLUE_INK = _solid(PALETTE_MEASURED_RGB[4].astype(np.uint8))

# Saturated colours (Lab chroma >> 25)
RED = _solid([200, 30, 30])
GREEN = _solid([30, 200, 30])


# ── Keys ─────────────────────────────────────────────────────────────────────


def test_returns_all_expected_keys():
    assert set(image_compare(GREY, GREY)) == _EXPECTED_KEYS


# ── Identical-image invariants ────────────────────────────────────────────────


def test_zero_error_on_identical_neutral():
    m = image_compare(GREY, GREY)
    assert m["overall_dE"] == pytest.approx(0.0, abs=1e-9)
    assert m["overall_dE2000"] == pytest.approx(0.0, abs=1e-9)
    assert m["lightness_dE"] == pytest.approx(0.0, abs=1e-9)
    assert m["chroma_dE"] == pytest.approx(0.0, abs=1e-9)
    assert m["error_roughness"] == pytest.approx(0.0, abs=1e-9)
    assert m["hue_error"] == pytest.approx(0.0, abs=1e-9)


def test_sat_hit_one_on_identical_saturated():
    m = image_compare(RED, RED)
    assert m["sat_hit"] == pytest.approx(1.0)


def test_hue_error_zero_on_identical_saturated():
    m = image_compare(RED, RED)
    assert m["hue_error"] == pytest.approx(0.0, abs=1e-9)


# ── neutral_leak ──────────────────────────────────────────────────────────────


def test_neutral_leak_low_for_grey_to_white_ink():
    m = image_compare(GREY, WHITE_INK)
    assert m["neutral_leak"] < 5.0  # white ink is near-neutral in Lab


def test_neutral_leak_high_for_grey_to_blue_ink():
    m = image_compare(GREY, BLUE_INK)
    assert m["neutral_leak"] > 20.0  # blue ink has Lab chroma ~50


def test_neutral_leak_grey_to_blue_exceeds_grey_to_white():
    assert image_compare(GREY, BLUE_INK)["neutral_leak"] > \
           image_compare(GREY, WHITE_INK)["neutral_leak"]


# ── neutral_blue_fraction ─────────────────────────────────────────────────────


def test_neutral_blue_fraction_is_one_for_grey_to_blue():
    m = image_compare(GREY, BLUE_INK)
    assert m["neutral_blue_fraction"] == pytest.approx(1.0)


def test_neutral_blue_fraction_is_zero_for_grey_to_white():
    m = image_compare(GREY, WHITE_INK)
    assert m["neutral_blue_fraction"] == pytest.approx(0.0)


# ── sat_hit ───────────────────────────────────────────────────────────────────


def test_sat_hit_zero_when_saturated_mapped_to_white():
    m = image_compare(RED, WHITE_INK)
    assert m["sat_hit"] == pytest.approx(0.0)


def test_sat_hit_zero_when_saturated_mapped_to_grey():
    m = image_compare(RED, GREY)
    assert m["sat_hit"] == pytest.approx(0.0)


# ── error_roughness ───────────────────────────────────────────────────────────


def test_error_roughness_zero_on_identical():
    assert image_compare(GREY, GREY)["error_roughness"] == pytest.approx(0.0, abs=1e-9)


def test_error_roughness_positive_on_different():
    # Mix of matching and non-matching pixels → std dev > 0
    orig = np.full((4, 4, 3), 128, dtype=np.uint8)
    derived = orig.copy()
    derived[0, 0] = [0, 0, 0]   # one very wrong pixel
    assert image_compare(orig, derived)["error_roughness"] > 0.0


# ── lightness_dE / chroma_dE ─────────────────────────────────────────────────


def test_lightness_de_nonzero_for_brightness_shift():
    m = image_compare(DARK_GREY, LIGHT_GREY)
    assert m["lightness_dE"] > 10.0
    assert m["chroma_dE"] < 2.0   # both are neutral; chroma barely changes


def test_chroma_de_nonzero_when_saturation_stripped():
    m = image_compare(RED, GREY)
    assert m["chroma_dE"] > 20.0


# ── hue_error ────────────────────────────────────────────────────────────────


def test_hue_error_large_for_red_to_green():
    m = image_compare(RED, GREEN)
    assert m["hue_error"] > 45.0


def test_hue_error_zero_when_no_saturated_pixels():
    # Both greys → no saturated source pixels → returns 0 sentinel
    m = image_compare(GREY, BLUE_INK)
    assert m["hue_error"] == pytest.approx(0.0)


# ── overall_dE / dE2000 ───────────────────────────────────────────────────────


def test_de2000_zero_on_identical():
    assert image_compare(GREY, GREY)["overall_dE2000"] == pytest.approx(0.0, abs=1e-9)


def test_de2000_non_negative():
    for orig, der in [(GREY, BLUE_INK), (RED, GREEN), (GREY, GREY)]:
        assert image_compare(orig, der)["overall_dE2000"] >= 0.0


def test_de2000_broadly_agrees_with_de76():
    m_same = image_compare(GREY, GREY)
    m_diff = image_compare(RED, BLUE_INK)
    assert m_diff["overall_dE2000"] > m_same["overall_dE2000"]


# ── high_freq_energy_ratio ────────────────────────────────────────────────────


def test_high_freq_ratio_zero_on_identical():
    assert image_compare(GREY, GREY)["high_freq_energy_ratio"] == pytest.approx(0.0)


def test_high_freq_ratio_checkerboard_exceeds_uniform():
    H, W = 32, 32
    original = np.full((H, W, 3), 128, dtype=np.uint8)

    # Uniform offset: pure DC error
    uniform = np.full((H, W, 3), 134, dtype=np.uint8)

    # Checkerboard: alternating ±6 → maximum high-frequency (Nyquist) error
    checker = np.empty((H, W, 3), dtype=np.uint8)
    y, x = np.indices((H, W))
    checker[(y + x) % 2 == 0] = 122
    checker[(y + x) % 2 == 1] = 134

    ratio_uniform = image_compare(original, uniform)["high_freq_energy_ratio"]
    ratio_checker = image_compare(original, checker)["high_freq_energy_ratio"]
    assert ratio_checker > ratio_uniform


# ── padding_mask ──────────────────────────────────────────────────────────────


def test_padding_mask_excludes_high_error_pixels():
    H, W = 6, 6
    original = np.full((H, W, 3), 128, dtype=np.uint8)
    derived = original.copy()
    # Bottom-right corner has large error — simulates letterbox region
    derived[4:, 4:] = PALETTE_MEASURED_RGB[4].astype(np.uint8)  # blue ink

    mask = np.zeros((H, W), dtype=bool)
    mask[4:, 4:] = True  # mark those pixels as padding

    without_mask = image_compare(original, derived)
    with_mask = image_compare(original, derived, padding_mask=mask)

    assert without_mask["overall_dE"] > 0.0
    assert with_mask["overall_dE"] == pytest.approx(0.0, abs=1e-9)


# ── input dtype ───────────────────────────────────────────────────────────────


def test_float_and_uint8_inputs_agree():
    arr_u8 = np.full((4, 4, 3), 128, dtype=np.uint8)
    arr_f32 = arr_u8.astype(np.float32)
    m_u8 = image_compare(arr_u8, arr_u8)
    m_f32 = image_compare(arr_f32, arr_f32)
    assert m_u8["overall_dE"] == pytest.approx(m_f32["overall_dE"], abs=1e-6)
    assert m_u8["neutral_leak"] == pytest.approx(m_f32["neutral_leak"], abs=1e-6)
