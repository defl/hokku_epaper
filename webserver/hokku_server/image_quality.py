"""Image quality metrics: compare an original RGB image to a dithered/derived output."""
from __future__ import annotations

import numpy as np
from numpy.typing import ArrayLike, NDArray

from hokku_server.dither_streaming import PALETTE_LAB, rgb_to_lab

_BLUE_IDX = 4  # blue ink index in the Spectra 6 palette


# ── Colour helpers ────────────────────────────────────────────────────────────


def _chroma(lab: NDArray) -> NDArray:
    return np.sqrt(lab[..., 1] ** 2 + lab[..., 2] ** 2)


def _de76(lab1: NDArray, lab2: NDArray) -> NDArray:
    return np.sqrt(((lab1 - lab2) ** 2).sum(axis=-1))


def _de2000(lab1: NDArray, lab2: NDArray) -> NDArray:
    """CIE ΔE2000, vectorized over any leading dimensions."""
    L1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    L2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]

    C1ab = np.sqrt(a1 ** 2 + b1 ** 2)
    C2ab = np.sqrt(a2 ** 2 + b2 ** 2)
    Cab_avg7 = ((C1ab + C2ab) / 2.0) ** 7
    G = 0.5 * (1.0 - np.sqrt(Cab_avg7 / (Cab_avg7 + 25.0 ** 7)))

    a1p = a1 * (1.0 + G)
    a2p = a2 * (1.0 + G)
    C1p = np.sqrt(a1p ** 2 + b1 ** 2)
    C2p = np.sqrt(a2p ** 2 + b2 ** 2)

    h1p = np.degrees(np.arctan2(b1, a1p)) % 360.0
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360.0

    dLp = L2 - L1
    dCp = C2p - C1p
    C12p = C1p * C2p
    achromatic = C12p == 0.0
    abs_hdiff = np.abs(h2p - h1p)
    raw_dhp = h2p - h1p
    dhp = np.where(
        achromatic, 0.0,
        np.where(abs_hdiff <= 180.0, raw_dhp,
        np.where(raw_dhp > 180.0, raw_dhp - 360.0, raw_dhp + 360.0)),
    )
    dHp = 2.0 * np.sqrt(C12p) * np.sin(np.radians(dhp / 2.0))

    Lp_avg = (L1 + L2) / 2.0
    Cp_avg = (C1p + C2p) / 2.0
    hp_avg = np.where(
        achromatic, h1p + h2p,
        np.where(abs_hdiff <= 180.0, (h1p + h2p) / 2.0,
        np.where(h1p + h2p < 360.0, (h1p + h2p + 360.0) / 2.0,
                                      (h1p + h2p - 360.0) / 2.0)),
    )

    T = (1.0
         - 0.17 * np.cos(np.radians(hp_avg - 30.0))
         + 0.24 * np.cos(np.radians(2.0 * hp_avg))
         + 0.32 * np.cos(np.radians(3.0 * hp_avg + 6.0))
         - 0.20 * np.cos(np.radians(4.0 * hp_avg - 63.0)))

    SL = 1.0 + 0.015 * (Lp_avg - 50.0) ** 2 / np.sqrt(20.0 + (Lp_avg - 50.0) ** 2)
    SC = 1.0 + 0.045 * Cp_avg
    SH = 1.0 + 0.015 * Cp_avg * T

    dTheta = 30.0 * np.exp(-((hp_avg - 275.0) / 25.0) ** 2)
    Cp_avg7 = Cp_avg ** 7
    RC = 2.0 * np.sqrt(Cp_avg7 / (Cp_avg7 + 25.0 ** 7))
    RT = -np.sin(np.radians(2.0 * dTheta)) * RC

    return np.sqrt(np.maximum(0.0,
        (dLp / SL) ** 2
        + (dCp / SC) ** 2
        + (dHp / SH) ** 2
        + RT * (dCp / SC) * (dHp / SH)
    ))


def _nearest_palette_index(lab: NDArray) -> NDArray:
    """Per-pixel nearest palette entry by CIE76 distance. Iterates over palette to stay O(N)."""
    flat = lab.reshape(-1, 3).astype(np.float64)
    palette = PALETTE_LAB.astype(np.float64)
    best_dist = np.full(len(flat), np.inf, dtype=np.float64)
    best_idx = np.zeros(len(flat), dtype=np.uint8)
    for i, entry in enumerate(palette):
        d = ((flat - entry) ** 2).sum(axis=-1)
        closer = d < best_dist
        best_dist[closer] = d[closer]
        best_idx[closer] = i
    return best_idx.reshape(lab.shape[:-1])


