"""Image rendering pipeline.

Two public entrypoints:
- render_panel_bytes()   — full-resolution panel, returns wire-format bytes
- render_preview_png()   — smaller PNG of the dithered output for the web GUI

Both go through the same _render_indices() pipeline; only the canvas dims differ.
"""
from __future__ import annotations

import time
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import ArrayLike, NDArray
from PIL import Image, ImageEnhance, ImageOps

from webserver.display import (
    FULL_W,
    PANEL_H,
    VISUAL_H,
    VISUAL_W,
    indices_to_panel_bytes,
    indices_to_preview_rgb,
    panel_bytes_to_indices,
)
from webserver.dither import (
    PALETTE_LAB,
    adaptive_saturate,
    dither,
    linear_to_xyz,
    rgb_to_lab,
    srgb_to_linear,
    xyz_to_lab,
)
from webserver.image_config import ImageConfig, Orientation  # noqa: F401 (re-exported)


IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp", ".gif",
    ".heic", ".heif", ".avif",
}

GRAYSCALE_CHROMA_THRESHOLD = 8.0

# L* of the on-panel black and white inks — used by compress_dynamic_range
# to map the source image's full Lab range into what the panel can actually show.
_DISPLAY_BLACK_L = float(PALETTE_LAB[0, 0])
_DISPLAY_WHITE_L = float(PALETTE_LAB[1, 0])


# ── Lab → RGB (for compress_dynamic_range) ─────────────────────────

def _lab_to_rgb(lab: ArrayLike) -> NDArray[Any]:
    lab = np.asarray(lab, dtype=np.float64)
    ref = np.array([0.95047, 1.00000, 1.08883])
    L, a, b_ch = lab[..., 0], lab[..., 1], lab[..., 2]
    fy = (L + 16) / 116.0
    fx = a / 500.0 + fy
    fz = fy - b_ch / 200.0
    eps = 0.008856
    kappa = 903.3
    xyz_out = np.zeros_like(lab)
    xyz_out[..., 0] = np.where(fx ** 3 > eps, fx ** 3, (116 * fx - 16) / kappa) * ref[0]
    xyz_out[..., 1] = np.where(L > kappa * eps, ((L + 16) / 116.0) ** 3, L / kappa) * ref[1]
    xyz_out[..., 2] = np.where(fz ** 3 > eps, fz ** 3, (116 * fz - 16) / kappa) * ref[2]
    M_inv = np.array([
        [3.2404542, -1.5371385, -0.4985314],
        [-0.9692660, 1.8760108, 0.0415560],
        [0.0556434, -0.2040259, 1.0572252],
    ])
    linear = np.clip(xyz_out @ M_inv.T, 0, 1)
    srgb = np.where(linear <= 0.0031308, linear * 12.92,
                    1.055 * (linear ** (1.0 / 2.4)) - 0.055)
    return np.clip(srgb * 255, 0, 255)


def compress_dynamic_range(
    img_array: ArrayLike,
    *,
    scale_chroma: bool,
    adaptive_vivid: bool,
    vivid_chroma_low: float,
    vivid_chroma_high: float,
) -> NDArray[Any]:
    """Map source Lab range into the panel's reachable L* range, optionally scaling chroma.

    The Spectra 6 panel's ink whites are dim and ink blacks are not jet black,
    so the displayable L* range is much narrower than [0, 100]. Without this
    compression, mid-tones quantize incorrectly.
    """
    rgb = np.asarray(img_array, dtype=np.float64)
    lab = rgb_to_lab(rgb)
    L = lab[..., 0]
    a = lab[..., 1]
    b_ch = lab[..., 2]
    L_out = _DISPLAY_BLACK_L + (L / 100.0) * (_DISPLAY_WHITE_L - _DISPLAY_BLACK_L)
    chroma = np.sqrt(a ** 2 + b_ch ** 2)
    c_ratio = (_DISPLAY_WHITE_L - _DISPLAY_BLACK_L) / 100.0
    if adaptive_vivid:
        t = np.clip(
            (chroma - vivid_chroma_low) / (vivid_chroma_high - vivid_chroma_low),
            0.0, 1.0,
        )
        c_factor = c_ratio + (1.0 - c_ratio) * t
        a_out = a * c_factor
        b_out = b_ch * c_factor
    elif scale_chroma:
        a_out = a * c_ratio
        b_out = b_ch * c_ratio
    else:
        a_out = a
        b_out = b_ch
    return _lab_to_rgb(np.stack([L_out, a_out, b_out], axis=-1)).astype(np.float32)


