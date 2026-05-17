"""Spectra 6 unconstrained (full-canvas, mutating) dither pipeline.

This module is a self-contained copy of the original pre-streaming
error-diffusion implementation — the code that existed before the
memory-constrained (streaming) path was introduced.  It intentionally
shares **no** code with ``dither_constrained``: no imports, no shared
helpers, no shared kernel constants.  A change to one module cannot
accidentally affect the other.

Public entry-point
------------------
dither(canvas, cfg) -> UInt8Array

    Interchangeable with ``dither_constrained.dither`` — same call
    signature, same return type.

    ``canvas`` must be an H×W×3 RGB array (PIL Image, uint8 numpy, or
    float32 numpy) that has already been through adaptive-saturate + DRC.
    The array is copied internally before mutation so the caller's buffer
    is not modified.

Algorithm differences from the streaming variants
-------------------------------------------------
* Operates on a full-panel float32 canvas.  No stripe cache, no rolling
  error buffer.  Memory peak during dither alone is ~60 MB on a 3200×1600
  panel.
* The pixel buffer is *mutated* as errors propagate.
* No ``prep_stripe`` callback.

The math is bit-equivalent to the streaming versions — same kernels,
same coefficients, same serpentine behaviour, same float32 precision,
same row-then-column iteration order.

Intended uses:
  * ``test_dither_quality.py`` — side-by-side ``__streaming`` vs
    ``__unconstrained`` output PNGs so a human can compare visually.
  * Regression baseline: if the streaming implementation ever diverges
    from the reference algorithm, the unconstrained output is the
    ground truth.
"""
from __future__ import annotations

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)
from typing import Any, TypeAlias

import numpy as np
from numpy.typing import ArrayLike, NDArray
from PIL import Image

from hokku_server.display import PALETTE_MEASURED_RGB
from hokku_server.dither_abc import AbstractDither, CanvasLike, UInt8Array  # noqa: F401
from hokku_server.dither_config import DitherConfig

UInt8Array: TypeAlias = NDArray[np.uint8]
FloatArray: TypeAlias = NDArray[Any]
CanvasLike: TypeAlias = "Image.Image | ArrayLike"
_DiffusionKernel = tuple[tuple[int, int, float], ...]


# ── Color space (self-contained copy) ─────────────────────────────

def _srgb_to_linear(c: ArrayLike) -> FloatArray:
    c = np.asarray(c, dtype=np.float64) / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _linear_to_xyz(rgb: ArrayLike) -> FloatArray:
    rgb = np.asarray(rgb, dtype=np.float64)
    M = np.array([
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ])
    return rgb @ M.T


def _xyz_to_lab(xyz: ArrayLike) -> FloatArray:
    ref = np.array([0.95047, 1.00000, 1.08883])
    xyz = xyz / ref
    f = np.where(xyz > 0.008856, xyz ** (1 / 3), 7.787 * xyz + 16 / 116)
    L = 116 * f[..., 1] - 16
    a = 500 * (f[..., 0] - f[..., 1])
    b = 200 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], axis=-1)


def _rgb_to_lab(rgb: ArrayLike) -> FloatArray:
    linear = _srgb_to_linear(np.clip(np.asarray(rgb, dtype=np.float64), 0, 255))
    return _xyz_to_lab(_linear_to_xyz(linear))


_PALETTE_LAB = _rgb_to_lab(PALETTE_MEASURED_RGB)


# ── LUTs (self-contained copy) ─────────────────────────────────────

def _build_rgb_lut() -> tuple[UInt8Array, float]:
    """32³ RGB grid → palette index by Euclidean Lab distance."""
    steps = 32
    scale = 256 / steps
    vals = np.arange(steps) * scale + scale / 2
    rr, gg, bb = np.meshgrid(vals, vals, vals, indexing="ij")
    rgb_grid = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)
    lab_grid = _rgb_to_lab(rgb_grid)
    dists = np.sum(
        (lab_grid[:, None, :] - _PALETTE_LAB[None, :, :]) ** 2, axis=2
    )
    lut = np.argmin(dists, axis=1).astype(np.uint8).reshape(steps, steps, steps)
    return lut, scale


