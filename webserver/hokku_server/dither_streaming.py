"""StreamingDither: memory-constrained error-diffusion dither.

Operates on a rolling 2–3 row window instead of a full-panel float32 buffer.
Peak memory for the dither step alone is ≤ 5 MB regardless of panel size.
The production path (dither_with_prep) further amortises per-stripe
adaptive-saturate + DRC preprocessing across 100-row batches, keeping the
combined transient well under the 50 MB per-render budget.

This module also owns the shared colour-space helpers and LUT builders that
other dither implementations reuse.  They are module-level functions so they
can be re-exported by the dither_constrained.py backward-compatibility shim.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import numpy as np
from numpy.typing import ArrayLike, NDArray
from PIL import Image

from hokku_server.display import PALETTE_MEASURED_RGB
from hokku_server.dither_abc import (
    _DEFAULT_STRIPE_H,
    AbstractDither,
    CanvasLike,
    DiffusionKernel,
    FloatArray,
    PrepStripe,
    UInt8Array,
)
from hokku_server.dither_config import DitherConfig

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ── Colour space ────────────────────────────────────────────────────────────


def srgb_to_linear(c: ArrayLike, *, dtype: Any = np.float64) -> FloatArray:
    arr = np.asarray(c, dtype=dtype) / dtype(255.0)
    return np.where(
        arr <= dtype(0.04045),
        arr / dtype(12.92),
        ((arr + dtype(0.055)) / dtype(1.055)) ** dtype(2.4),
    )


def linear_to_xyz(rgb: ArrayLike, *, dtype: Any = np.float64) -> FloatArray:
    rgb = np.asarray(rgb, dtype=dtype)
    M = np.array(
        [
            [0.4124564, 0.3575761, 0.1804375],
            [0.2126729, 0.7151522, 0.0721750],
            [0.0193339, 0.1191920, 0.9503041],
        ],
        dtype=dtype,
    )
    return rgb @ M.T


def xyz_to_lab(xyz: ArrayLike, *, dtype: Any = np.float64) -> FloatArray:
    xyz = np.asarray(xyz, dtype=dtype)
    ref = np.array([0.95047, 1.00000, 1.08883], dtype=dtype)
    scaled = xyz / ref
    f = np.where(
        scaled > dtype(0.008856), scaled ** dtype(1 / 3), dtype(7.787) * scaled + dtype(16 / 116)
    )
    L = dtype(116) * f[..., 1] - dtype(16)
    a = dtype(500) * (f[..., 0] - f[..., 1])
    b = dtype(200) * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], axis=-1)


def rgb_to_lab(rgb: ArrayLike, *, dtype: Any = np.float64) -> FloatArray:
    arr = np.clip(np.asarray(rgb, dtype=dtype), 0, 255)
    return xyz_to_lab(linear_to_xyz(srgb_to_linear(arr, dtype=dtype), dtype=dtype), dtype=dtype)


PALETTE_LAB = rgb_to_lab(PALETTE_MEASURED_RGB)


def linear_rgb_to_oklab(rgb: ArrayLike, *, dtype: Any = np.float64) -> FloatArray:
    """Linear RGB [0,1] → OKLAB. https://bottosson.github.io/posts/oklab/"""
    rgb = np.asarray(rgb, dtype=dtype)
    M1 = np.array(
        [
            [0.4122214708, 0.5363325363, 0.0514459929],
            [0.2119034982, 0.6806995451, 0.1073969566],
            [0.0883024619, 0.2817188376, 0.6299787005],
        ],
        dtype=dtype,
    )
    lms = np.cbrt(rgb @ M1.T)
    M2 = np.array(
        [
            [0.2104542553, 0.7936177850, -0.0040720468],
            [1.9779984951, -2.4285922050, 0.4505937099],
            [0.0259040371, 0.7827717662, -0.8086757660],
        ],
        dtype=dtype,
    )
    return lms @ M2.T


def rgb_to_oklab(rgb: ArrayLike, *, dtype: Any = np.float64) -> FloatArray:
    arr = np.clip(np.asarray(rgb, dtype=dtype), 0, 255)
    return linear_rgb_to_oklab(srgb_to_linear(arr, dtype=dtype), dtype=dtype)


PALETTE_OKLAB = rgb_to_oklab(PALETTE_MEASURED_RGB)

