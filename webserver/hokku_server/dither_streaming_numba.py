"""NumbaDither: streaming dither with a Numba-JIT pixel loop (streaming variant).

Same stripe-by-stripe memory model as StreamingDither (≤ 50 MB peak); the
inner ``for y: for x:`` loop is compiled to native code with
``@numba.njit(nogil=True, cache=True)``, releasing the GIL so parallel
thread-pool renders can overlap without blocking the Python interpreter.

Requires: ``numba >= 0.56`` (``pip install numba``).  The module can be
imported without numba; the ImportError is deferred to instantiation time
so other dither classes are not affected.

Design: the JIT function ``_diffuse_stripe`` processes one stripe of rows at
a time.  The caller pre-loads the first row of each stripe into the rolling
error window before the call; the JIT function processes all rows in the
stripe, slides the window, and pre-loads intra-stripe rows itself.  At the
end of a stripe the window is zeroed, ready for the Python wrapper to add
the first row of the next stripe.
"""
from __future__ import annotations

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from hokku_server.display import PALETTE_MEASURED_RGB
from hokku_server.dither_abc import (
    AbstractDither,
    CanvasLike,
    DiffusionKernel,
    PrepStripe,
    UInt8Array,
    _DEFAULT_STRIPE_H,
)
from hokku_server.dither_config import DitherConfig
from hokku_server.dither_streaming import (
    _KERNEL_FOR,
    _validate,
    lut_and_scale_for_dither_config,
    noop_dither,
)


# ── Numba JIT kernel ─────────────────────────────────────────────────────────

def _make_jit_fn():
    """Build and return the Numba-JIT diffuse function.  Called once at first use."""
    try:
        import numba
    except ImportError as exc:
        raise ImportError(
            "NumbaDither requires numba (pip install numba).  "
            "Use StreamingDither or UnconstrainedDither without it."
        ) from exc

    @numba.njit(nogil=True, cache=True)
    def _diffuse_stripe(
        stripe: np.ndarray,    # float32 (stripe_h, W, 3) — pre-processed stripe
        y_start: int,          # global row index of stripe[0]
        H: int,                # total image height
        rolling: np.ndarray,   # float32 (n_rows, W, 3) — mutated in-place
        result: np.ndarray,    # uint8 (H, W) — mutated in-place
        lut: np.ndarray,       # uint8 (n, n, n)
        lut_scale: float,
        pal_rgb: np.ndarray,   # float32 (N_PAL, 3)
        kdx: np.ndarray,       # int32 (K,)
        kdy: np.ndarray,       # int32 (K,)
        kwt: np.ndarray,       # float32 (K,)
        serpentine: bool,
        n_rows: int,
        lut_max: int,
    ) -> None:
        """Process stripe rows [y_start, y_start+stripe_h) using the rolling window.

        On entry: rolling[0] contains accumulated pixels for row y_start.
        On exit: rolling[0] is zeroed; caller adds first row of next stripe.
        """
        W = stripe.shape[1]
        stripe_h = stripe.shape[0]
        K = kdx.shape[0]

        for local_y in range(stripe_h):
            y = y_start + local_y
            if y >= H:
                break

            reverse = serpentine and (y % 2 == 0)
            x_start = W - 1 if reverse else 0
            x_end = -1 if reverse else W
            x_step = -1 if reverse else 1

            x = x_start
            while x != x_end:
                r = rolling[0, x, 0]
                g = rolling[0, x, 1]
                b = rolling[0, x, 2]
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
                    dy_k = kdy[k]
                    if 0 <= nx < W and y + dy_k < H:
                        rolling[dy_k, nx, 0] += er * kwt[k]
                        rolling[dy_k, nx, 1] += eg * kwt[k]
                        rolling[dy_k, nx, 2] += eb * kwt[k]

                x += x_step

            # Slide rolling window up by one row, zero the vacated last row.
            for i in range(n_rows - 1):
                for xx in range(W):
                    rolling[i, xx, 0] = rolling[i + 1, xx, 0]
                    rolling[i, xx, 1] = rolling[i + 1, xx, 1]
                    rolling[i, xx, 2] = rolling[i + 1, xx, 2]
            for xx in range(W):
                rolling[n_rows - 1, xx, 0] = 0.0
                rolling[n_rows - 1, xx, 1] = 0.0
                rolling[n_rows - 1, xx, 2] = 0.0

            # Pre-load next row from this stripe (intra-stripe transition).
            next_local = local_y + 1
            if next_local < stripe_h and y + 1 < H:
                for xx in range(W):
                    rolling[0, xx, 0] += stripe[next_local, xx, 0]
                    rolling[0, xx, 1] += stripe[next_local, xx, 1]
                    rolling[0, xx, 2] += stripe[next_local, xx, 2]
            # Cross-stripe transitions: Python wrapper loads the first row of
            # the next stripe after this function returns.

    return _diffuse_stripe


_jit_fn = None  # Lazy: built on first NumbaDither use.


def _get_jit_fn():
    global _jit_fn
    if _jit_fn is None:
        _jit_fn = _make_jit_fn()
    return _jit_fn