def _build_rgb_lut_hue_aware(
    hue_cutoff_deg: float,
    neutral_chroma: float,
) -> tuple[UInt8Array, float]:
    """32³ RGB grid → palette index, excluding hue-distant palette entries."""
    steps = 32
    scale = 256 / steps
    vals = np.arange(steps) * scale + scale / 2
    rr, gg, bb = np.meshgrid(vals, vals, vals, indexing="ij")
    rgb_grid = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)
    lab_grid = _rgb_to_lab(rgb_grid)

    pal_a = _PALETTE_LAB[:, 1]
    pal_b = _PALETTE_LAB[:, 2]
    pal_chroma = np.sqrt(pal_a ** 2 + pal_b ** 2)
    pal_hue = np.arctan2(pal_b, pal_a)
    neutral_pal = pal_chroma < neutral_chroma

    pix_a = lab_grid[:, 1]
    pix_b = lab_grid[:, 2]
    pix_chroma = np.sqrt(pix_a ** 2 + pix_b ** 2)
    pix_hue = np.arctan2(pix_b, pix_a)
    dh = pix_hue[:, None] - pal_hue[None, :]
    dh = np.arctan2(np.sin(dh), np.cos(dh))
    dh_deg = np.abs(np.degrees(dh))

    forbidden = (
        (pix_chroma[:, None] > neutral_chroma)
        & (~neutral_pal[None, :])
        & (dh_deg > hue_cutoff_deg)
    )
    dists = np.sum(
        (lab_grid[:, None, :] - _PALETTE_LAB[None, :, :]) ** 2, axis=2
    )
    dists = np.where(forbidden, np.inf, dists)
    lut = np.argmin(dists, axis=1).astype(np.uint8).reshape(steps, steps, steps)
    return lut, scale


def _build_rgb_lut_bw() -> tuple[UInt8Array, float]:
    """32³ RGB grid → palette index using ONLY black (0) and white (1) entries.

    Prevents B&W dithering from using colored palette entries.
    """
    steps = 32
    scale = 256 / steps
    vals = np.arange(steps) * scale + scale / 2
    rr, gg, bb = np.meshgrid(vals, vals, vals, indexing="ij")
    rgb_grid = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)
    lab_grid = _rgb_to_lab(rgb_grid)

    bw_palette = _PALETTE_LAB[[0, 1]]
    dists = np.sum((lab_grid[:, None, :] - bw_palette[None, :, :]) ** 2, axis=2)
    lut_indices = np.argmin(dists, axis=1).astype(np.uint8)
    lut = lut_indices.reshape(steps, steps, steps)
    return lut, scale


@lru_cache(maxsize=1)
def _cached_euclidean_lut() -> tuple[UInt8Array, float]:
    return _build_rgb_lut()


@lru_cache(maxsize=16)
def _cached_hue_aware_lut(
    hue_cutoff_deg: float, neutral_chroma: float
) -> tuple[UInt8Array, float]:
    return _build_rgb_lut_hue_aware(hue_cutoff_deg, neutral_chroma)


@lru_cache(maxsize=1)
def _cached_bw_lut() -> tuple[UInt8Array, float]:
    return _build_rgb_lut_bw()


def _lut_and_scale(cfg: DitherConfig) -> tuple[NDArray[np.uint8], float]:
    if cfg.lut_name == "euclidean":
        return _cached_euclidean_lut()
    if cfg.lut_name == "bw":
        return _cached_bw_lut()
    return _cached_hue_aware_lut(cfg.hue_cutoff_deg, cfg.neutral_chroma)


# ── Diffusion kernels ──────────────────────────────────────────────

_FS_KERNEL: _DiffusionKernel = (
    (1, 0, 7 / 16.0),
    (-1, 1, 3 / 16.0),
    (0, 1, 5 / 16.0),
    (1, 1, 1 / 16.0),
)

_ATKINSON_KERNEL: _DiffusionKernel = (
    (1, 0, 1 / 8.0),
    (2, 0, 1 / 8.0),
    (-1, 1, 1 / 8.0),
    (0, 1, 1 / 8.0),
    (1, 1, 1 / 8.0),
    (0, 2, 1 / 8.0),
)