def _high_freq_energy_ratio(error_L: NDArray, valid: NDArray) -> float:
    """Fraction of L* error energy in spatial frequencies above min(H,W)/4.

    Higher means error energy is concentrated at fine scales (blue-noise, less visible grain).
    """
    err = np.where(valid, error_L, 0.0)
    power = np.abs(np.fft.fftshift(np.fft.fft2(err))) ** 2
    H, W = err.shape
    cy, cx = H // 2, W // 2
    y, x = np.indices((H, W))
    r = np.sqrt((y - cy) ** 2 + (x - cx) ** 2)
    total = power.sum()
    return float(power[r >= min(H, W) / 4.0].sum() / total) if total > 0 else 0.0


# ── Public API ────────────────────────────────────────────────────────────────


def image_compare(
    original: ArrayLike,
    derived: ArrayLike,
    *,
    padding_mask: ArrayLike | None = None,
) -> dict[str, float]:
    """Compare original to derived; both H×W×3 uint8 or float RGB in 0–255 range.

    padding_mask: bool H×W, True for letterbox pixels to exclude from all metrics.

    Returned keys (lower is better unless noted):
      neutral_leak          — mean chroma of derived for source-neutral pixels (src C* < 10)
      sat_hit               — fraction of source-saturated pixels (src C* > 25) kept saturated
                              in derived (der C* > 15); higher is better
      overall_dE            — mean CIE76 ΔE
      overall_dE2000        — mean CIE ΔE2000
      neutral_blue_fraction — fraction of source-neutral pixels mapped to blue palette ink
      lightness_dE          — mean |ΔL*|
      chroma_dE             — mean |ΔC*|
      hue_error             — mean hue-angle error (°) on source-saturated pixels
      error_roughness       — std dev of per-pixel ΔE76 (uneven error = visible grain)
      high_freq_energy_ratio — fraction of L* error energy at high spatial freqs; higher=better
    """
    orig_lab = rgb_to_lab(np.asarray(original, dtype=np.float64))
    der_lab = rgb_to_lab(np.asarray(derived, dtype=np.float64))

    valid: NDArray[np.bool_] = (
        ~np.asarray(padding_mask, dtype=bool)
        if padding_mask is not None
        else np.ones(orig_lab.shape[:-1], dtype=bool)
    )

    orig_c = _chroma(orig_lab)
    der_c = _chroma(der_lab)
    neutral = valid & (orig_c < 10.0)
    saturated = valid & (orig_c > 25.0)

    neutral_leak = float(der_c[neutral].mean()) if neutral.any() else 0.0
    sat_hit = float((der_c[saturated] > 15.0).mean()) if saturated.any() else 0.0

    o_v = orig_lab[valid]
    d_v = der_lab[valid]
    de76_vals = _de76(o_v, d_v)
    overall_de = float(de76_vals.mean())
    error_roughness = float(de76_vals.std())
    overall_de2000 = float(_de2000(o_v, d_v).mean())
    lightness_de = float(np.abs(o_v[:, 0] - d_v[:, 0]).mean())
    chroma_de = float(np.abs(_chroma(o_v) - _chroma(d_v)).mean())

    if saturated.any():
        ho = np.arctan2(orig_lab[saturated, 2], orig_lab[saturated, 1])
        hd = np.arctan2(der_lab[saturated, 2], der_lab[saturated, 1])
        delta = np.abs(ho - hd)
        hue_error = float(np.degrees(np.minimum(delta, 2 * np.pi - delta).mean()))
    else:
        hue_error = 0.0

    nearest = _nearest_palette_index(der_lab)
    neutral_blue_fraction = (
        float((nearest[neutral] == _BLUE_IDX).mean()) if neutral.any() else 0.0
    )

    high_freq_ratio = _high_freq_energy_ratio(
        der_lab[..., 0] - orig_lab[..., 0], valid
    )

    return {
        "neutral_leak": neutral_leak,
        "sat_hit": sat_hit,
        "overall_dE": overall_de,
        "overall_dE2000": overall_de2000,
        "neutral_blue_fraction": neutral_blue_fraction,
        "lightness_dE": lightness_de,
        "chroma_dE": chroma_de,
        "hue_error": hue_error,
        "error_roughness": error_roughness,
        "high_freq_energy_ratio": high_freq_ratio,
    }
