"""ImageRenderer: production image renderer backed by a pluggable dither strategy.

Accepts any ``AbstractDither`` instance at construction.  Defaults to
``StreamingDither()`` — the memory-constrained rolling-window path that keeps
peak transient under 50 MB per render.  Swap in ``NumbaDither()`` for a
native-speed inner loop (GIL released, true parallelism in the thread pool),
or ``UnconstrainedDither()`` for the full-canvas reference path.

Usage::

    from hokku_server.image_renderer import ImageRenderer
    from hokku_server.dither_numba import NumbaDither

    renderer = ImageRenderer(NumbaDither())
    panel_bytes = renderer.render_panel_bytes(img, cfg, "landscape")
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from hokku_server.dither_abc import AbstractDither
from hokku_server.dither_streaming import adaptive_saturate
from hokku_server.image_abc import (
    AbstractImageRenderer,
    Orientation,
    _encode_panel_rgb_to_png,
    _preview_canvas_dims,
)
from hokku_server.image_config import ImageConfig


def _compress_dynamic_range(img_array, *, scale_chroma, adaptive_vivid,
                             vivid_chroma_low, vivid_chroma_high):
    """Thin shim — delegates to image.compress_dynamic_range to avoid duplication."""
    from hokku_server.image import compress_dynamic_range
    return compress_dynamic_range(
        img_array,
        scale_chroma=scale_chroma,
        adaptive_vivid=adaptive_vivid,
        vivid_chroma_low=vivid_chroma_low,
        vivid_chroma_high=vivid_chroma_high,
    )


class ImageRenderer(AbstractImageRenderer):
    """Production renderer: fit/crop → enhancements → dither strategy.

    Parameters
    ----------
    dither:
        Any ``AbstractDither`` implementation.  When ``None``, defaults to
        ``StreamingDither()`` (memory-safe rolling-window path).
    """

    def __init__(self, dither: AbstractDither | None = None) -> None:
        if dither is None:
            from hokku_server.dither_streaming import StreamingDither
            dither = StreamingDither()
        self._dither = dither

    @property
    def dither(self) -> AbstractDither:
        return self._dither

    def render_indices(
        self,
        img: Image.Image,
        cfg: ImageConfig,
        orientation: Orientation,
        canvas_w: int,
        canvas_h: int,
        crop_to_fill_threshold: float = 0.0,
        *,
        release_input: bool = False,
    ) -> np.ndarray:
        arr, padding_mask = self._prepare_canvas(
            img, cfg, orientation, canvas_w, canvas_h,
            crop_to_fill_threshold, release_input=release_input,
        )

        drc_kwargs = dict(
            scale_chroma=cfg.scale_chroma,
            adaptive_vivid=cfg.adaptive_vivid,
            vivid_chroma_low=cfg.vivid_chroma_low,
            vivid_chroma_high=cfg.vivid_chroma_high,
        )
        use_sat = cfg.use_adaptive_saturate
        sat_max = cfg.saturate_max_enhance
        sat_lo = cfg.saturate_low_chroma_thresh
        sat_hi = cfg.saturate_high_chroma_thresh

        def _prep_stripe(stripe_uint8):
            if use_sat:
                f32 = adaptive_saturate(stripe_uint8, sat_max, sat_lo, sat_hi)
            else:
                f32 = stripe_uint8.astype(np.float32)
            return _compress_dynamic_range(f32, **drc_kwargs)

        result_idx = self._dither.dither_with_prep(arr, cfg.dither, _prep_stripe)
        del arr
        result_idx[padding_mask] = 1
        return result_idx
