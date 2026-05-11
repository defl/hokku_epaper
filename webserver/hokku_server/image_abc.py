"""Abstract base class for Spectra 6 image renderers.

All renderers share the same public surface:

  render_panel_bytes(img, cfg, orientation, crop_to_fill_threshold) → bytes
  render_preview_png(img, cfg, orientation, max_side_px, crop_to_fill_threshold) → bytes

The shared fit-crop → PIL enhancements → rotate pipeline lives in the
protected ``_prepare_canvas()`` template method so concrete subclasses
only need to implement the dither dispatch in ``render_indices()``.
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from io import BytesIO
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageEnhance, ImageOps

from hokku_server.display import (
    FULL_W,
    PANEL_H,
    indices_to_panel_bytes,
    indices_to_preview_rgb,
)
from hokku_server.image_config import ImageConfig, Orientation  # noqa: F401 (re-exported)

if TYPE_CHECKING:
    pass


def _preview_canvas_dims(orientation: Orientation, max_side_px: int) -> tuple[int, int]:
    """Scale (FULL_W, PANEL_H) so the longer side is ≤ max_side_px."""
    s = min(1.0, float(max_side_px) / float(max(FULL_W, PANEL_H)))
    cw = max(1, int(FULL_W * s))
    ch = max(1, int(PANEL_H * s))
    return cw, ch


def _encode_panel_rgb_to_png(panel_rgb: NDArray[np.uint8], orientation: Orientation) -> bytes:
    """Panel-memory RGB → PNG bytes in the visible (browser) orientation."""
    img = Image.fromarray(np.asarray(panel_rgb, dtype=np.uint8))
    if orientation == "landscape":
        img = img.rotate(90, expand=True)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def preview_png_from_panel_bytes(panel_bytes: bytes, orientation: "Orientation") -> bytes:
    """Decode an already-rendered panel binary back to a PNG preview."""
    from hokku_server.display import panel_bytes_to_indices, indices_to_preview_rgb
    idx = panel_bytes_to_indices(panel_bytes)
    rgb = indices_to_preview_rgb(idx)
    return _encode_panel_rgb_to_png(rgb, orientation)


def _apply_prepare_enhancements(canvas: Image.Image, cfg: ImageConfig) -> Image.Image:
    canvas = ImageOps.autocontrast(canvas, cutoff=cfg.prepare_autocontrast_cutoff)
    gamma_lut = [int(((i / 255.0) ** cfg.prepare_gamma) * 255) for i in range(256)] * 3
    canvas = canvas.point(gamma_lut)
    canvas = ImageEnhance.Brightness(canvas).enhance(cfg.prepare_brightness)
    canvas = ImageEnhance.Contrast(canvas).enhance(cfg.prepare_contrast)
    canvas = ImageEnhance.Sharpness(canvas).enhance(cfg.prepare_sharpness)
    if not cfg.use_adaptive_saturate:
        canvas = ImageEnhance.Color(canvas).enhance(cfg.color_enhance)
    return canvas


class AbstractImageRenderer(ABC):
    """Strategy interface for panel-image rendering.

    ``_prepare_canvas()`` is the shared template: it handles fit/crop,
    PIL enhancements, and rotation, returning a uint8 numpy array and a
    boolean padding mask.  Concrete subclasses implement ``render_indices()``
    which calls ``_prepare_canvas()`` and dispatches to their dither strategy.
    """

    # ── Template method ────────────────────────────────────────────────────

    def _prepare_canvas(
        self,
        img: Image.Image,
        cfg: ImageConfig,
        orientation: Orientation,
        canvas_w: int,
        canvas_h: int,
        crop_to_fill_threshold: float = 0.0,
        *,
        release_input: bool = False,
    ) -> tuple[NDArray[np.uint8], NDArray[np.bool_]]:
        """Fit-or-crop → PIL enhancements → rotate.

        Returns ``(uint8_array, padding_mask)`` where ``padding_mask`` is
        True for pixels that are white letterbox padding and should be forced
        to palette index 1 (white ink) after dithering.
        """
        portrait = orientation == "portrait"
        visible_w, visible_h = (canvas_w, canvas_h) if portrait else (canvas_h, canvas_w)

        src_w, src_h = img.size
        scale_fit = min(visible_w / src_w, visible_h / src_h)
        scale_cover = max(visible_w / src_w, visible_h / src_h)
        zoom_ratio = scale_cover / scale_fit - 1.0

        use_cover = crop_to_fill_threshold > 0.0 and zoom_ratio <= crop_to_fill_threshold

        if use_cover:
            scaled_w = max(visible_w, math.ceil(src_w * scale_cover))
            scaled_h = max(visible_h, math.ceil(src_h * scale_cover))
            img_scaled = img.resize((scaled_w, scaled_h), Image.LANCZOS)
            if release_input:
                img.close()
            x_off = (scaled_w - visible_w) // 2
            y_off = (scaled_h - visible_h) // 2
            composed = img_scaled.crop((x_off, y_off, x_off + visible_w, y_off + visible_h))
            img_scaled.close()
            padding_mask = np.zeros((visible_h, visible_w), dtype=bool)
        else:
            scale = scale_fit
            new_w, new_h = int(src_w * scale), int(src_h * scale)
            img_resized = img.resize((new_w, new_h), Image.LANCZOS)
            if release_input:
                img.close()
            composed = Image.new("RGB", (visible_w, visible_h), (255, 255, 255))
            x_off = (visible_w - new_w) // 2
            y_off = (visible_h - new_h) // 2
            composed.paste(img_resized, (x_off, y_off))
            img_resized.close()
            padding_mask = np.ones((visible_h, visible_w), dtype=bool)
            padding_mask[y_off:y_off + new_h, x_off:x_off + new_w] = False

        composed = _apply_prepare_enhancements(composed, cfg)

        if not portrait:
            composed = composed.rotate(-90, expand=True)
            padding_mask = np.rot90(padding_mask, k=3)

        arr = np.asarray(composed, dtype=np.uint8)
        composed = None  # noqa: F841
        return arr, padding_mask

    # ── Abstract core ──────────────────────────────────────────────────────

    @abstractmethod
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
    ) -> NDArray[np.uint8]:
        """Render to palette indices.

        Implementations call ``self._prepare_canvas(...)`` and then apply
        their dither strategy.  Returns H×W uint8 palette-index array.
        """

    # ── Concrete public API ────────────────────────────────────────────────

    def render_panel_bytes(
        self,
        img: Image.Image,
        cfg: ImageConfig,
        orientation: Orientation,
        crop_to_fill_threshold: float = 0.0,
    ) -> bytes:
        """Full-resolution panel → wire bytes."""
        idx = self.render_indices(
            img, cfg, orientation, FULL_W, PANEL_H,
            crop_to_fill_threshold, release_input=True,
        )
        return indices_to_panel_bytes(idx)

    def render_preview_png(
        self,
        img: Image.Image,
        cfg: ImageConfig,
        orientation: Orientation,
        max_side_px: int = 800,
        crop_to_fill_threshold: float = 0.0,
    ) -> bytes:
        """Smaller panel → PNG preview bytes."""
        cw, ch = _preview_canvas_dims(orientation, max_side_px)
        idx = self.render_indices(img, cfg, orientation, cw, ch, crop_to_fill_threshold)
        preview_rgb = indices_to_preview_rgb(idx)
        return _encode_panel_rgb_to_png(preview_rgb, orientation)
