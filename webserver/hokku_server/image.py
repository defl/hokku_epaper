"""Image rendering pipeline.

Two public entrypoints:
- render_panel_bytes()   — full-resolution panel, returns wire-format bytes
- render_preview_png()   — smaller PNG of the dithered output for the web GUI

Both go through the same _render_indices() pipeline; only the canvas dims differ.
"""
from __future__ import annotations

import math
import time
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import ArrayLike, NDArray
from PIL import Image, ImageEnhance, ImageOps

from hokku_server.display import (
    FULL_W,
    PANEL_H,
    VISUAL_H,
    VISUAL_W,
    indices_to_panel_bytes,
    indices_to_preview_rgb,
    panel_bytes_to_indices,
)
from hokku_server.dither_constrained import (
    PALETTE_LAB,
    adaptive_saturate,
    dither_with_prep,
    linear_to_xyz,
    rgb_to_lab,
    srgb_to_linear,
    xyz_to_lab,
)
from hokku_server.image_config import ImageConfig, Orientation  # noqa: F401 (re-exported)


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

def _lab_to_rgb(lab: ArrayLike) -> NDArray[np.float32]:
    """float32 Lab → float32 sRGB.  Allocation-conscious: avoids float64
    intermediates so the full-panel Lab buffer fits well under budget."""
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
    """Map source Lab range into the panel's reachable L* range, optionally scaling chroma.

    The Spectra 6 panel's ink whites are dim and ink blacks are not jet black,
    so the displayable L* range is much narrower than [0, 100]. Without this
    compression, mid-tones quantize incorrectly.

    All math is float32 — the visible round-trip error is far below the
    dither quantisation noise and saves ~50 % memory on a full panel.
    """
    f32 = np.float32
    rgb = np.asarray(img_array, dtype=f32)
    lab = rgb_to_lab(rgb, dtype=f32)
    # Reuse the lab buffer as our output Lab — only L (and optionally a/b)
    # change.  This avoids one full-panel float32 allocation.
    L = lab[..., 0]
    a = lab[..., 1]
    b_ch = lab[..., 2]
    black_L = f32(_DISPLAY_BLACK_L)
    white_L = f32(_DISPLAY_WHITE_L)
    c_ratio = f32((_DISPLAY_WHITE_L - _DISPLAY_BLACK_L) / 100.0)
    # In-place L compression.
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


# ── Pipeline ────────────────────────────────────────────────────────