def _kernel_arrays(kernel: DiffusionKernel):
    """Convert a kernel tuple-of-tuples to flat int32/float32 arrays for Numba."""
    kdx = np.array([dx for dx, _, _ in kernel], dtype=np.int32)
    kdy = np.array([dy for _, dy, _ in kernel], dtype=np.int32)
    kwt = np.array([w for _, _, w in kernel], dtype=np.float32)
    return kdx, kdy, kwt


# ── Public class ─────────────────────────────────────────────────────────────


class NumbaDither(AbstractDither):
    """Streaming dither whose inner pixel loop is compiled to native code.

    Identical memory budget to ``StreamingDither`` (rolling window + per-stripe
    prep callback, ≤ 50 MB peak).  The ``@numba.njit(nogil=True)`` decoration
    releases the GIL during the tight loop so multiple concurrent thread-pool
    renders can make genuine CPU progress in parallel.

    First use triggers Numba compilation (~1 s on a Pi; cached to ``__pycache__``
    for subsequent runs via ``cache=True``).

    Raises ``ImportError`` at instantiation if numba is not installed.
    """

    def __init__(self) -> None:
        self._fn = _get_jit_fn()  # raises ImportError if numba not available

    def dither(self, canvas: CanvasLike, cfg: DitherConfig) -> UInt8Array:
        """Dither a pre-processed canvas using the Numba JIT loop.

        Materialises the whole float32 canvas (same memory as unconstrained,
        ~60 MB for a full panel).  For the memory-bounded production path use
        ``dither_with_prep`` instead.
        """
        _validate(cfg)
        lut, scale = lut_and_scale_for_dither_config(cfg)
        if cfg.algorithm == "noop":
            return noop_dither(canvas, lut, scale, cfg.serpentine)

        if isinstance(canvas, Image.Image):
            arr = np.asarray(canvas, dtype=np.float32)
        else:
            arr = np.asarray(canvas, dtype=np.float32)
        if arr.ndim != 3 or arr.shape[2] != 3:
            raise ValueError(f"canvas must be H×W×3, got {arr.shape}")

        H, W = int(arr.shape[0]), int(arr.shape[1])
        kernel = _KERNEL_FOR[cfg.algorithm]
        kdx, kdy, kwt = _kernel_arrays(kernel)
        max_dy = int(kdy.max())
        n_rows = max_dy + 1
        lut_max = lut.shape[0] - 1
        pal_rgb = PALETTE_MEASURED_RGB.astype(np.float32)
        rolling = np.zeros((n_rows, W, 3), dtype=np.float32)
        result = np.empty((H, W), dtype=np.uint8)

        rolling[0] += arr[0]
        self._fn(
            arr, 0, H, rolling, result,
            lut, float(scale), pal_rgb,
            kdx, kdy, kwt, cfg.serpentine, n_rows, lut_max,
        )
        return result

    def dither_with_prep(
        self,
        canvas: CanvasLike,
        cfg: DitherConfig,
        prep_stripe: PrepStripe,
        stripe_h: int = _DEFAULT_STRIPE_H,
    ) -> UInt8Array:
        """Streaming Numba dither with per-stripe preprocessing.

        Peak memory matches ``StreamingDither.dither_with_prep``: one stripe of
        float32 plus the rolling window.  GIL is released during each JIT call.
        """
        _validate(cfg)
        lut, scale = lut_and_scale_for_dither_config(cfg)
        if cfg.algorithm == "noop":
            return noop_dither(canvas, lut, scale, cfg.serpentine)

        if isinstance(canvas, Image.Image):
            canvas_arr = np.asarray(canvas, dtype=np.uint8)
        else:
            canvas_arr = np.asarray(canvas, dtype=np.uint8)
        if canvas_arr.ndim != 3 or canvas_arr.shape[2] != 3:
            raise ValueError(f"canvas must be H×W×3, got {canvas_arr.shape}")

        H, W = int(canvas_arr.shape[0]), int(canvas_arr.shape[1])
        kernel = _KERNEL_FOR[cfg.algorithm]
        kdx, kdy, kwt = _kernel_arrays(kernel)
        max_dy = int(kdy.max())
        n_rows = max_dy + 1
        lut_max = lut.shape[0] - 1
        pal_rgb = PALETTE_MEASURED_RGB.astype(np.float32)
        rolling = np.zeros((n_rows, W, 3), dtype=np.float32)
        result = np.empty((H, W), dtype=np.uint8)

        # Prime the rolling window with the first row of the first stripe.
        y = 0
        stripe_end = min(stripe_h, H)
        stripe = prep_stripe(canvas_arr[y:stripe_end]).astype(np.float32)
        rolling[0] += stripe[0]

        while y < H:
            self._fn(
                stripe, y, H, rolling, result,
                lut, float(scale), pal_rgb,
                kdx, kdy, kwt, cfg.serpentine, n_rows, lut_max,
            )
            y += len(stripe)
            if y < H:
                # After Numba returns, rolling[0] is zeroed. Load first row
                # of the next stripe before the next JIT call.
                next_end = min(y + stripe_h, H)
                stripe = prep_stripe(canvas_arr[y:next_end]).astype(np.float32)
                rolling[0] += stripe[0]

        return result
