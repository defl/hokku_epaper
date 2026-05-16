"""ImageRenderer: production image renderer backed by a pluggable dither strategy.

Accepts any ``AbstractDither`` instance at construction.  Use ``NumbaStreamingDither()``
for the default path (same ≤50 MB rolling-window memory model as ``StreamingDither``
but with a Numba-JIT inner loop that releases the GIL and runs at native speed),
or ``UnconstrainedDither()`` for the full-canvas reference path.

Usage::

    from hokku_server.image_renderer import ImageRenderer
    from hokku_server.dither_streaming_numba import NumbaStreamingDither

    renderer = ImageRenderer(NumbaStreamingDither())
    panel_bytes = renderer.render_panel_bytes(img, cfg, "landscape")
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageOps

from hokku_server.bounding_box import BoundingBox
from hokku_server.dither_abc import AbstractDither
from hokku_server.dither_streaming import PALETTE_LAB, adaptive_saturate, rgb_to_lab
from hokku_server.dither_streaming_numba import NumbaStreamingDither
from hokku_server.image_abc import (
    AbstractImageRenderer,
    Orientation,
    _encode_panel_rgb_to_png,
    _preview_canvas_dims,
)
from hokku_server.image_config import ImageConfig, Orientation


IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp", ".gif",
    ".heic", ".heif", ".avif", ".jxl",
}

# Hard cap on decoded pixel count. Anything above raises
# PIL.Image.DecompressionBombError from .load()/.convert(). Sized to comfortably
# fit 8K (33 MP) photos while keeping a decoded RGB buffer under ~120 MB —
# safe for a Raspberry Pi.
MAX_IMAGE_PIXELS = 40_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

# Upload-time caps. Pixel cap matches the decode cap; byte cap is a coarse
# first line of defense before we even decode the header.
MAX_UPLOAD_PIXELS = MAX_IMAGE_PIXELS
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB

GRAYSCALE_CHROMA_THRESHOLD = 8.0


def _lab_to_rgb(lab) -> NDArray[np.float32]:
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
    xyz_out[..., 0] = np.where(fx3 > eps, fx3, (f32(116) * fx - f32(16)) / kappa) * ref[0]
    xyz_out[..., 1] = np.where(L > kappa * eps, ((L + f32(16)) / f32(116)) ** 3, L / kappa) * ref[1]
    xyz_out[..., 2] = np.where(fz3 > eps, fz3, (f32(116) * fz - f32(16)) / kappa) * ref[2]
    M_inv = np.array([
        [3.2404542, -1.5371385, -0.4985314],
        [-0.9692660, 1.8760108, 0.0415560],
        [0.0556434, -0.2040259, 1.0572252],
    ], dtype=f32)
    linear = np.clip(xyz_out @ M_inv.T, f32(0), f32(1))
    srgb = np.where(linear <= f32(0.0031308), linear * f32(12.92),
                    f32(1.055) * (linear ** f32(1.0 / 2.4)) - f32(0.055))
    return np.clip(srgb * f32(255), f32(0), f32(255))


def compress_dynamic_range(
    img_array,
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
    black_L = f32(PALETTE_LAB[0, 0])
    white_L = f32(PALETTE_LAB[1, 0])
    c_ratio = f32((float(white_L) - float(black_L)) / 100.0)
    np.multiply(L, f32((float(white_L) - float(black_L)) / 100.0), out=L)
    np.add(L, black_L, out=L)

    # Soft highlight rolloff: tanh shoulder for the top 15% of the L* range.
    # Prevents near-white regions from hard-clipping to the panel's white ink;
    # always on — no config flag needed (monotone, transparent when not clipping).
    threshold = black_L + f32(0.85) * (white_L - black_L)
    headroom = white_L - threshold
    above = L > threshold
    if np.any(above):
        delta = L[above] - threshold
        L[above] = (threshold + headroom * np.tanh(delta / headroom)).astype(f32)

    if adaptive_vivid:
        chroma = np.sqrt(a * a + b_ch * b_ch)
        t = np.clip((chroma - f32(vivid_chroma_low)) / f32(vivid_chroma_high - vivid_chroma_low), f32(0.0), f32(1.0))
        c_factor = c_ratio + (f32(1.0) - c_ratio) * t
        np.multiply(a, c_factor, out=a)
        np.multiply(b_ch, c_factor, out=b_ch)
    elif scale_chroma:
        np.multiply(a, c_ratio, out=a)
        np.multiply(b_ch, c_ratio, out=b_ch)
    return _lab_to_rgb(lab)


_MAX_SOURCE_LONG_SIDE = max(3200, 1800)  # FULL_W, PANEL_H values

# Screen geometry (the panel is 1200x1600; viewed landscape that's 1600x1200).
# A source image carrying more than 2x screen pixels in BOTH directions has
# more detail than dithering can possibly use — and on a Pi those extra
# pixels are just RAM pressure waiting to OOM. Pre-shrink to this bbox so
# decoded buffers stay bounded regardless of source size.
from hokku_server.display import FULL_W as _SCREEN_W, PANEL_H as _SCREEN_H
_SCREEN_LONG = max(_SCREEN_W, _SCREEN_H)
_SCREEN_SHORT = min(_SCREEN_W, _SCREEN_H)
MAX_SOURCE_LONG = 2 * _SCREEN_LONG    # 3200
MAX_SOURCE_SHORT = 2 * _SCREEN_SHORT  # 2400


def open_image_for_render(path: Path) -> Image.Image:
    """PIL.open + EXIF transpose + RGB convert + size cap.  Caller closes.

    Raises ``ValueError`` if the source exceeds ``MAX_IMAGE_PIXELS`` — protects
    the Pi from decompression-bomb PNGs (small file, huge declared dimensions)
    that would otherwise blow up RAM on ``convert("RGB")``.

    Also shrinks sources that exceed 2x screen resolution in *both* directions
    down to a 2x-screen bbox.  Skipped for sources that are only oversized in
    one direction (e.g. a tall thin portrait) so we don't throw away detail
    along the short axis.
    """
    img = Image.open(path)
    w0, h0 = img.size
    if w0 * h0 > MAX_IMAGE_PIXELS:
        img.close()
        raise ValueError(
            f"image {path.name} is too large: {w0}x{h0} "
            f"({w0 * h0:,} px) exceeds cap of {MAX_IMAGE_PIXELS:,} px"
        )
    img_long = max(w0, h0)
    img_short = min(w0, h0)
    oversize_both = img_long > MAX_SOURCE_LONG and img_short > MAX_SOURCE_SHORT
    # Cheap JPEG-only header-time downscale (no-op for PNG/HEIC/etc.) — keeps
    # the decoded buffer small for huge JPEGs before we ever load pixels.
    long0 = img_long
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
    try:
        img = img.convert("RGB")
    except Image.DecompressionBombError as exc:
        raise ValueError(f"image {path.name} is too large to decode") from exc
    if oversize_both:
        # Match orientation: thumbnail uses (w_cap, h_cap) so route long/short.
        w_cap, h_cap = (
            (MAX_SOURCE_LONG, MAX_SOURCE_SHORT)
            if img.size[0] >= img.size[1]
            else (MAX_SOURCE_SHORT, MAX_SOURCE_LONG)
        )
        img.thumbnail((w_cap, h_cap), Image.Resampling.LANCZOS)
    elif max(img.size) > _MAX_SOURCE_LONG_SIDE:
        img.thumbnail((_MAX_SOURCE_LONG_SIDE, _MAX_SOURCE_LONG_SIDE), Image.Resampling.LANCZOS)
    return img


def render_panel_bytes_from_path(
    path: Path,
    cfg,
    orientation: str,
    dither=None,
) -> bytes:
    """Full convert: open file → render full panel bytes.  Logs progress."""
    print(f"Converting: {path.name}")
    t0 = time.time()
    img = open_image_for_render(path)
    print(f"  {path.name}: {img.size[0]}x{img.size[1]}")
    if dither is None:
        dither = NumbaStreamingDither()
    renderer = ImageRenderer(dither)
    raw = renderer.render_panel_bytes(img, cfg, orientation)
    print(f"  {path.name}: done in {time.time() - t0:.1f}s")
    return raw


class ImageRenderer(AbstractImageRenderer):
    """Production renderer: fit/crop → enhancements → dither strategy.

    Parameters
    ----------
    dither:
        Any ``AbstractDither`` implementation.  Required — no default.
        Typical choices: ``StreamingDither()``, ``NumbaStreamingDither()``,
        ``UnconstrainedDither()``.
    """

    def __init__(self, dither: AbstractDither) -> None:
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
        clahe_keepout_bboxes_norm: "tuple[BoundingBox, ...] | None" = None,
    ) -> np.ndarray:
        arr, padding_mask = self._prepare_canvas(
            img, cfg, orientation, canvas_w, canvas_h,
            crop_to_fill_threshold, release_input=release_input,
            clahe_keepout_bboxes_norm=clahe_keepout_bboxes_norm,
        )

        use_sat = cfg.use_adaptive_saturate
        sat_max = cfg.saturate_max_enhance
        sat_lo = cfg.saturate_low_chroma_thresh
        sat_hi = cfg.saturate_high_chroma_thresh
        noise_std = cfg.dither_noise

        def _prep_stripe(stripe_uint8):
            if use_sat:
                f32 = adaptive_saturate(stripe_uint8, sat_max, sat_lo, sat_hi)
            else:
                f32 = stripe_uint8.astype(np.float32)
            f32 = compress_dynamic_range(
                f32,
                scale_chroma=cfg.scale_chroma,
                adaptive_vivid=cfg.adaptive_vivid,
                vivid_chroma_low=cfg.vivid_chroma_low,
                vivid_chroma_high=cfg.vivid_chroma_high,
            )
            if noise_std > 0.0:
                noise = np.random.normal(0.0, noise_std, f32.shape).astype(np.float32)
                f32 = np.clip(f32 + noise, 0.0, 255.0)
            return f32

        result_idx = self._dither.dither_with_prep(arr, cfg.dither, _prep_stripe)
        del arr
        result_idx[padding_mask] = 1
        return result_idx