# ── Pipeline ────────────────────────────────────────────────────────

def _apply_prepare_enhancements(canvas: Image.Image, cfg: ImageConfig) -> Image.Image:
    """Autocontrast → gamma → brightness/contrast/sharpness → color or adaptive saturate."""
    canvas = ImageOps.autocontrast(canvas, cutoff=cfg.prepare_autocontrast_cutoff)
    gamma_lut = [int(((i / 255.0) ** cfg.prepare_gamma) * 255) for i in range(256)] * 3
    canvas = canvas.point(gamma_lut)
    canvas = ImageEnhance.Brightness(canvas).enhance(cfg.prepare_brightness)
    canvas = ImageEnhance.Contrast(canvas).enhance(cfg.prepare_contrast)
    canvas = ImageEnhance.Sharpness(canvas).enhance(cfg.prepare_sharpness)
    if cfg.use_adaptive_saturate:
        arr = adaptive_saturate(
            np.array(canvas, dtype=np.float64),
            cfg.saturate_max_enhance,
            cfg.saturate_low_chroma_thresh,
            cfg.saturate_high_chroma_thresh,
        )
        canvas = Image.fromarray(arr.astype(np.uint8))
    else:
        canvas = ImageEnhance.Color(canvas).enhance(cfg.color_enhance)
    return canvas


def _is_near_grayscale(img: Image.Image) -> bool:
    """95th-percentile chroma below threshold ⇒ source is essentially B&W.

    Used to skip saturation boosters that turn faint film grain into colour confetti.
    """
    thumb = img.copy()
    thumb.thumbnail((200, 200), Image.LANCZOS)
    arr = np.asarray(thumb.convert("RGB"), dtype=np.float64)
    lab = xyz_to_lab(linear_to_xyz(srgb_to_linear(arr)))
    chroma = np.sqrt(lab[..., 1] ** 2 + lab[..., 2] ** 2)
    return float(np.percentile(chroma, 95)) < GRAYSCALE_CHROMA_THRESHOLD


# Public alias for use by ImageClassifier.
is_grayscale_image = _is_near_grayscale


def is_grayscale(path: Path) -> bool:
    """Return True iff the image at *path* is essentially monochrome.

    Convenience wrapper that opens the file and delegates to
    ``is_grayscale_image()``. Used by ``ImageClassifier`` to avoid opening
    the same file twice.
    """
    with open_image_for_render(path) as img:
        return is_grayscale_image(img)


def _bw_safe_image_config(cfg: ImageConfig) -> ImageConfig:
    """Disable saturation boosters and chroma scaling for B&W sources."""
    return replace(
        cfg,
        color_enhance=1.05,
        use_adaptive_saturate=False,
        adaptive_vivid=False,
        scale_chroma=False,
    )


def _render_indices(
    img: Image.Image,
    cfg: ImageConfig,
    orientation: Orientation,
    canvas_w: int,
    canvas_h: int,
) -> NDArray[np.uint8]:
    """Letterbox → enhance → rotate → DRC → dither → mask padding.

    canvas_w/canvas_h are the *post-rotation* panel-memory buffer dims.
    For full panel use (FULL_W, PANEL_H). For preview, scaled-down versions.
    """
    portrait = orientation == "portrait"

    # Composite at pre-rotation visible dims so aspect-ratio letterboxing is correct.
    # For landscape, panel buffer (canvas_w × canvas_h) is rotated by -90 from visible
    # (visible_w × visible_h) — so visible is (canvas_h × canvas_w).
    visible_w, visible_h = (canvas_w, canvas_h) if portrait else (canvas_h, canvas_w)

    src_w, src_h = img.size
    scale = min(visible_w / src_w, visible_h / src_h)
    new_w, new_h = int(src_w * scale), int(src_h * scale)
    img_resized = img.resize((new_w, new_h), Image.LANCZOS)

    composed = Image.new("RGB", (visible_w, visible_h), (255, 255, 255))
    x_off = (visible_w - new_w) // 2
    y_off = (visible_h - new_h) // 2
    composed.paste(img_resized, (x_off, y_off))

    padding_mask = np.ones((visible_h, visible_w), dtype=bool)
    padding_mask[y_off:y_off + new_h, x_off:x_off + new_w] = False

    composed = _apply_prepare_enhancements(composed, cfg)

    if not portrait:
        composed = composed.rotate(-90, expand=True)
        padding_mask = np.rot90(padding_mask, k=3)

    arr = np.asarray(composed, dtype=np.float32)
    compressed = compress_dynamic_range(
        arr,
        scale_chroma=cfg.scale_chroma,
        adaptive_vivid=cfg.adaptive_vivid,
        vivid_chroma_low=cfg.vivid_chroma_low,
        vivid_chroma_high=cfg.vivid_chroma_high,
    )
    canvas_d = Image.fromarray(compressed.astype(np.uint8))
    result_idx = dither(canvas_d, cfg.dither)
    # White out the padding so it can't get speckled by enhancement chains.
    result_idx[padding_mask] = 1
    return result_idx