# OKLAB chroma below which a palette entry is treated as neutral (no hue).
# Black≈0.000, White≈0.014 on our panel; coloured inks are 0.08–0.19.
_OKLAB_NEUTRAL_CHROMA = 0.05


def oklab_to_linear_rgb(oklab: ArrayLike, *, dtype: Any = np.float64) -> FloatArray:
    """OKLAB → linear RGB [0,1]. https://bottosson.github.io/posts/oklab/"""
    oklab = np.asarray(oklab, dtype=dtype)
    M2_inv = np.array(
        [
            [1.0, 0.3963377774, 0.2158037573],
            [1.0, -0.1055613458, -0.0638541728],
            [1.0, -0.0894841775, -1.2914855480],
        ],
        dtype=dtype,
    )
    lms_ = oklab @ M2_inv.T
    lms = lms_ * lms_ * lms_
    M1_inv = np.array(
        [
            [4.0767416621, -3.3077115913, 0.2309699292],
            [-1.2684380046, 2.6097574011, -0.3413193965],
            [-0.0041960863, -0.7034186147, 1.7076147010],
        ],
        dtype=dtype,
    )
    return lms @ M1_inv.T


def oklab_to_rgb(oklab: ArrayLike, *, dtype: Any = np.float64) -> FloatArray:
    """OKLAB → sRGB [0, 255] (uint8-range float). Inverse of ``rgb_to_oklab``."""
    linear = np.clip(oklab_to_linear_rgb(oklab, dtype=dtype), dtype(0), dtype(1))
    srgb = np.where(
        linear <= dtype(0.0031308),
        linear * dtype(12.92),
        dtype(1.055) * (linear ** dtype(1.0 / 2.4)) - dtype(0.055),
    )
    return np.clip(srgb * dtype(255), dtype(0), dtype(255))


def adaptive_saturate(
    img_array: ArrayLike,
    max_enhance: float,
    low_thresh: float,
    high_thresh: float,
) -> FloatArray:
    """Boost saturation only on already-colourful pixels (chroma > low_thresh).

    Below low_thresh the factor is 1.0 (no change); above high_thresh it's
    ``max_enhance``; linearly ramped between.  Operates in float32 throughout.
    """
    f32 = np.float32
    rgb = np.asarray(img_array, dtype=f32)
    lab = rgb_to_lab(rgb, dtype=f32)
    chroma = np.sqrt(lab[..., 1] ** 2 + lab[..., 2] ** 2)
    t = np.clip((chroma - f32(low_thresh)) / f32(high_thresh - low_thresh), f32(0.0), f32(1.0))
    factor = f32(1.0) + f32(max_enhance - 1.0) * t
    lab[..., 1] *= factor
    lab[..., 2] *= factor

    ref = np.array([0.95047, 1.00000, 1.08883], dtype=f32)
    L = lab[..., 0]
    a = lab[..., 1]
    b = lab[..., 2]
    fy = (L + f32(16)) / f32(116)
    fx = a / f32(500) + fy
    fz = fy - b / f32(200)
    eps = f32(0.008856)
    kappa = f32(903.3)
    xyz_out = np.empty_like(lab)
    fx3 = fx**3
    fz3 = fz**3
    xyz_out[..., 0] = np.where(fx3 > eps, fx3, (f32(116) * fx - f32(16)) / kappa) * ref[0]
    xyz_out[..., 1] = np.where(L > kappa * eps, ((L + f32(16)) / f32(116)) ** 3, L / kappa) * ref[1]
    xyz_out[..., 2] = np.where(fz3 > eps, fz3, (f32(116) * fz - f32(16)) / kappa) * ref[2]
    M_inv = np.array(
        [
            [3.2404542, -1.5371385, -0.4985314],
            [-0.9692660, 1.8760108, 0.0415560],
            [0.0556434, -0.2040259, 1.0572252],
        ],
        dtype=f32,
    )
    linear = np.clip(xyz_out @ M_inv.T, f32(0), f32(1))
    srgb = np.where(
        linear <= f32(0.0031308),
        linear * f32(12.92),
        f32(1.055) * (linear ** f32(1.0 / 2.4)) - f32(0.055),
    )
    return np.clip(srgb * f32(255), f32(0), f32(255))


