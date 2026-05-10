"""Image rendering pipeline.

Public API (unchanged from before the class refactor):
  render_panel_bytes()          — full-resolution panel → wire bytes
  render_preview_png()          — scaled PNG for the web GUI
  open_image_for_render()       — open + normalise a source file
  preview_png_from_panel_bytes()— decode an already-rendered panel to PNG
  is_grayscale()                — True iff the image at a path is monochrome
  compress_dynamic_range()      — map source L* into the panel's reachable range

``render_panel_bytes`` and ``render_preview_png`` delegate to a module-level
``ImageRenderer(StreamingDither())`` instance.  Pass a custom ``AbstractDither``
to ``ImageRenderer`` directly when you need a different memory/speed strategy.
"""
from __future__ import annotations

import time
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import ArrayLike, NDArray
from PIL import Image, ImageOps

from hokku_server.display import (
    FULL_W,
    PANEL_H,
    VISUAL_H,  # noqa: F401 (re-exported)
    VISUAL_W,  # noqa: F401 (re-exported)
    indices_to_preview_rgb,
    panel_bytes_to_indices,
)
from hokku_server.dither_streaming import (
    PALETTE_LAB,
    linear_to_xyz,
    rgb_to_lab,
    srgb_to_linear,
    xyz_to_lab,
)
from hokku_server.image_abc import (  # noqa: F401 (re-exported)
    _apply_prepare_enhancements,
    _encode_panel_rgb_to_png,
)
from hokku_server.image_config import ImageConfig, Orientation  # noqa: F401 (re-exported)


IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp", ".gif",
    ".heic", ".heif", ".avif",
}

GRAYSCALE_CHROMA_THRESHOLD = 8.0

_DISPLAY_BLACK_L = float(PALETTE_LAB[0, 0])
_DISPLAY_WHITE_L = float(PALETTE_LAB[1, 0])


# ── Lab → RGB ────────────────────────────────────────────────────────────────

def _lab_to_rgb(lab: ArrayLike) -> NDArray[np.float32]:
    """float32 Lab → float32 sRGB.  Avoids float64 intermediates."""
    f32 = np.float32
    lab = np.asarray(lab, dtype=f32)
    ref = np.array([0.95047, 1.00000, 1.08883], dtype=f32)
    L = lab[..., 0]
    a = lab[..., 1]
    b_ch = lab[..., 2]
    fy = (L + f32(16)) / f32(116)
    fx = a / f32(500) + fy
    fz = fy - b_ch / f32(200)
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


def compress_dynamic_range(
    img_array: ArrayLike,
    *,
    scale_chroma: bool,
    adaptive_vivid: bool,
    vivid_chroma_low: float,
    vivid_chroma_high: float,
) -> NDArray[np.float32]:
    """Map source Lab range into the panel's reachable L* range."""
    f32 = np.float32
    rgb = np.asarray(img_array, dtype=f32)
    lab = rgb_to_lab(rgb, dtype=f32)
    L = lab[..., 0]
    a = lab[..., 1]
    b_ch = lab[..., 2]
    black_L = f32(_DISPLAY_BLACK_L)
    white_L = f32(_DISPLAY_WHITE_L)
    c_ratio = f32((_DISPLAY_WHITE_L - _DISPLAY_BLACK_L) / 100.0)
    np.multiply(L, f32((white_L - black_L) / 100.0), out=L)
    np.add(L, black_L, out=L)
    if adaptive_vivid:
        chroma = np.sqrt(a * a + b_ch * b_ch)
        t = np.clip(
            (chroma - f32(vivid_chroma_low))
            / f32(vivid_chroma_high - vivid_chroma_low),
            f32(0.0), f32(1.0),
        )
        c_factor = c_ratio + (f32(1.0) - c_ratio) * t
        np.multiply(a, c_factor, out=a)
        np.multiply(b_ch, c_factor, out=b_ch)
    elif scale_chroma:
        np.multiply(a, c_ratio, out=a)
        np.multiply(b_ch, c_ratio, out=b_ch)
    return _lab_to_rgb(lab)


# ── Grayscale detection ───────────────────────────────────────────────────────

def _is_near_grayscale(img: Image.Image) -> bool:
    thumb = img.copy()
    thumb.thumbnail((200, 200), Image.LANCZOS)
    arr = np.asarray(thumb.convert("RGB"), dtype=np.float64)
    lab = xyz_to_lab(linear_to_xyz(srgb_to_linear(arr)))
    chroma = np.sqrt(lab[..., 1] ** 2 + lab[..., 2] ** 2)
    return float(np.percentile(chroma, 95)) < GRAYSCALE_CHROMA_THRESHOLD


is_grayscale_image = _is_near_grayscale