def _apply_prepare_enhancements(canvas: Image.Image, cfg: ImageConfig) -> Image.Image:
    """Autocontrast → gamma → brightness/contrast/sharpness → color.

    Adaptive saturation has been moved out of this function and into the
    streaming ``prep_row`` callback in ``_render_indices`` — it would
    otherwise allocate a full-panel float32 buffer (and several float32
    intermediates) here, blowing the per-render memory budget. PIL
    transforms above run on the uint8 canvas so they each peak at one
    extra 15 MB image (current + new) and recycle quickly.
    """
    canvas = ImageOps.autocontrast(canvas, cutoff=cfg.prepare_autocontrast_cutoff)
    gamma_lut = [int(((i / 255.0) ** cfg.prepare_gamma) * 255) for i in range(256)] * 3
    canvas = canvas.point(gamma_lut)
    canvas = ImageEnhance.Brightness(canvas).enhance(cfg.prepare_brightness)
    canvas = ImageEnhance.Contrast(canvas).enhance(cfg.prepare_contrast)
    canvas = ImageEnhance.Sharpness(canvas).enhance(cfg.prepare_sharpness)
    if not cfg.use_adaptive_saturate:
        # Plain global color enhance — cheap PIL operation.
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
    crop_to_fill_threshold: float = 0.0,
    *,
    release_input: bool = False,
    unconstrained: bool = False,
) -> NDArray[np.uint8]:
    """Fit (or crop-to-fill) → enhance → rotate → DRC → dither → mask padding.

    canvas_w/canvas_h are the *post-rotation* panel-memory buffer dims.
    For full panel use (FULL_W, PANEL_H). For preview, scaled-down versions.

    crop_to_fill_threshold: if the zoom needed to eliminate letterbox bands is
    ≤ this fraction (e.g. 0.02 = 2 %), scale to cover and center-crop instead
    of scaling to fit and padding.  0.0 = always letterbox.

    unconstrained: when True, run adaptive_saturate + compress_dynamic_range
    on the full canvas in one shot and then call the unconstrained
    (full-canvas mutating) dither variants. Intended for the side-by-side
    quality comparison test, NOT for production — the full-canvas float
    path peaks at ~230 MB on a panel render. Default False keeps the
    streaming, ≤ 50 MB-per-render path on by default.
    """
    portrait = orientation == "portrait"

    # Composite at pre-rotation visible dims so aspect-ratio letterboxing is correct.
    # For landscape, panel buffer (canvas_w × canvas_h) is rotated by -90 from visible
    # (visible_w × visible_h) — so visible is (canvas_h × canvas_w).
    visible_w, visible_h = (canvas_w, canvas_h) if portrait else (canvas_h, canvas_w)

    src_w, src_h = img.size
    scale_fit   = min(visible_w / src_w, visible_h / src_h)
    scale_cover = max(visible_w / src_w, visible_h / src_h)
    zoom_ratio  = scale_cover / scale_fit - 1.0  # 0.0 = image exactly fits

    use_cover = crop_to_fill_threshold > 0.0 and zoom_ratio <= crop_to_fill_threshold

    if use_cover:
        # Scale to cover: the shorter panel axis is exactly filled.
        # The longer axis is trimmed symmetrically — no white bands.
        # Use ceil so integer rounding never produces a scaled image that is
        # a pixel short of the canvas (which would leave a white row/column).
        scaled_w = max(visible_w, math.ceil(src_w * scale_cover))
        scaled_h = max(visible_h, math.ceil(src_h * scale_cover))
        img_scaled = img.resize((scaled_w, scaled_h), Image.LANCZOS)
        # Drop the source PIL buffer immediately if the caller has consented
        # — we have the resized canvas and don't need the original pixels.
        # Saves a double-held image during the rest of the pipeline.
        if release_input:
            img.close()
        x_off = (scaled_w - visible_w) // 2
        y_off = (scaled_h - visible_h) // 2
        composed = img_scaled.crop((x_off, y_off, x_off + visible_w, y_off + visible_h))
        img_scaled.close()
        padding_mask = np.zeros((visible_h, visible_w), dtype=bool)  # no padding at all
    else:
        # Scale to fit: image fits entirely, padded with white.
        scale = scale_fit
        new_w, new_h = int(src_w * scale), int(src_h * scale)
        img_resized = img.resize((new_w, new_h), Image.LANCZOS)
        if release_input:
            img.close()  # source no longer needed; release PIL buffer
        composed = Image.new("RGB", (visible_w, visible_h), (255, 255, 255))
        x_off = (visible_w - new_w) // 2
        y_off = (visible_h - new_h) // 2
        composed.paste(img_resized, (x_off, y_off))
        img_resized.close()  # paste copies pixels; resized buffer can go
        padding_mask = np.ones((visible_h, visible_w), dtype=bool)
        padding_mask[y_off:y_off + new_h, x_off:x_off + new_w] = False

    composed = _apply_prepare_enhancements(composed, cfg)

    if not portrait:
        composed = composed.rotate(-90, expand=True)
        padding_mask = np.rot90(padding_mask, k=3)

    # Take a uint8 numpy view of the PIL canvas (no copy). The streaming
    # dither will pull rows on demand and run DRC per-row via prep_row,
    # so we never materialise a full-panel float buffer.
    arr = np.asarray(composed, dtype=np.uint8)
    composed = None  # noqa: F841 (drop reference; PIL buffer can be released)

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

    if unconstrained:
        # Full-canvas pipeline (the "unconstrained" path): saturate + DRC
        # are applied to the entire panel at once and the unconstrained
        # dither mutates a full-canvas float32 buffer.  Peak memory ~60 MB.
        # Strictly for offline / side-by-side quality comparison.
        from hokku_server.dither_unconstrained import dither as _dither_unc
        if use_sat:
            f32 = adaptive_saturate(arr, sat_max, sat_lo, sat_hi)
        else:
            f32 = arr.astype(np.float32)
        del arr
        f32 = compress_dynamic_range(f32, **drc_kwargs)
        result_idx = _dither_unc(f32, cfg.dither)
        del f32
    else:
        def _prep_stripe(stripe_uint8):
            # stripe_uint8 is shape (stripe_h, W, 3), uint8.  We return a
            # fresh float32 array of the same shape with adaptive_saturate
            # + DRC applied.  Transient buffers inside saturate / DRC are
            # sized to the stripe (~3.8 MB at 100 rows × 3200 wide × 3 ch
            # × 4 bytes), so each function's peak is well under 20 MB and
            # the streaming dither holds at most one cached stripe at a
            # time.
            if use_sat:
                f32 = adaptive_saturate(stripe_uint8, sat_max, sat_lo, sat_hi)
            else:
                f32 = stripe_uint8.astype(np.float32)
            return compress_dynamic_range(f32, **drc_kwargs)

        result_idx = dither_with_prep(arr, cfg.dither, _prep_stripe)
        del arr
    # White out the padding so it can't get speckled by enhancement chains.
    result_idx[padding_mask] = 1
    return result_idx