def adaptive_saturate_oklab(
    img_array: ArrayLike,
    max_enhance: float,
    low_thresh: float,
    high_thresh: float,
) -> FloatArray:
    """OKLAB-space version of ``adaptive_saturate``.

    Boosts ``a``/``b`` (chroma) only on already-colourful pixels — same shape
    as the CIELAB variant, but in OKLAB the hue stays much more stable under
    the boost (Bottosson 2020).  ``low_thresh``/``high_thresh`` are in OKLAB
    chroma units (panel inks land between 0.094 and 0.176; white ≈ 0.011).
    """
    f32 = np.float32
    rgb = np.asarray(img_array, dtype=f32)
    oklab = rgb_to_oklab(rgb, dtype=f32)
    chroma = np.sqrt(oklab[..., 1] ** 2 + oklab[..., 2] ** 2)
    span = f32(high_thresh - low_thresh) if high_thresh > low_thresh else f32(1e-6)
    t = np.clip((chroma - f32(low_thresh)) / span, f32(0.0), f32(1.0))
    factor = f32(1.0) + f32(max_enhance - 1.0) * t
    oklab[..., 1] *= factor
    oklab[..., 2] *= factor
    return oklab_to_rgb(oklab, dtype=f32)


# ── LUTs ────────────────────────────────────────────────────────────────────


def build_rgb_lut() -> tuple[UInt8Array, float]:
    """32³ RGB grid → palette index by Euclidean CIELAB distance."""
    steps = 32
    scale = 256 / steps
    vals = np.arange(steps) * scale + scale / 2
    rr, gg, bb = np.meshgrid(vals, vals, vals, indexing="ij")
    rgb_grid = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)
    lab_grid = rgb_to_lab(rgb_grid)
    dists = np.sum((lab_grid[:, None, :] - PALETTE_LAB[None, :, :]) ** 2, axis=2)
    lut = np.argmin(dists, axis=1).astype(np.uint8).reshape(steps, steps, steps)
    return lut, scale


def build_rgb_lut_weighted() -> tuple[UInt8Array, float]:
    """32³ RGB grid → palette index by weighted CIELAB distance (2L² + a² + b²).

    The 2× L* weight reflects that a 1-unit lightness step is perceptually
    larger than a 1-unit chroma step in CIELAB.
    """
    steps = 32
    scale = 256 / steps
    vals = np.arange(steps) * scale + scale / 2
    rr, gg, bb = np.meshgrid(vals, vals, vals, indexing="ij")
    rgb_grid = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)
    lab_grid = rgb_to_lab(rgb_grid)
    diff = lab_grid[:, None, :] - PALETTE_LAB[None, :, :]
    dists = 2.0 * diff[..., 0] ** 2 + diff[..., 1] ** 2 + diff[..., 2] ** 2
    lut = np.argmin(dists, axis=1).astype(np.uint8).reshape(steps, steps, steps)
    return lut, scale


def build_rgb_lut_hue_aware(
    hue_cutoff_deg: float,
    neutral_chroma: float,
) -> tuple[UInt8Array, float]:
    """Like build_rgb_lut, but forbids hue-distant colour palette entries."""
    steps = 32
    scale = 256 / steps
    vals = np.arange(steps) * scale + scale / 2
    rr, gg, bb = np.meshgrid(vals, vals, vals, indexing="ij")
    rgb_grid = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)
    lab_grid = rgb_to_lab(rgb_grid)

    pal_a = PALETTE_LAB[:, 1]
    pal_b = PALETTE_LAB[:, 2]
    pal_chroma = np.sqrt(pal_a**2 + pal_b**2)
    pal_hue = np.arctan2(pal_b, pal_a)
    neutral_pal = pal_chroma < neutral_chroma

    pix_a = lab_grid[:, 1]
    pix_b = lab_grid[:, 2]
    pix_chroma = np.sqrt(pix_a**2 + pix_b**2)
    pix_hue = np.arctan2(pix_b, pix_a)
    dh = pix_hue[:, None] - pal_hue[None, :]
    dh = np.arctan2(np.sin(dh), np.cos(dh))
    dh_deg = np.abs(np.degrees(dh))

    forbidden = (
        (pix_chroma[:, None] > neutral_chroma) & (~neutral_pal[None, :]) & (dh_deg > hue_cutoff_deg)
    )
    dists = np.sum((lab_grid[:, None, :] - PALETTE_LAB[None, :, :]) ** 2, axis=2)
    dists = np.where(forbidden, np.inf, dists)
    lut = np.argmin(dists, axis=1).astype(np.uint8).reshape(steps, steps, steps)
    return lut, scale