_STUCKI_KERNEL: _DiffusionKernel = (
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


# ── Algorithms ──────────────────────────────────────────────────────

def _canvas_to_float32(canvas: CanvasLike) -> tuple[NDArray[np.float32], int, int]:
    """Convert canvas to a writable float32 H×W×3 array."""
    if isinstance(canvas, Image.Image):
        canvas = np.asarray(canvas)
    pixels = np.asarray(canvas, dtype=np.float32)
    if pixels.ndim != 3 or pixels.shape[2] != 3:
        raise ValueError(f"canvas must be H×W×3 RGB, got {pixels.shape}")
    # Always copy: error diffusion mutates pixels in place, and the caller
    # may not want their canvas trampled.
    pixels = pixels.copy()
    h, w = int(pixels.shape[0]), int(pixels.shape[1])
    return pixels, h, w


def _full_canvas_diffusion(
    canvas: CanvasLike,
    kernel: _DiffusionKernel,
    lut: NDArray[np.uint8],
    lut_scale: float,
    serpentine: bool,
) -> UInt8Array:
    """Full-canvas error-diffusion dither.

    Mutates a copy of the canvas float32 buffer in place as errors propagate.
    Memory peak ≈ 60 MB for a 3200×1600 panel (the full-panel float32).
    """
    if not (np.isfinite(lut_scale) and lut_scale > 0):
        raise ValueError(f"lut_scale must be positive and finite, got {lut_scale}")
    n = lut.shape[0]
    if lut.shape != (n, n, n):
        raise ValueError(f"lut must be a cube, got {lut.shape}")

    pixels, h, w = _canvas_to_float32(canvas)
    result_idx = np.zeros((h, w), dtype=np.uint8)
    pal_rgb = PALETTE_MEASURED_RGB
    lut_max = n - 1

    for y in range(h):
        reverse = serpentine and (y % 2 == 0)
        x_iter = range(w - 1, -1, -1) if reverse else range(w)
        for x in x_iter:
            r = min(max(pixels[y, x, 0], 0.0), 255.0)
            g = min(max(pixels[y, x, 1], 0.0), 255.0)
            b = min(max(pixels[y, x, 2], 0.0), 255.0)
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
                nx, ny = x + eff_dx, y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    pixels[ny, nx, 0] += er * wgt
                    pixels[ny, nx, 1] += eg * wgt
                    pixels[ny, nx, 2] += eb * wgt

        if y % 200 == 0:
            logger.debug("Dithering (unconstrained): %d/%d", y, h)
    return result_idx


def _noop(
    canvas: CanvasLike,
    lut: NDArray[np.uint8],
    lut_scale: float,
    serpentine: bool,
) -> UInt8Array:
    """Nearest-palette quantize per pixel — no error diffusion."""
    del serpentine
    pixels = np.clip(np.asarray(canvas, dtype=np.float32), 0, 255)
    n = lut.shape[0]
    lut_max = n - 1
    idx = np.minimum((pixels / lut_scale).astype(np.int32), lut_max)
    return lut[idx[..., 0], idx[..., 1], idx[..., 2]].astype(np.uint8)


# ── Public interface ───────────────────────────────────────────────

def dither(canvas: CanvasLike, cfg: DitherConfig) -> UInt8Array:
    """Run the configured full-canvas dither algorithm.

    Interchangeable with ``dither_constrained.dither`` — same call
    signature, same return type.  ``canvas`` should be a float32 H×W×3
    array pre-processed by the caller (adaptive-saturate + DRC applied),
    or a uint8 array (cast to float32 without further preprocessing).

    Memory peak: ~60 MB for a full 3200×1600 panel render.  Prefer
    ``dither_constrained.dither_with_prep`` for production rendering
    within the 50 MB budget.
    """
    return UnconstrainedDither().dither(canvas, cfg)


# ── Public class ─────────────────────────────────────────────────────────────


class UnconstrainedDither(AbstractDither):
    """Full-canvas error-diffusion dither — reference / quality-comparison strategy.

    Operates on a full H×W×3 float32 buffer mutated in place as errors
    propagate.  Memory peak ~60 MB for a 3200×1600 panel.  Interchangeable
    with ``StreamingDither`` via the ``AbstractDither`` interface.

    Intended uses:
      * ``test_dither_quality.py`` — side-by-side vs streaming.
      * Offline rendering where RAM is not the bottleneck.
    """

    _KERNELS: dict[str, _DiffusionKernel] = {
        "floyd_steinberg": _FS_KERNEL,
        "atkinson": _ATKINSON_KERNEL,
        "stucki": _STUCKI_KERNEL,
    }

    def dither(self, canvas: CanvasLike, cfg: DitherConfig) -> UInt8Array:
        if cfg.algorithm not in self._KERNELS and cfg.algorithm != "noop":
            raise ValueError(f"Unknown algorithm: {cfg.algorithm!r}")
        if cfg.lut_name not in ("euclidean", "hue_aware", "bw"):
            raise ValueError(f"Unknown lut_name: {cfg.lut_name!r}")
        lut, lut_scale = _lut_and_scale(cfg)
        if cfg.algorithm == "noop":
            return _noop(canvas, lut, lut_scale, cfg.serpentine)
        return _full_canvas_diffusion(canvas, self._KERNELS[cfg.algorithm], lut, lut_scale, cfg.serpentine)
