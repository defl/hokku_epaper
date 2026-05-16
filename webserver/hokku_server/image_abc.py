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

import cv2
import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from hokku_server.bounding_box import BoundingBox
from hokku_server.display import (
    FULL_W,
    PANEL_H,
    indices_to_panel_bytes,
    indices_to_preview_rgb,
    panel_bytes_to_indices,
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


def transform_bboxes_to_canvas_norm(
    bboxes_norm: tuple[BoundingBox, ...] | None,
    orig_w: int,
    orig_h: int,
    orientation: Orientation,
    canvas_w: int,
    canvas_h: int,
    crop_to_fill_threshold: float = 0.0,
) -> list[tuple[float, float, float, float]]:
    """Convert face bboxes from original-image normalised coords to coords
    normalised against the rendered **preview PNG** (the PNG returned by
    ``render_preview_png``, which is already in visible orientation —
    ``_encode_panel_rgb_to_png`` rotates landscape back +90° to undo
    ``_prepare_canvas``'s -90°).

    Mirrors the fit/cover scaling in ``_prepare_canvas``. Result is normalised
    against the *visible* dimensions so it lines up with the PNG the browser
    actually displays.
    """
    if not bboxes_norm:
        return []

    portrait = orientation == "portrait"
    visible_w, visible_h = (canvas_w, canvas_h) if portrait else (canvas_h, canvas_w)

    scale_fit = min(visible_w / orig_w, visible_h / orig_h)
    scale_cover = max(visible_w / orig_w, visible_h / orig_h)
    zoom_ratio = scale_cover / scale_fit - 1.0
    use_cover = crop_to_fill_threshold > 0.0 and zoom_ratio <= crop_to_fill_threshold

    if use_cover:
        scaled_w = max(visible_w, math.ceil(orig_w * scale_cover))
        scaled_h = max(visible_h, math.ceil(orig_h * scale_cover))
        x_off = (scaled_w - visible_w) // 2
        y_off = (scaled_h - visible_h) // 2
    else:
        scaled_w = orig_w * scale_fit
        scaled_h = orig_h * scale_fit
        x_off = (visible_w - scaled_w) / 2
        y_off = (visible_h - scaled_h) / 2

    out: list[tuple[float, float, float, float]] = []
    for bbox in bboxes_norm:
        if use_cover:
            fx = bbox.x * scaled_w - x_off
            fy = bbox.y * scaled_h - y_off
        else:
            fx = bbox.x * scaled_w + x_off
            fy = bbox.y * scaled_h + y_off
        fw = bbox.w * scaled_w
        fh = bbox.h * scaled_h

        # Clamp to visible bounds
        fx = max(0.0, fx)
        fy = max(0.0, fy)
        fw = max(0.0, min(fw, visible_w - fx))
        fh = max(0.0, min(fh, visible_h - fy))

        out.append((fx / visible_w, fy / visible_h, fw / visible_w, fh / visible_h))
    return out


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
    idx = panel_bytes_to_indices(panel_bytes)
    rgb = indices_to_preview_rgb(idx)
    return _encode_panel_rgb_to_png(rgb, orientation)


def _apply_prepare_enhancements(
    canvas: Image.Image,
    cfg: ImageConfig,
    keepout_bboxes_canvas: list[tuple[int, int, int, int]] | None = None,
) -> Image.Image:
    # 1. Global autocontrast
    canvas = ImageOps.autocontrast(canvas, cutoff=cfg.prepare_autocontrast_cutoff)

    # 2. Gamma correction (power curve via uint8 LUT)
    gamma_lut = [int(((i / 255.0) ** cfg.prepare_gamma) * 255) for i in range(256)] * 3
    canvas = canvas.point(gamma_lut)

    # 3. Midtone lift (separate power curve; 1.0 = identity, skipped for speed)
    if cfg.prepare_midtone != 1.0:
        exp = 1.0 / cfg.prepare_midtone
        midtone_lut = [round(255 * (i / 255.0) ** exp) if i > 0 else 0 for i in range(256)] * 3
        canvas = canvas.point(midtone_lut)

    # 4. Brightness / contrast
    canvas = ImageEnhance.Brightness(canvas).enhance(cfg.prepare_brightness)
    canvas = ImageEnhance.Contrast(canvas).enhance(cfg.prepare_contrast)

    # 5. CLAHE local contrast on Lab L* channel (skipped when clip_limit == 0).
    #    When face bboxes are provided, the face region's L* is restored after
    #    CLAHE so the local contrast expansion doesn't blow out skin highlights.
    if cfg.clahe_clip_limit > 0.0:
        arr = np.asarray(canvas, dtype=np.uint8)
        lab = cv2.cvtColor(arr, cv2.COLOR_RGB2LAB)
        clahe = cv2.createCLAHE(
            clipLimit=cfg.clahe_clip_limit,
            tileGridSize=(8, 8),
        )
        if keepout_bboxes_canvas:
            saved = [(fx, fy, fw, fh, lab[fy:fy + fh, fx:fx + fw, 0].copy())
                     for fx, fy, fw, fh in keepout_bboxes_canvas]
            lab[:, :, 0] = clahe.apply(lab[:, :, 0])
            for fx, fy, fw, fh, face_L in saved:
                lab[fy:fy + fh, fx:fx + fw, 0] = face_L
        else:
            lab[:, :, 0] = clahe.apply(lab[:, :, 0])
        canvas = Image.fromarray(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB))

    # 6. Unsharp mask sharpening (replaces fixed PIL Sharpness kernel)
    canvas = canvas.filter(
        ImageFilter.UnsharpMask(
            radius=cfg.prepare_usm_radius,
            percent=cfg.prepare_usm_amount,
            threshold=3,  # ignore diffs < 3 to avoid sharpening noise
        )
    )

    # 7. Colour enhance (only when adaptive_saturate is off — both boost chroma)
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
        clahe_keepout_bboxes_norm: tuple[BoundingBox, ...] | None = None,
    ) -> tuple[NDArray[np.uint8], NDArray[np.bool_]]:
        """Fit-or-crop → PIL enhancements → rotate.

        Returns ``(uint8_array, padding_mask)`` where ``padding_mask`` is
        True for pixels that are white letterbox padding and should be forced
        to palette index 1 (white ink) after dithering.

        clahe_keepout_bboxes_norm: [(x, y, w, h), ...] in [0, 1] relative to the original image.
        Converted to canvas pixel coordinates and passed to
        _apply_prepare_enhancements to scope CLAHE away from the face regions.
        """
        portrait = orientation == "portrait"
        visible_w, visible_h = (canvas_w, canvas_h) if portrait else (canvas_h, canvas_w)

        src_w, src_h = img.size
        scale_fit = min(visible_w / src_w, visible_h / src_h)
        scale_cover = max(visible_w / src_w, visible_h / src_h)
        zoom_ratio = scale_cover / scale_fit - 1.0

        use_cover = crop_to_fill_threshold > 0.0 and zoom_ratio <= crop_to_fill_threshold

        keepout_canvas: list[tuple[int, int, int, int]] = []

        if use_cover:
            scaled_w = max(visible_w, math.ceil(src_w * scale_cover))
            scaled_h = max(visible_h, math.ceil(src_h * scale_cover))
            img_scaled = img.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)
            if release_input:
                img.close()
            x_off = (scaled_w - visible_w) // 2
            y_off = (scaled_h - visible_h) // 2
            composed = img_scaled.crop((x_off, y_off, x_off + visible_w, y_off + visible_h))
            img_scaled.close()
            padding_mask = np.zeros((visible_h, visible_w), dtype=bool)
            # Bbox in canvas coords for cover: scale by cover scale, subtract crop offset
            if clahe_keepout_bboxes_norm:
                for bbox in clahe_keepout_bboxes_norm:
                    fx = int(bbox.x * scaled_w) - x_off
                    fy = int(bbox.y * scaled_h) - y_off
                    fw = int(bbox.w * scaled_w)
                    fh = int(bbox.h * scaled_h)
                    keepout_canvas.append((
                        max(0, fx), max(0, fy),
                        min(fw, visible_w - max(0, fx)),
                        min(fh, visible_h - max(0, fy)),
                    ))
        else:
            scale = scale_fit
            new_w, new_h = int(src_w * scale), int(src_h * scale)
            img_resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            if release_input:
                img.close()
            composed = Image.new("RGB", (visible_w, visible_h), (255, 255, 255))
            x_off = (visible_w - new_w) // 2
            y_off = (visible_h - new_h) // 2
            composed.paste(img_resized, (x_off, y_off))
            img_resized.close()
            padding_mask = np.ones((visible_h, visible_w), dtype=bool)
            padding_mask[y_off:y_off + new_h, x_off:x_off + new_w] = False
            # Bbox in canvas coords for fit: scale by fit scale, add letterbox offset
            if clahe_keepout_bboxes_norm:
                for bbox in clahe_keepout_bboxes_norm:
                    fx = int(bbox.x * new_w) + x_off
                    fy = int(bbox.y * new_h) + y_off
                    fw = int(bbox.w * new_w)
                    fh = int(bbox.h * new_h)
                    keepout_canvas.append((
                        max(0, fx), max(0, fy),
                        min(fw, visible_w - max(0, fx)),
                        min(fh, visible_h - max(0, fy)),
                    ))

        composed = _apply_prepare_enhancements(composed, cfg, keepout_canvas or None)

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
        clahe_keepout_bboxes_norm: tuple[BoundingBox, ...] | None = None,
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
        clahe_keepout_bboxes_norm: tuple["BoundingBox", ...] | None = None,
    ) -> bytes:
        """Full-resolution panel → wire bytes."""
        idx = self.render_indices(
            img, cfg, orientation, FULL_W, PANEL_H,
            crop_to_fill_threshold, release_input=True,
            clahe_keepout_bboxes_norm=clahe_keepout_bboxes_norm,
        )
        return indices_to_panel_bytes(idx)

    def render_preview_png(
        self,
        img: Image.Image,
        cfg: ImageConfig,
        orientation: Orientation,
        max_side_px: int = 800,
        crop_to_fill_threshold: float = 0.0,
        clahe_keepout_bboxes_norm: tuple[BoundingBox, ...] | None = None,
    ) -> bytes:
        """Smaller panel → PNG preview bytes."""
        cw, ch = _preview_canvas_dims(orientation, max_side_px)
        idx = self.render_indices(
            img, cfg, orientation, cw, ch, crop_to_fill_threshold,
            clahe_keepout_bboxes_norm=clahe_keepout_bboxes_norm,
        )
        preview_rgb = indices_to_preview_rgb(idx)
        return _encode_panel_rgb_to_png(preview_rgb, orientation)