def build_rgb_lut_hue_aware_weighted(
    hue_cutoff_deg: float,
    neutral_chroma: float,
) -> tuple[UInt8Array, float]:
    """Like build_rgb_lut_hue_aware but uses weighted CIELAB distance (2L² + a² + b²)."""
    steps = 32
    scale = 256 / steps
    vals = np.arange(steps) * scale + scale / 2
    rr, gg, bb = np.meshgrid(vals, vals, vals, indexing="ij")
    rgb_grid = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)
    lab_grid = rgb_to_lab(rgb_grid)

    pal_a = PALETTE_LAB[:, 1]
    pal_b = PALETTE_LAB[:, 2]
    pal_chroma = np.sqrt(pal_a**2 + pal_b**2)
    pal_hue = np.arctan2(pal_b, pal_a)
    neutral_pal = pal_chroma < neutral_chroma

    pix_a = lab_grid[:, 1]
    pix_b = lab_grid[:, 2]
    pix_chroma = np.sqrt(pix_a**2 + pix_b**2)
    pix_hue = np.arctan2(pix_b, pix_a)
    dh = pix_hue[:, None] - pal_hue[None, :]
    dh = np.arctan2(np.sin(dh), np.cos(dh))
    dh_deg = np.abs(np.degrees(dh))

    forbidden = (
        (pix_chroma[:, None] > neutral_chroma) & (~neutral_pal[None, :]) & (dh_deg > hue_cutoff_deg)
    )
    diff = lab_grid[:, None, :] - PALETTE_LAB[None, :, :]
    dists = 2.0 * diff[..., 0] ** 2 + diff[..., 1] ** 2 + diff[..., 2] ** 2
    dists = np.where(forbidden, np.inf, dists)
    lut = np.argmin(dists, axis=1).astype(np.uint8).reshape(steps, steps, steps)
    return lut, scale


def build_rgb_lut_bw() -> tuple[UInt8Array, float]:
    """32³ RGB grid → palette index using ONLY black (0) and white (1) entries.

    Prevents B&W dithering from using colored palette entries, which would
    create artifacts like red/pink tints in grayscale images.
    """
    steps = 32
    scale = 256 / steps
    vals = np.arange(steps) * scale + scale / 2
    rr, gg, bb = np.meshgrid(vals, vals, vals, indexing="ij")
    rgb_grid = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)
    lab_grid = rgb_to_lab(rgb_grid)

    # Only use black (0) and white (1) palette entries
    bw_palette = PALETTE_LAB[[0, 1]]
    dists = np.sum((lab_grid[:, None, :] - bw_palette[None, :, :]) ** 2, axis=2)
    # Map 0→0 (black), 1→1 (white) in the results
    lut_indices = np.argmin(dists, axis=1).astype(np.uint8)
    lut = lut_indices.reshape(steps, steps, steps)
    return lut, scale


def build_rgb_lut_oklab() -> tuple[UInt8Array, float]:
    """32³ RGB grid → palette index by Euclidean OKLAB distance."""
    steps = 32
    scale = 256 / steps
    vals = np.arange(steps) * scale + scale / 2
    rr, gg, bb = np.meshgrid(vals, vals, vals, indexing="ij")
    rgb_grid = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)
    oklab_grid = rgb_to_oklab(rgb_grid)
    dists = np.sum((oklab_grid[:, None, :] - PALETTE_OKLAB[None, :, :]) ** 2, axis=2)
    lut = np.argmin(dists, axis=1).astype(np.uint8).reshape(steps, steps, steps)
    return lut, scale


