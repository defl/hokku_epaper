"""Spectra 6 streaming dither pipeline.

Memory-constrained error-diffusion dithers that operate on a rolling
2–3 row window instead of a full-panel float32 buffer.

Public entry-points
-------------------
dither(canvas, cfg)
    Simple interface shared with ``dither_unconstrained``.  ``canvas`` may be
    float32 (pre-processed by the caller) or uint8 (converted row-by-row
    without any additional preprocessing).  Suitable for tests and any caller
    that has already applied DRC / saturation.

dither_with_prep(canvas, cfg, prep_stripe, stripe_h)
    Production entry-point used by ``image.py``.  ``canvas`` is the raw uint8
    panel-sized array; ``prep_stripe`` is a callback that receives a uint8
    ``(stripe_h, W, 3)`` slice and returns a float32 array of the same shape
    with adaptive-saturate + DRC applied.  Stripe batching amortises the Lab
    conversion overhead while keeping peak memory well under the 50 MB budget.
    ``prep_stripe`` and ``stripe_h`` are *not* part of the interchangeable
    ``dither()`` interface — they are an implementation detail of the
    memory-constrained path.

Both ``dither`` and ``dither_with_prep`` return a ``UInt8Array`` (H×W) of
palette indices suitable for ``indices_to_panel_bytes``.
"""
from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import Any, TypeAlias

import numpy as np
from numpy.typing import ArrayLike, NDArray
from PIL import Image

from hokku_server.display import PALETTE_MEASURED_RGB
from hokku_server.dither_config import AlgorithmName, DitherConfig, LutName  # noqa: F401 (re-exported)

PrepStripe = Callable[[NDArray[np.uint8]], NDArray[np.float32]]
DiffusionKernel = tuple[tuple[int, int, float], ...]

# Stripe height (rows) the streaming dither batches at a time.
# 100 rows is a sweet spot on the memory/performance curve: small enough that
# adaptive_saturate + compress_dynamic_range transient buffers stay well under
# 20 MB, large enough to amortise the Python call / numpy dispatch overhead
# across hundreds of rows so per-render time stays close to the full-canvas
# baseline. Wider stripes blow the memory budget; narrower ones slow renders
# by 30–50 %.
_DEFAULT_STRIPE_H = 100

FloatArray: TypeAlias = NDArray[Any]
UInt8Array: TypeAlias = NDArray[np.uint8]
CanvasLike: TypeAlias = "Image.Image | ArrayLike"


# ── Color space ────────────────────────────────────────────────────

def srgb_to_linear(c: ArrayLike, *, dtype=np.float64) -> FloatArray:
    arr = np.asarray(c, dtype=dtype) / dtype(255.0)
    return np.where(arr <= dtype(0.04045), arr / dtype(12.92),
                    ((arr + dtype(0.055)) / dtype(1.055)) ** dtype(2.4))


def linear_to_xyz(rgb: ArrayLike, *, dtype=np.float64) -> FloatArray:
    rgb = np.asarray(rgb, dtype=dtype)
    M = np.array([
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ], dtype=dtype)
    return rgb @ M.T


def xyz_to_lab(xyz: ArrayLike, *, dtype=np.float64) -> FloatArray:
    xyz = np.asarray(xyz, dtype=dtype)
    ref = np.array([0.95047, 1.00000, 1.08883], dtype=dtype)
    scaled = xyz / ref
    f = np.where(scaled > dtype(0.008856),
                 scaled ** dtype(1 / 3),
                 dtype(7.787) * scaled + dtype(16 / 116))
    L = dtype(116) * f[..., 1] - dtype(16)
    a = dtype(500) * (f[..., 0] - f[..., 1])
    b = dtype(200) * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], axis=-1)


def rgb_to_lab(rgb: ArrayLike, *, dtype=np.float64) -> FloatArray:
    arr = np.clip(np.asarray(rgb, dtype=dtype), 0, 255)
    return xyz_to_lab(linear_to_xyz(srgb_to_linear(arr, dtype=dtype),
                                    dtype=dtype), dtype=dtype)


