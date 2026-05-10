"""Abstract base class for palette dithers.

All dither implementations expose the same two-method API:

  dither(canvas, cfg) → UInt8Array
      Operate on a pre-processed canvas.  The caller is responsible for any
      adaptive-saturate + DRC preprocessing beforehand.

  dither_with_prep(canvas, cfg, prep_stripe, stripe_h) → UInt8Array
      Production path: canvas is the raw uint8 panel array; prep_stripe is a
      callback ``(uint8 stripe) → float32 stripe`` that applies adaptive-
      saturate + DRC per horizontal band.  The default implementation applies
      prep_stripe to the full canvas and delegates to dither(); streaming
      subclasses override this to call prep_stripe lazily per stripe and keep
      peak memory within the 50 MB budget.

Common type aliases live here so downstream modules share a single definition.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeAlias

import numpy as np
from numpy.typing import ArrayLike, NDArray
from PIL import Image

if TYPE_CHECKING:
    from hokku_server.dither_config import DitherConfig

PrepStripe = Callable[[NDArray[np.uint8]], NDArray[np.float32]]
DiffusionKernel = tuple[tuple[int, int, float], ...]

FloatArray: TypeAlias = NDArray[Any]
UInt8Array: TypeAlias = NDArray[np.uint8]
CanvasLike: TypeAlias = "Image.Image | ArrayLike"

_DEFAULT_STRIPE_H = 100


class AbstractDither(ABC):
    """Strategy interface for Spectra 6 palette dithers.

    Subclasses differ in memory strategy (streaming / full-canvas / Numba JIT)
    while presenting the same two-method API.  The algorithm (Floyd-Steinberg,
    Atkinson, Stucki, noop) is selected via ``DitherConfig.algorithm`` at call
    time, not at construction — one class handles all algorithms.
    """

    @abstractmethod
    def dither(self, canvas: CanvasLike, cfg: "DitherConfig") -> UInt8Array:
        """Dither a pre-processed canvas.

        Parameters
        ----------
        canvas:
            float32 H×W×3 (adaptive-saturate + DRC already applied) **or**
            uint8 H×W×3 (cast to float32 without further preprocessing).
            A PIL Image is also accepted and converted internally.
        cfg:
            Selects algorithm, LUT, and serpentine scan order.

        Returns
        -------
        UInt8Array of shape ``(H, W)`` — palette indices, values in
        ``range(N_PALETTE_COLOURS)``.
        """

    def dither_with_prep(
        self,
        canvas: CanvasLike,
        cfg: "DitherConfig",
        prep_stripe: PrepStripe,
        stripe_h: int = _DEFAULT_STRIPE_H,
    ) -> UInt8Array:
        """Dither with per-stripe preprocessing.

        Default: applies ``prep_stripe`` to the full canvas in one call, then
        delegates to ``dither()``.  Streaming subclasses override this to call
        ``prep_stripe`` lazily one stripe at a time, keeping transient memory
        within the per-render budget.

        Parameters
        ----------
        canvas:
            Raw uint8 H×W×3 panel array (or PIL Image).
        cfg:
            Dither algorithm and LUT selection.
        prep_stripe:
            ``(uint8 stripe) → float32 stripe`` callback that applies
            adaptive-saturate + DRC to each horizontal band.
        stripe_h:
            Row-batch hint for streaming subclasses; ignored here.
        """
        if isinstance(canvas, Image.Image):
            arr = np.asarray(canvas, dtype=np.uint8)
        else:
            arr = np.asarray(canvas, dtype=np.uint8)
        f32 = prep_stripe(arr)
        return self.dither(f32, cfg)