def build_rgb_lut_oklab_hue_aware(hue_cutoff_deg: float) -> tuple[UInt8Array, float]:
    """Like build_rgb_lut_oklab but forbids hue-distant colour palette entries.

    Hue and chroma are measured in OKLAB space.  Neutral palette entries
    (OKLAB chroma < _OKLAB_NEUTRAL_CHROMA) are always allowed.
    """
    steps = 32
    scale = 256 / steps
    vals = np.arange(steps) * scale + scale / 2
    rr, gg, bb = np.meshgrid(vals, vals, vals, indexing="ij")
    rgb_grid = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)
    oklab_grid = rgb_to_oklab(rgb_grid)

    pal_a = PALETTE_OKLAB[:, 1]
    pal_b = PALETTE_OKLAB[:, 2]
    pal_chroma = np.sqrt(pal_a**2 + pal_b**2)
    pal_hue = np.arctan2(pal_b, pal_a)
    neutral_pal = pal_chroma < _OKLAB_NEUTRAL_CHROMA

    pix_a = oklab_grid[:, 1]
    pix_b = oklab_grid[:, 2]
    pix_chroma = np.sqrt(pix_a**2 + pix_b**2)
    pix_hue = np.arctan2(pix_b, pix_a)
    dh = pix_hue[:, None] - pal_hue[None, :]
    dh = np.arctan2(np.sin(dh), np.cos(dh))
    dh_deg = np.abs(np.degrees(dh))

    forbidden = (
        (pix_chroma[:, None] > _OKLAB_NEUTRAL_CHROMA)
        & (~neutral_pal[None, :])
        & (dh_deg > hue_cutoff_deg)
    )
    dists = np.sum((oklab_grid[:, None, :] - PALETTE_OKLAB[None, :, :]) ** 2, axis=2)
    dists = np.where(forbidden, np.inf, dists)
    lut = np.argmin(dists, axis=1).astype(np.uint8).reshape(steps, steps, steps)
    return lut, scale


# ── CAM16-UCS LUTs ─────────────────────────────────────────────────────────
# CAM16-UCS is the CIE "gold standard" perceptual colour space (Li et al. 2017).
# It produces the most perceptually-uniform palette match available, but the
# colour-science library that implements it has a ~6-second import time and
# ~100 ms per-stripe conversion overhead.  We use it ONLY for one-shot LUT
# building (amortised to zero via @lru_cache); per-stripe operations (saturate,
# DRC) stay on CIELAB or OKLAB.


def _rgb_to_cam16ucs(rgb: ArrayLike) -> FloatArray:
    """sRGB [0,255] → CAM16-UCS J', a', b'.  Lazy-imports colour-science."""
    import colour  # noqa: PLC0415 — lazy: 6s import cost paid only on first CAM16 LUT build

    arr = np.clip(np.asarray(rgb, dtype=np.float64), 0, 255)
    xyz = linear_to_xyz(srgb_to_linear(arr))
    return colour.XYZ_to_CAM16UCS(xyz)


def build_rgb_lut_cam16ucs() -> tuple[UInt8Array, float]:
    """32³ RGB grid → palette index by Euclidean CAM16-UCS distance."""
    steps = 32
    scale = 256 / steps
    vals = np.arange(steps) * scale + scale / 2
    rr, gg, bb = np.meshgrid(vals, vals, vals, indexing="ij")
    rgb_grid = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)
    pal_jab = _rgb_to_cam16ucs(PALETTE_MEASURED_RGB)
    jab_grid = _rgb_to_cam16ucs(rgb_grid)
    dists = np.sum((jab_grid[:, None, :] - pal_jab[None, :, :]) ** 2, axis=2)
    lut = np.argmin(dists, axis=1).astype(np.uint8).reshape(steps, steps, steps)
    return lut, scale


def build_rgb_lut_cam16ucs_hue_aware(
    hue_cutoff_deg: float,
    neutral_chroma: float,
) -> tuple[UInt8Array, float]:
    """Like build_rgb_lut_cam16ucs but forbids hue-distant colour palette entries.

    Hue and chroma are derived from the CAM16-UCS a', b' axes.  ``neutral_chroma``
    is in CAM16-UCS chroma units — comparable in scale to CIELAB (panel inks
    land roughly 20–60, panel-white near 4), so the existing default 8.0 works.
    """
    steps = 32
    scale = 256 / steps
    vals = np.arange(steps) * scale + scale / 2
    rr, gg, bb = np.meshgrid(vals, vals, vals, indexing="ij")
    rgb_grid = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)
    pal_jab = _rgb_to_cam16ucs(PALETTE_MEASURED_RGB)
    jab_grid = _rgb_to_cam16ucs(rgb_grid)

    pal_a = pal_jab[:, 1]
    pal_b = pal_jab[:, 2]
    pal_chroma = np.sqrt(pal_a**2 + pal_b**2)
    pal_hue = np.arctan2(pal_b, pal_a)
    neutral_pal = pal_chroma < neutral_chroma

    pix_a = jab_grid[:, 1]
    pix_b = jab_grid[:, 2]
    pix_chroma = np.sqrt(pix_a**2 + pix_b**2)
    pix_hue = np.arctan2(pix_b, pix_a)
    dh = pix_hue[:, None] - pal_hue[None, :]
    dh = np.arctan2(np.sin(dh), np.cos(dh))
    dh_deg = np.abs(np.degrees(dh))

    forbidden = (
        (pix_chroma[:, None] > neutral_chroma) & (~neutral_pal[None, :]) & (dh_deg > hue_cutoff_deg)
    )
    dists = np.sum((jab_grid[:, None, :] - pal_jab[None, :, :]) ** 2, axis=2)
    dists = np.where(forbidden, np.inf, dists)
    lut = np.argmin(dists, axis=1).astype(np.uint8).reshape(steps, steps, steps)
    return lut, scale