PALETTE_LAB = rgb_to_lab(PALETTE_MEASURED_RGB)


def adaptive_saturate(
    img_array: ArrayLike,
    max_enhance: float,
    low_thresh: float,
    high_thresh: float,
) -> FloatArray:
    """Boost saturation only on already-colourful pixels (chroma > low_thresh).

    Below low_thresh the factor is 1.0 (no change); above high_thresh it's
    ``max_enhance``; linearly ramped between.

    Operates in float32 throughout — the visible difference vs float64 is
    well below the dither quantisation noise.
    """
    f32 = np.float32
    rgb = np.asarray(img_array, dtype=f32)
    lab = rgb_to_lab(rgb, dtype=f32)
    chroma = np.sqrt(lab[..., 1] ** 2 + lab[..., 2] ** 2)
    t = np.clip((chroma - f32(low_thresh)) / f32(high_thresh - low_thresh),
                f32(0.0), f32(1.0))
    factor = f32(1.0) + f32(max_enhance - 1.0) * t
    lab[..., 1] *= factor
    lab[..., 2] *= factor

    # Lab → linear sRGB → sRGB (all float32, in-place where safe).
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
    fx3 = fx ** 3
    fz3 = fz ** 3
    xyz_out[..., 0] = np.where(fx3 > eps, fx3,
                               (f32(116) * fx - f32(16)) / kappa) * ref[0]
    xyz_out[..., 1] = np.where(L > kappa * eps,
                               ((L + f32(16)) / f32(116)) ** 3,
                               L / kappa) * ref[1]
    xyz_out[..., 2] = np.where(fz3 > eps, fz3,
                               (f32(116) * fz - f32(16)) / kappa) * ref[2]
    M_inv = np.array([
        [3.2404542, -1.5371385, -0.4985314],
        [-0.9692660, 1.8760108, 0.0415560],
        [0.0556434, -0.2040259, 1.0572252],
    ], dtype=f32)
    linear = np.clip(xyz_out @ M_inv.T, f32(0), f32(1))
    srgb = np.where(linear <= f32(0.0031308),
                    linear * f32(12.92),
                    f32(1.055) * (linear ** f32(1.0 / 2.4)) - f32(0.055))
    return np.clip(srgb * f32(255), f32(0), f32(255))


# ── LUTs ────────────────────────────────────────────────────────────

def build_rgb_lut() -> tuple[UInt8Array, float]:
    """32³ RGB grid → palette index by Euclidean Lab distance."""
    steps = 32
    scale = 256 / steps
    vals = np.arange(steps) * scale + scale / 2
    rr, gg, bb = np.meshgrid(vals, vals, vals, indexing="ij")
    rgb_grid = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)
    lab_grid = rgb_to_lab(rgb_grid)
    dists = np.sum((lab_grid[:, None, :] - PALETTE_LAB[None, :, :]) ** 2, axis=2)
    lut = np.argmin(dists, axis=1).astype(np.uint8).reshape(steps, steps, steps)
    return lut, scale


def build_rgb_lut_hue_aware(
    hue_cutoff_deg: float,
    neutral_chroma: float,
) -> tuple[UInt8Array, float]:
    """Like build_rgb_lut, but forbids hue-distant colour palette entries.

    For chromatic source pixels (chroma > neutral_chroma), candidate palette
    entries whose hue differs by more than hue_cutoff_deg are excluded — this
    stops e.g. saturated reds drifting into yellow.
    """
    steps = 32
    scale = 256 / steps
    vals = np.arange(steps) * scale + scale / 2
    rr, gg, bb = np.meshgrid(vals, vals, vals, indexing="ij")
    rgb_grid = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)
    lab_grid = rgb_to_lab(rgb_grid)

    pal_a = PALETTE_LAB[:, 1]
    pal_b = PALETTE_LAB[:, 2]
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
    dists = np.sum((lab_grid[:, None, :] - PALETTE_LAB[None, :, :]) ** 2, axis=2)
    dists = np.where(forbidden, np.inf, dists)
    lut = np.argmin(dists, axis=1).astype(np.uint8).reshape(steps, steps, steps)
    return lut, scale


