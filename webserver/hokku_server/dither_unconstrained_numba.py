"""NumbaUnconstrainedDither: full-canvas dither with a Numba-JIT pixel loop.

Same full-canvas, mutating approach as ``UnconstrainedDither`` (~60 MB peak
for a 3200×1600 panel) but the inner ``for y: for x:`` loop is compiled to
native code with ``@numba.njit(nogil=True, cache=True)``, releasing the GIL
so parallel thread-pool renders can make genuine CPU progress.

Intended uses:
  * Fast tests that compare streaming vs unconstrained output without the
    pure-Python overhead of ``UnconstrainedDither``.
  * ``test_dither_quality.py`` time_intensive side-by-side renders alongside
    all four dither strategies.

Requires: ``numba >= 0.59`` (``pip install numba``).  0.59+ is needed for
NumPy 2.x compatibility (``numpy.core`` was removed in NumPy 2.0).  Import is deferred to
instantiation so other dither classes remain importable if numba is absent.
"""
from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from hokku_server.display import PALETTE_MEASURED_RGB
from hokku_server.dither_abc import (
    AbstractDither,
    CanvasLike,
    DiffusionKernel,
    UInt8Array,
)
from hokku_server.dither_config import DitherConfig
from hokku_server.dither_streaming import (
    _KERNEL_FOR,
    _validate,
    lut_and_scale_for_dither_config,
    noop_dither,
)


# ── Numba JIT kernel ──────────────────────────────────────────────────────────

def _make_jit_fn():
    """Build and return the Numba-JIT full-canvas diffuse function."""
    try:
        import numba
    except ImportError as exc:
        raise ImportError(
            "NumbaUnconstrainedDither requires numba (pip install numba)."
        ) from exc

    @numba.njit(nogil=True, cache=True)
    def _diffuse_full(
        pixels: np.ndarray,   # float32 (H, W, 3) — mutated in-place
        result: np.ndarray,   # uint8  (H, W)      — mutated in-place
        lut: np.ndarray,      # uint8  (n, n, n)
        lut_scale: float,
        pal_rgb: np.ndarray,  # float32 (N_PAL, 3)
        kdx: np.ndarray,      # int32  (K,)
        kdy: np.ndarray,      # int32  (K,)
        kwt: np.ndarray,      # float32 (K,)
        serpentine: bool,
        lut_max: int,
    ) -> None:
        H = pixels.shape[0]
        W = pixels.shape[1]
        K = kdx.shape[0]

        for y in range(H):
            reverse = serpentine and (y % 2 == 0)
            x_start = W - 1 if reverse else 0
            x_end   = -1    if reverse else W
            x_step  = -1    if reverse else 1

            x = x_start
            while x != x_end:
                r = pixels[y, x, 0]
                g = pixels[y, x, 1]
                b = pixels[y, x, 2]
                if r < 0.0:
                    r = 0.0
                elif r > 255.0:
                    r = 255.0
                if g < 0.0:
                    g = 0.0
                elif g > 255.0:
                    g = 255.0
                if b < 0.0:
                    b = 0.0
                elif b > 255.0:
                    b = 255.0

                ri = int(r / lut_scale)
                gi = int(g / lut_scale)
                bi = int(b / lut_scale)
                if ri > lut_max:
                    ri = lut_max
                if gi > lut_max:
                    gi = lut_max
                if bi > lut_max:
                    bi = lut_max

                idx = lut[ri, gi, bi]
                result[y, x] = idx

                er = r - pal_rgb[idx, 0]
                eg = g - pal_rgb[idx, 1]
                eb = b - pal_rgb[idx, 2]

                for k in range(K):
                    eff_dx = -kdx[k] if reverse else kdx[k]
                    nx = x + eff_dx
                    ny = y + kdy[k]
                    if 0 <= nx < W and 0 <= ny < H:
                        pixels[ny, nx, 0] += er * kwt[k]
                        pixels[ny, nx, 1] += eg * kwt[k]
                        pixels[ny, nx, 2] += eb * kwt[k]

                x += x_step

    return _diffuse_full


_jit_fn = None  # Lazy: built on first NumbaUnconstrainedDither use.


def _get_jit_fn():
    global _jit_fn
    if _jit_fn is None:
        _jit_fn = _make_jit_fn()
    return _jit_fn


def _kernel_arrays(kernel: DiffusionKernel):
    """Convert a kernel tuple-of-tuples to flat int32/float32 arrays for Numba."""
    kdx = np.array([dx for dx, _, _ in kernel], dtype=np.int32)
    kdy = np.array([dy for _, dy, _ in kernel], dtype=np.int32)
    kwt = np.array([w  for _, _,  w in kernel], dtype=np.float32)
    return kdx, kdy, kwt


# ── Public class ──────────────────────────────────────────────────────────────

class NumbaUnconstrainedDither(AbstractDither):
    """Full-canvas dither whose inner pixel loop is compiled to native code.

    Operates on a full H×W×3 float32 copy of the canvas, mutating it in place
    as errors propagate.  Memory peak ~60 MB for a 3200×1600 panel — same as
    ``UnconstrainedDither``.  The ``@numba.njit(nogil=True)`` decoration releases
    the GIL during the tight loop.

    ``dither_with_prep`` is inherited from ``AbstractDither``: it applies the
    prep callback to the whole canvas at once, then calls ``dither()``.  No
    stripe streaming — use ``NumbaStreamingDither`` if the 50 MB budget matters.

    First use triggers Numba compilation (~1 s on a Pi; cached via ``cache=True``).
    Raises ``ImportError`` at instantiation if numba is not installed.
    """

    def __init__(self) -> None:
        self._fn = _get_jit_fn()  # raises ImportError if numba not available

    def dither(self, canvas: CanvasLike, cfg: DitherConfig) -> UInt8Array:
        """Dither a pre-processed canvas using the Numba JIT full-canvas loop."""
        _validate(cfg)
        lut, scale = lut_and_scale_for_dither_config(cfg)
        if cfg.algorithm == "noop":
            return noop_dither(canvas, lut, scale, cfg.serpentine)

        pixels = np.asarray(canvas, dtype=np.float32).copy()
        if pixels.ndim != 3 or pixels.shape[2] != 3:
            raise ValueError(f"canvas must be H×W×3, got {pixels.shape}")

        H, W = int(pixels.shape[0]), int(pixels.shape[1])
        kernel = _KERNEL_FOR[cfg.algorithm]
        kdx, kdy, kwt = _kernel_arrays(kernel)
        lut_max = lut.shape[0] - 1
        pal_rgb = PALETTE_MEASURED_RGB.astype(np.float32)
        result = np.empty((H, W), dtype=np.uint8)

        self._fn(
            pixels, result,
            lut, float(scale), pal_rgb,
            kdx, kdy, kwt, cfg.serpentine, lut_max,
        )
        return result