@lru_cache(maxsize=1)
def _cached_euclidean_lut() -> tuple[UInt8Array, float]:
    return build_rgb_lut()


@lru_cache(maxsize=1)
def _cached_euclidean_weighted_lut() -> tuple[UInt8Array, float]:
    return build_rgb_lut_weighted()


@lru_cache(maxsize=16)
def _cached_hue_aware_lut(hue_cutoff_deg: float, neutral_chroma: float) -> tuple[UInt8Array, float]:
    return build_rgb_lut_hue_aware(hue_cutoff_deg, neutral_chroma)


@lru_cache(maxsize=16)
def _cached_hue_aware_weighted_lut(
    hue_cutoff_deg: float, neutral_chroma: float
) -> tuple[UInt8Array, float]:
    return build_rgb_lut_hue_aware_weighted(hue_cutoff_deg, neutral_chroma)


@lru_cache(maxsize=1)
def _cached_bw_lut() -> tuple[UInt8Array, float]:
    return build_rgb_lut_bw()


@lru_cache(maxsize=1)
def _cached_oklab_lut() -> tuple[UInt8Array, float]:
    return build_rgb_lut_oklab()


@lru_cache(maxsize=16)
def _cached_oklab_hue_aware_lut(hue_cutoff_deg: float) -> tuple[UInt8Array, float]:
    return build_rgb_lut_oklab_hue_aware(hue_cutoff_deg)


@lru_cache(maxsize=1)
def _cached_cam16ucs_lut() -> tuple[UInt8Array, float]:
    return build_rgb_lut_cam16ucs()


@lru_cache(maxsize=16)
def _cached_cam16ucs_hue_aware_lut(
    hue_cutoff_deg: float, neutral_chroma: float
) -> tuple[UInt8Array, float]:
    return build_rgb_lut_cam16ucs_hue_aware(hue_cutoff_deg, neutral_chroma)


def lut_and_scale_for_dither_config(cfg: DitherConfig) -> tuple[NDArray[np.uint8], float]:
    if cfg.lut_name == "euclidean":
        return _cached_euclidean_lut()
    if cfg.lut_name == "euclidean_weighted":
        return _cached_euclidean_weighted_lut()
    if cfg.lut_name == "bw":
        return _cached_bw_lut()
    if cfg.lut_name == "oklab":
        return _cached_oklab_lut()
    if cfg.lut_name == "oklab_hue_aware":
        return _cached_oklab_hue_aware_lut(cfg.hue_cutoff_deg)
    if cfg.lut_name == "cam16ucs":
        return _cached_cam16ucs_lut()
    if cfg.lut_name == "cam16ucs_hue_aware":
        return _cached_cam16ucs_hue_aware_lut(cfg.hue_cutoff_deg, cfg.neutral_chroma)
    if cfg.lut_name == "hue_aware_weighted":
        return _cached_hue_aware_weighted_lut(cfg.hue_cutoff_deg, cfg.neutral_chroma)
    return _cached_hue_aware_lut(cfg.hue_cutoff_deg, cfg.neutral_chroma)


# ── Algorithms ───────────────────────────────────────────────────────────────

_FS_KERNEL: DiffusionKernel = (
    (1, 0, 7.0 / 16.0),
    (-1, 1, 3.0 / 16.0),
    (0, 1, 5.0 / 16.0),
    (1, 1, 1.0 / 16.0),
)