@lru_cache(maxsize=1)
def _cached_euclidean_lut() -> tuple[UInt8Array, float]:
    return build_rgb_lut()


@lru_cache(maxsize=16)
def _cached_hue_aware_lut(hue_cutoff_deg: float, neutral_chroma: float) -> tuple[UInt8Array, float]:
    return build_rgb_lut_hue_aware(hue_cutoff_deg, neutral_chroma)


# ── Algorithms ──────────────────────────────────────────────────────

# Diffusion kernels expressed as (dx, dy, weight). dy is always non-negative
# (errors only flow forward). Forward direction is left-to-right; serpentine
# mode mirrors dx every other row.

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


def _streaming_diffusion_dither(
    canvas: CanvasLike,
    kernel: DiffusionKernel,
    lut: NDArray[np.uint8],
    lut_scale: float,
    serpentine: bool,
    prep_stripe: PrepStripe | None = None,
    stripe_h: int = _DEFAULT_STRIPE_H,
) -> UInt8Array:
    """Streaming error-diffusion dither.

    Holds only ``max_dy + 1`` rows of float32 RGB working state (typically
    2 rows for Floyd-Steinberg, 3 rows for Atkinson / Stucki) plus one
    cached pre-processed stripe of ``stripe_h`` rows.  The full-panel
    float32 buffer is never materialised.

    ``canvas`` may be:
    - uint8 H×W×3 numpy: rows are pulled in stripes of ``stripe_h``,
      pre-processed by ``prep_stripe`` (saturate + DRC).  ``prep_stripe``
      takes a uint8 ``(stripe_h, W, 3)`` view and returns a float32 array
      of the same shape.  If ``prep_stripe`` is None, rows are cast to
      float32 without preprocessing.
    - PIL Image: converted once to a uint8 numpy view (no copy).
    - float32 H×W×3 numpy: used as-is; ``prep_stripe`` is ignored.

    Stripe batching lets the heavy numpy work (Lab conversion, DRC math)
    amortise its Python overhead across many rows while keeping transient
    buffers small.
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

    # Rolling stripe cache: holds at most one pre-processed stripe at a time.
    stripe_y0 = -1
    stripe_data: NDArray[np.float32] | None = None

    def _row_pixels(y: int) -> NDArray[np.float32]:
        """Return the float32 pixel row for canvas row y."""
        nonlocal stripe_y0, stripe_data
        if not use_prep:
            return canvas_arr[y].astype(np.float32, copy=True)
        sh = stripe_h
        new_y0 = (y // sh) * sh
        if new_y0 != stripe_y0:
            # Drop the previous stripe BEFORE prep_stripe runs so the GC
            # can reclaim its memory while the new stripe's transients
            # are being allocated.
            stripe_data = None
            y1 = min(new_y0 + sh, H)
            stripe_data = prep_stripe(canvas_arr[new_y0:y1])  # type: ignore[misc]
            stripe_y0 = new_y0
        assert stripe_data is not None
        # copy=True so error-diffusion writes don't poison the cache for
        # rows we haven't reached yet.
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

        # Slide the rolling window up by one row, then pull in row y+1.
        if y + 1 < H:
            for i in range(n_rows - 1):
                rolling[i] = rolling[i + 1]
            rolling[n_rows - 1].fill(0)
            rolling[0] += _row_pixels(y + 1)

        if y % 200 == 0:
            print(f"  Dithering (streaming): {y}/{H}")

    return result_idx


def noop_dither(
    canvas: CanvasLike,
    lut: NDArray[np.uint8],
    lut_scale: float,
    serpentine: bool,
) -> UInt8Array:
    """Nearest-palette quantize per pixel — no error diffusion. Fast path for tests."""
    del serpentine  # not applicable
    pixels = np.clip(np.asarray(canvas, dtype=np.float32), 0, 255)
    n = lut.shape[0]
    lut_max = n - 1
    idx = np.minimum((pixels / lut_scale).astype(np.int32), lut_max)
    return lut[idx[..., 0], idx[..., 1], idx[..., 2]].astype(np.uint8)


# ── Config + dispatch ──────────────────────────────────────────────

def lut_and_scale_for_dither_config(cfg: DitherConfig) -> tuple[NDArray[np.uint8], float]:
    if cfg.lut_name == "euclidean":
        return _cached_euclidean_lut()
    return _cached_hue_aware_lut(cfg.hue_cutoff_deg, cfg.neutral_chroma)


_KERNEL_FOR: dict[str, DiffusionKernel] = {
    "floyd_steinberg": _FS_KERNEL,
    "atkinson": _ATKINSON_KERNEL,
    "stucki": _STUCKI_KERNEL,
}


def dither(canvas: CanvasLike, cfg: DitherConfig) -> UInt8Array:
    """Run the configured streaming dither algorithm.

    Interchangeable with ``dither_unconstrained.dither`` — same call
    signature, same return type.  ``canvas`` should be a float32 H×W×3
    array (pre-processed by the caller with DRC / saturate), or a uint8
    array (cast to float32 row-by-row without further preprocessing).

    For the memory-constrained production path where adaptive-saturate + DRC
    are applied in 100-row stripes, use ``dither_with_prep`` instead.
    """
    if cfg.algorithm not in _KERNEL_FOR and cfg.algorithm != "noop":
        raise ValueError(f"Unknown algorithm: {cfg.algorithm!r}")
    if cfg.lut_name not in ("euclidean", "hue_aware"):
        raise ValueError(f"Unknown lut_name: {cfg.lut_name!r}")
    lut, lut_scale = lut_and_scale_for_dither_config(cfg)
    if cfg.algorithm == "noop":
        return noop_dither(canvas, lut, lut_scale, cfg.serpentine)
    return _streaming_diffusion_dither(
        canvas, _KERNEL_FOR[cfg.algorithm], lut, lut_scale, cfg.serpentine,
    )


def dither_with_prep(
    canvas: CanvasLike,
    cfg: DitherConfig,
    prep_stripe: PrepStripe,
    stripe_h: int = _DEFAULT_STRIPE_H,
) -> UInt8Array:
    """Streaming dither with per-stripe preprocessing.

    Production entry-point for ``image.py``.  ``canvas`` is the raw uint8
    panel array; ``prep_stripe`` receives uint8 ``(stripe_h, W, 3)`` slices
    and returns float32 arrays with adaptive-saturate + DRC applied.

    ``prep_stripe`` and ``stripe_h`` are implementation details of the
    memory-constrained path — they are deliberately absent from the
    interchangeable ``dither()`` interface above.
    """
    if cfg.algorithm not in _KERNEL_FOR and cfg.algorithm != "noop":
        raise ValueError(f"Unknown algorithm: {cfg.algorithm!r}")
    if cfg.lut_name not in ("euclidean", "hue_aware"):
        raise ValueError(f"Unknown lut_name: {cfg.lut_name!r}")
    lut, lut_scale = lut_and_scale_for_dither_config(cfg)
    if cfg.algorithm == "noop":
        return noop_dither(canvas, lut, lut_scale, cfg.serpentine)
    return _streaming_diffusion_dither(
        canvas, _KERNEL_FOR[cfg.algorithm], lut, lut_scale, cfg.serpentine,
        prep_stripe=prep_stripe, stripe_h=stripe_h,
    )