# ── Public render helpers ──────────────────────────────────────────

def render_panel_bytes(
    img: Image.Image,
    cfg: ImageConfig,
    orientation: Orientation,
    crop_to_fill_threshold: float = 0.0,
    *,
    unconstrained: bool = False,
) -> bytes:
    """Full-resolution panel buffer → wire bytes.

    Renders with exactly the ``cfg`` provided — no hidden B&W fallback.
    Callers that want B&W-safe rendering should use an appropriate ``ImageConfig``
    (e.g. obtained from ``ImageClassifier.screen_config_for()``).

    The source image's PIL buffer is released as soon as we have the resized
    panel canvas (the long-running per-render memory budget depends on this).
    Callers must NOT use ``img`` after this function returns.

    ``unconstrained=True`` switches to the full-canvas, memory-unconstrained
    dither path. Peak ~230 MB. Useful only for the side-by-side quality
    comparison test in ``test_dither_quality.py`` — production code should
    leave this False to keep within the 50 MB / render budget.
    """
    result_idx = _render_indices(
        img, cfg, orientation, FULL_W, PANEL_H,
        crop_to_fill_threshold, release_input=True,
        unconstrained=unconstrained,
    )
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
    crop_to_fill_threshold: float = 0.0,
) -> bytes:
    """Smaller panel buffer → PNG of the dithered preview.

    PNG (not JPEG) because each pixel is already snapped to a palette colour
    and JPEG's chroma subsampling would smear them.

    Renders with exactly the ``cfg`` provided — no hidden B&W fallback.
    """
    cw, ch = _preview_canvas_dims(orientation, max_side_px)
    result_idx = _render_indices(img, cfg, orientation, cw, ch, crop_to_fill_threshold)
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

# Source-image dimensions are capped before the pipeline runs so a 6000×4000
# JPEG can't blow the per-render memory budget on its own. 1 × the larger
# panel axis (long edge FULL_W=3200) keeps the decoded source ≤ ~15 MB of
# uint8 RGB; the 25 % "oversampling" room a Lanczos resize would prefer is
# only relevant for crisp downsampling from massively larger sources, and
# our JPEG draft already preserves full sub-pixel detail in the decode path.
_MAX_SOURCE_LONG_SIDE = max(FULL_W, PANEL_H)


def open_image_for_render(path: Path) -> Image.Image:
    """PIL.open + EXIF transpose + RGB convert + size cap. Caller closes.

    For oversized sources we shrink the image before returning, so the rest
    of the pipeline never sees a buffer larger than ~24 MB of decoded RGB
    (uint8). For JPEG we ask the decoder to downsample by an integer power
    of two during decode via ``Image.draft`` — the full source pixels are
    never materialised.  For other formats (PNG, HEIC, WebP, …) we fall
    back to ``thumbnail`` after open, which has higher transient peak.
    """
    img = Image.open(path)
    w0, h0 = img.size
    long0 = max(w0, h0)
    # Aggressive JPEG draft: PIL only honours powers of two (1, 1/2, 1/4, 1/8)
    # and is conservative — calling ``draft`` with target = MAX won't pick a
    # smaller scale unless the half-size result is still ≥ MAX in both dims.
    # Compute the largest k ∈ {2, 4, 8} that still leaves us above MAX, and
    # ask draft for size/k explicitly so the decoder downsamples in flight.
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