_ATKINSON_KERNEL: DiffusionKernel = (
    (1, 0, 1.0 / 8.0),
    (2, 0, 1.0 / 8.0),
    (-1, 1, 1.0 / 8.0),
    (0, 1, 1.0 / 8.0),
    (1, 1, 1.0 / 8.0),
    (0, 2, 1.0 / 8.0),
)

_STUCKI_KERNEL: DiffusionKernel = (
    (1, 0, 8 / 42.0),
    (2, 0, 4 / 42.0),
    (-2, 1, 2 / 42.0),
    (-1, 1, 4 / 42.0),
    (0, 1, 8 / 42.0),
    (1, 1, 4 / 42.0),
    (2, 1, 2 / 42.0),
    (-2, 2, 1 / 42.0),
    (-1, 2, 2 / 42.0),
    (0, 2, 4 / 42.0),
    (1, 2, 2 / 42.0),
    (2, 2, 1 / 42.0),
)

_KERNEL_FOR: dict[str, DiffusionKernel] = {
    "floyd_steinberg": _FS_KERNEL,
    "atkinson": _ATKINSON_KERNEL,
    "stucki": _STUCKI_KERNEL,
}


def noop_dither(
    canvas: CanvasLike,
    lut: NDArray[np.uint8],
    lut_scale: float,
    serpentine: bool,
) -> UInt8Array:
    """Nearest-palette quantize per pixel — no error diffusion. Fast test path."""
    del serpentine
    pixels = np.clip(np.asarray(canvas, dtype=np.float32), 0, 255)
    n = lut.shape[0]
    lut_max = n - 1
    idx = np.minimum((pixels / lut_scale).astype(np.int32), lut_max)
    return lut[idx[..., 0], idx[..., 1], idx[..., 2]].astype(np.uint8)


# ── Public class ─────────────────────────────────────────────────────────────