# ── Public render helpers ──────────────────────────────────────────

def render_panel_bytes(img: Image.Image, cfg: ImageConfig, orientation: Orientation) -> bytes:
    """Full-resolution panel buffer → wire bytes.

    Renders with exactly the ``cfg`` provided — no hidden B&W fallback.
    Callers that want B&W-safe rendering should use an appropriate ``ImageConfig``
    (e.g. obtained from ``ImageClassifier.screen_config_for()``).
    """
    result_idx = _render_indices(img, cfg, orientation, FULL_W, PANEL_H)
    return indices_to_panel_bytes(result_idx)


def _preview_canvas_dims(orientation: Orientation, max_side_px: int) -> tuple[int, int]:
    """Scale (FULL_W, PANEL_H) so the longer side is ≤ max_side_px."""
    s = min(1.0, float(max_side_px) / float(max(FULL_W, PANEL_H)))
    cw = max(1, int(FULL_W * s))
    ch = max(1, int(PANEL_H * s))
    return cw, ch


def render_preview_png(
    img: Image.Image,
    cfg: ImageConfig,
    orientation: Orientation,
    max_side_px: int = 800,
) -> bytes:
    """Smaller panel buffer → PNG of the dithered preview.

    PNG (not JPEG) because each pixel is already snapped to a palette colour
    and JPEG's chroma subsampling would smear them.

    Renders with exactly the ``cfg`` provided — no hidden B&W fallback.
    """
    cw, ch = _preview_canvas_dims(orientation, max_side_px)
    result_idx = _render_indices(img, cfg, orientation, cw, ch)
    preview_rgb = indices_to_preview_rgb(result_idx)
    return _encode_panel_rgb_to_png(preview_rgb, orientation)


def _encode_panel_rgb_to_png(panel_rgb: NDArray[np.uint8], orientation: Orientation) -> bytes:
    """Panel-memory RGB → PNG bytes in the visible (browser) orientation."""
    img = Image.fromarray(np.asarray(panel_rgb, dtype=np.uint8))
    if orientation == "landscape":
        img = img.rotate(90, expand=True)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def preview_png_from_panel_bytes(panel_bytes: bytes, orientation: Orientation) -> bytes:
    """Decode an already-rendered panel binary back into a PNG preview."""
    idx = panel_bytes_to_indices(panel_bytes)
    return _encode_panel_rgb_to_png(indices_to_preview_rgb(idx), orientation)


# ── File-based wrappers ────────────────────────────────────────────

def open_image_for_render(path: Path) -> Image.Image:
    """PIL.open + EXIF transpose + RGB convert. Caller closes."""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def render_panel_bytes_from_path(
    path: Path,
    cfg: ImageConfig,
    orientation: Orientation,
) -> bytes:
    """Full convert: open file → render full panel bytes. Logs progress."""
    print(f"Converting: {path.name}")
    t0 = time.time()
    img = open_image_for_render(path)
    print(f"  {path.name}: {img.size[0]}x{img.size[1]}")
    raw = render_panel_bytes(img, cfg, orientation)
    print(f"  {path.name}: done in {time.time() - t0:.1f}s")
    return raw