def is_grayscale(path: Path) -> bool:
    """True iff the image at *path* is essentially monochrome."""
    with open_image_for_render(path) as img:
        return is_grayscale_image(img)


def _bw_safe_image_config(cfg: ImageConfig) -> ImageConfig:
    return replace(
        cfg,
        color_enhance=1.05,
        use_adaptive_saturate=False,
        adaptive_vivid=False,
        scale_chroma=False,
    )


# ── Default renderer (lazy init to avoid circular import at module load) ─────

_default_renderer = None


def _get_renderer():
    global _default_renderer
    if _default_renderer is None:
        from hokku_server.image_renderer import ImageRenderer
        _default_renderer = ImageRenderer()
    return _default_renderer


# ── Public render helpers ─────────────────────────────────────────────────────

def render_panel_bytes(
    img: Image.Image,
    cfg: ImageConfig,
    orientation: Orientation,
    crop_to_fill_threshold: float = 0.0,
    *,
    unconstrained: bool = False,
) -> bytes:
    """Full-resolution panel buffer → wire bytes.

    ``unconstrained=True`` switches to UnconstrainedDither (full-canvas float32,
    ~230 MB peak).  Intended only for the side-by-side quality test.
    """
    if unconstrained:
        from hokku_server.dither_unconstrained import UnconstrainedDither
        from hokku_server.image_renderer import ImageRenderer
        return ImageRenderer(UnconstrainedDither()).render_panel_bytes(
            img, cfg, orientation, crop_to_fill_threshold,
        )
    return _get_renderer().render_panel_bytes(img, cfg, orientation, crop_to_fill_threshold)


def render_preview_png(
    img: Image.Image,
    cfg: ImageConfig,
    orientation: Orientation,
    max_side_px: int = 800,
    crop_to_fill_threshold: float = 0.0,
) -> bytes:
    """Smaller panel buffer → PNG preview."""
    return _get_renderer().render_preview_png(
        img, cfg, orientation, max_side_px, crop_to_fill_threshold,
    )


def preview_png_from_panel_bytes(panel_bytes: bytes, orientation: Orientation) -> bytes:
    """Decode an already-rendered panel binary back to a PNG preview."""
    from hokku_server.display import indices_to_preview_rgb
    idx = panel_bytes_to_indices(panel_bytes)
    return _encode_panel_rgb_to_png(indices_to_preview_rgb(idx), orientation)


# ── File-based wrappers ───────────────────────────────────────────────────────

_MAX_SOURCE_LONG_SIDE = max(FULL_W, PANEL_H)


def open_image_for_render(path: Path) -> Image.Image:
    """PIL.open + EXIF transpose + RGB convert + size cap.  Caller closes."""
    img = Image.open(path)
    w0, h0 = img.size
    long0 = max(w0, h0)
    if long0 > _MAX_SOURCE_LONG_SIDE:
        k = 1
        while long0 / (k * 2) >= _MAX_SOURCE_LONG_SIDE / 2 and k < 8:
            k *= 2
        if k > 1:
            try:
                img.draft("RGB", (w0 // k, h0 // k))
            except (AttributeError, OSError):
                pass
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    if max(img.size) > _MAX_SOURCE_LONG_SIDE:
        img.thumbnail(
            (_MAX_SOURCE_LONG_SIDE, _MAX_SOURCE_LONG_SIDE), Image.LANCZOS,
        )
    return img


def _render_indices(
    img: Image.Image,
    cfg: ImageConfig,
    orientation: Orientation,
    canvas_w: int,
    canvas_h: int,
    crop_to_fill_threshold: float = 0.0,
    *,
    release_input: bool = False,
    unconstrained: bool = False,
) -> NDArray[np.uint8]:
    """Backward-compatible shim: delegates to ImageRenderer.render_indices."""
    if unconstrained:
        from hokku_server.dither_unconstrained import UnconstrainedDither
        from hokku_server.image_renderer import ImageRenderer
        renderer: Any = ImageRenderer(UnconstrainedDither())
    else:
        renderer = _get_renderer()
    return renderer.render_indices(
        img, cfg, orientation, canvas_w, canvas_h,
        crop_to_fill_threshold, release_input=release_input,
    )


def render_panel_bytes_from_path(
    path: Path,
    cfg: ImageConfig,
    orientation: Orientation,
) -> bytes:
    """Full convert: open file → render full panel bytes.  Logs progress."""
    print(f"Converting: {path.name}")
    t0 = time.time()
    img = open_image_for_render(path)
    print(f"  {path.name}: {img.size[0]}x{img.size[1]}")
    raw = render_panel_bytes(img, cfg, orientation)
    print(f"  {path.name}: done in {time.time() - t0:.1f}s")
    return raw