class StreamingDither(AbstractDither):
    """Rolling-window error-diffusion dither — the default production strategy.

    Holds only ``max_dy + 1`` rows of float32 state (2 rows for FS, 3 for
    Atkinson/Stucki).  Combined with ``dither_with_prep``'s 100-row stripe
    cache the peak transient stays well under the 50 MB per-render budget.
    """

    def dither(self, canvas: CanvasLike, cfg: DitherConfig) -> UInt8Array:
        _validate(cfg)
        lut, scale = lut_and_scale_for_dither_config(cfg)
        if cfg.algorithm == "noop":
            return noop_dither(canvas, lut, scale, cfg.serpentine)
        return self._diffuse(canvas, _KERNEL_FOR[cfg.algorithm], lut, scale, cfg.serpentine)

    def dither_with_prep(
        self,
        canvas: CanvasLike,
        cfg: DitherConfig,
        prep_stripe: PrepStripe,
        stripe_h: int = _DEFAULT_STRIPE_H,
    ) -> UInt8Array:
        _validate(cfg)
        lut, scale = lut_and_scale_for_dither_config(cfg)
        if cfg.algorithm == "noop":
            return noop_dither(canvas, lut, scale, cfg.serpentine)
        return self._diffuse(
            canvas,
            _KERNEL_FOR[cfg.algorithm],
            lut,
            scale,
            cfg.serpentine,
            prep_stripe=prep_stripe,
            stripe_h=stripe_h,
        )

    @staticmethod
    def _diffuse(
        canvas: CanvasLike,
        kernel: DiffusionKernel,
        lut: NDArray[np.uint8],
        lut_scale: float,
        serpentine: bool,
        prep_stripe: PrepStripe | None = None,
        stripe_h: int = _DEFAULT_STRIPE_H,
    ) -> UInt8Array:
        """Streaming error-diffusion dither (rolling-window implementation).

        Holds only ``max_dy + 1`` rows of float32 working state plus one cached
        pre-processed stripe at a time.  The full-panel float32 buffer is never
        materialised.
        """
        if not (np.isfinite(lut_scale) and lut_scale > 0):
            raise ValueError(f"lut_scale must be positive and finite, got {lut_scale}")
        n = lut.shape[0]
        if lut.shape != (n, n, n):
            raise ValueError(f"lut must be a cube, got {lut.shape}")

        if isinstance(canvas, Image.Image):
            canvas = np.asarray(canvas)
        canvas_arr = np.asarray(canvas)
        if canvas_arr.ndim != 3 or canvas_arr.shape[2] != 3:
            raise ValueError(f"canvas must be H×W×3 RGB, got {canvas_arr.shape}")
        H, W = int(canvas_arr.shape[0]), int(canvas_arr.shape[1])

        use_prep = canvas_arr.dtype == np.uint8 and prep_stripe is not None

        pal_rgb = PALETTE_MEASURED_RGB
        lut_max = n - 1
        result_idx = np.empty((H, W), dtype=np.uint8)

        max_dy = max(dy for _, dy, _ in kernel)
        n_rows = max_dy + 1
        rolling = np.zeros((n_rows, W, 3), dtype=np.float32)

        stripe_y0 = -1
        stripe_data: NDArray[np.float32] | None = None

        def _row_pixels(y: int) -> NDArray[np.float32]:
            nonlocal stripe_y0, stripe_data
            if not use_prep:
                return canvas_arr[y].astype(np.float32, copy=True)
            sh = stripe_h
            new_y0 = (y // sh) * sh
            if new_y0 != stripe_y0:
                stripe_data = None
                y1 = min(new_y0 + sh, H)
                stripe_data = prep_stripe(canvas_arr[new_y0:y1])  # type: ignore[misc]
                stripe_y0 = new_y0
            assert stripe_data is not None  # type-narrowing guard; never stripped in practice
            return stripe_data[y - stripe_y0].astype(np.float32, copy=True)

        rolling[0] += _row_pixels(0)

        for y in range(H):
            reverse = serpentine and (y % 2 == 0)
            x_iter = range(W - 1, -1, -1) if reverse else range(W)

            for x in x_iter:
                r = min(max(rolling[0, x, 0], 0.0), 255.0)
                g = min(max(rolling[0, x, 1], 0.0), 255.0)
                b = min(max(rolling[0, x, 2], 0.0), 255.0)
                ri = min(int(r / lut_scale), lut_max)
                gi = min(int(g / lut_scale), lut_max)
                bi = min(int(b / lut_scale), lut_max)
                idx = int(lut[ri, gi, bi])
                result_idx[y, x] = idx
                er = r - pal_rgb[idx, 0]
                eg = g - pal_rgb[idx, 1]
                eb = b - pal_rgb[idx, 2]
                for dx, dy, wgt in kernel:
                    eff_dx = -dx if reverse else dx
                    nx = x + eff_dx
                    if 0 <= nx < W and y + dy < H:
                        rolling[dy, nx, 0] += er * wgt
                        rolling[dy, nx, 1] += eg * wgt
                        rolling[dy, nx, 2] += eb * wgt

            if y + 1 < H:
                for i in range(n_rows - 1):
                    rolling[i] = rolling[i + 1]
                rolling[n_rows - 1].fill(0)
                rolling[0] += _row_pixels(y + 1)

            if y % 200 == 0:
                logger.debug("Dithering (streaming): %d/%d", y, H)

        return result_idx


_run_streaming = StreamingDither._diffuse  # backward-compat alias


# ── Helpers ──────────────────────────────────────────────────────────────────


def _validate(cfg: DitherConfig) -> None:
    if cfg.algorithm not in _KERNEL_FOR and cfg.algorithm != "noop":
        raise ValueError(f"Unknown algorithm: {cfg.algorithm!r}")
    if cfg.lut_name not in (
        "euclidean",
        "euclidean_weighted",
        "hue_aware",
        "hue_aware_weighted",
        "bw",
        "oklab",
        "oklab_hue_aware",
        "cam16ucs",
        "cam16ucs_hue_aware",
    ):
        raise ValueError(f"Unknown lut_name: {cfg.lut_name!r}")


# ── Module-level free-function API (mirrors old dither_constrained interface) ─


def dither(canvas: CanvasLike, cfg: DitherConfig) -> UInt8Array:
    """Run the configured streaming dither.  Interchangeable with dither_unconstrained.dither."""
    return StreamingDither().dither(canvas, cfg)


def dither_with_prep(
    canvas: CanvasLike,
    cfg: DitherConfig,
    prep_stripe: PrepStripe,
    stripe_h: int = _DEFAULT_STRIPE_H,
) -> UInt8Array:
    """Streaming dither with per-stripe preprocessing (production entry-point)."""
    return StreamingDither().dither_with_prep(canvas, cfg, prep_stripe, stripe_h)
