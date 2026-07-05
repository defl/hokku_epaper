"""ImageRenderer: production image renderer backed by a pluggable dither strategy.

Accepts any ``AbstractDither`` instance at construction.  Use ``NumbaStreamingDither()``
for the default path (same ≤50 MB rolling-window memory model as ``StreamingDither``
but with a Numba-JIT inner loop that releases the GIL and runs at native speed),
or ``UnconstrainedDither()`` for the full-canvas reference path.

Usage::

    from hokku.webserver.image_renderer import ImageRenderer
    from hokku.webserver.dither_streaming_numba import NumbaStreamingDither

    renderer = ImageRenderer(NumbaStreamingDither())
    panel_bytes = renderer.render_panel_bytes(img, cfg, "landscape")
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import resvg_py
from numpy.typing import NDArray
from PIL import Image, ImageOps

if TYPE_CHECKING:
    from hokku.screens.display import Display

from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.bounding_box import BoundingBox
from hokku.webserver.dither_abc import AbstractDither
from hokku.webserver.dither_streaming import (
    # PALETTE_LAB / PALETTE_OKLAB are the Huessen reference DRC L*-anchors
    # (black/white lightness).  Bigme F7 ink is not yet photographically
    # measured, so its DRC reuses these reference anchors until calibrated.
    PALETTE_LAB,
    PALETTE_OKLAB,
    adaptive_saturate,
    adaptive_saturate_oklab,
    oklab_to_rgb,
    rgb_to_lab,
    rgb_to_oklab,
)
from hokku.webserver.image_abc import AbstractImageRenderer
from hokku.webserver.image_config import DrcSpace, ImageConfig
from hokku.webserver.orientation import Orientation

# Reference panel geometry (Huessen EPF1301) used only to bound the source
# pre-shrink budget below.  It caps decoded-buffer RAM; it does not affect
# output geometry, which comes from the per-render Display instance.
_REFERENCE_DISPLAY = DISPLAY_REGISTRY["huessen_epf1301"]
_SCREEN_W = _REFERENCE_DISPLAY.panel_w
_SCREEN_H = _REFERENCE_DISPLAY.panel_h

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".tiff",
    ".webp",
    ".gif",
    ".heic",
    ".heif",
    ".avif",
    ".jxl",
    ".svg",
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


_MAX_SOURCE_LONG_SIDE = max(3200, 1800)  # FULL_W, PANEL_H values

# Screen geometry (the panel is 1200x1600; viewed landscape that's 1600x1200).
# A source image carrying more than 2x screen pixels in BOTH directions has
# more detail than dithering can possibly use — and on a Pi those extra
# pixels are just RAM pressure waiting to OOM. Pre-shrink to this bbox so
# decoded buffers stay bounded regardless of source size.
_SCREEN_LONG = max(_SCREEN_W, _SCREEN_H)
_SCREEN_SHORT = min(_SCREEN_W, _SCREEN_H)
MAX_SOURCE_LONG = 2 * _SCREEN_LONG  # 3200
MAX_SOURCE_SHORT = 2 * _SCREEN_SHORT  # 2400

# Conservative upper bound used for the upload pixel-budget guard. The actual
# rasterisation uses a square canvas so both orientations get full resolution.
SVG_PROBE_DIMS: tuple[int, int] = (_SCREEN_LONG, _SCREEN_LONG)


def _rasterize_svg(path: Path) -> Image.Image:
    """Rasterise an SVG to a PIL RGB Image, bounded at screen resolution.

    Uses resvg (bundled Rust binary) via resvg_py — no system library required.
    A square canvas (_SCREEN_LONG × _SCREEN_LONG) is used so portrait and
    landscape SVGs both get full-resolution rasterisation regardless of their
    native orientation. resvg preserves aspect ratio, so the output is never
    distorted.
    """
    try:
        png_bytes = resvg_py.svg_to_bytes(
            svg_path=str(path),
            dpi=96,  # CSS-standard DPI; correctly converts pt/mm/cm/in to px
            width=_SCREEN_LONG,
            height=_SCREEN_LONG,
            background="white",  # composite transparency against white, not black
        )
    except Exception as exc:
        raise ValueError(f"SVG rasterisation failed for {path.name}: {exc}") from exc
    return Image.open(io.BytesIO(png_bytes)).convert("RGB")


def open_image_for_render(path: Path) -> Image.Image:
    """PIL.open + EXIF transpose + RGB convert + size cap.  Caller closes.

    SVG files are rasterised via resvg before entering the PIL pipeline.

    Raises ``ValueError`` if the source exceeds ``MAX_IMAGE_PIXELS`` — protects
    the Pi from decompression-bomb PNGs (small file, huge declared dimensions)
    that would otherwise blow up RAM on ``convert("RGB")``.

    Also shrinks sources that exceed 2x screen resolution in *both* directions
    down to a 2x-screen bbox.  Skipped for sources that are only oversized in
    one direction (e.g. a tall thin portrait) so we don't throw away detail
    along the short axis.
    """
    if path.suffix.lower() == ".svg":
        img = _rasterize_svg(path)
    else:
        img = Image.open(path)
        w0, h0 = img.size
        if w0 * h0 > MAX_IMAGE_PIXELS:
            img.close()
            raise ValueError(
                f"image {path.name} is too large: {w0}x{h0} "
                f"({w0 * h0:,} px) exceeds cap of {MAX_IMAGE_PIXELS:,} px"
            )
        img_long = max(w0, h0)
        # Cheap JPEG-only header-time downscale (no-op for PNG/HEIC/etc.) — keeps
        # the decoded buffer small for huge JPEGs before we ever load pixels.
        if img_long > _MAX_SOURCE_LONG_SIDE:
            k = 1
            while img_long / (k * 2) >= _MAX_SOURCE_LONG_SIDE / 2 and k < 8:
                k *= 2
            if k > 1:
                try:
                    img.draft("RGB", (w0 // k, h0 // k))
                except (AttributeError, OSError):
                    pass
        img = ImageOps.exif_transpose(img)
        try:
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                # Flatten transparency onto WHITE so a transparent background
                # becomes white (the panel/letterbox background) rather than the
                # black that a plain convert("RGB") leaves under transparent pixels.
                rgba = img.convert("RGBA")
                bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                img = Image.alpha_composite(bg, rgba).convert("RGB")
            else:
                img = img.convert("RGB")
        except Image.DecompressionBombError as exc:
            raise ValueError(f"image {path.name} is too large to decode") from exc

    # Common size-cap: pre-shrink if oversized in both dimensions (saves RAM on Pi).
    # SVG path: already bounded at screen res by _rasterize_svg; still guarded here.
    img_long = max(img.size)
    img_short = min(img.size)
    oversize_both = img_long > MAX_SOURCE_LONG and img_short > MAX_SOURCE_SHORT
    if oversize_both:
        # Match orientation: thumbnail uses (w_cap, h_cap) so route long/short.
        w_cap, h_cap = (
            (MAX_SOURCE_LONG, MAX_SOURCE_SHORT)
            if img.size[0] >= img.size[1]
            else (MAX_SOURCE_SHORT, MAX_SOURCE_LONG)
        )
        img.thumbnail((w_cap, h_cap), Image.Resampling.LANCZOS)
    elif img_long > _MAX_SOURCE_LONG_SIDE:
        img.thumbnail((_MAX_SOURCE_LONG_SIDE, _MAX_SOURCE_LONG_SIDE), Image.Resampling.LANCZOS)
    return img


class ImageRenderer(AbstractImageRenderer):
    """Production renderer: fit/crop → enhancements → dither strategy.

    Parameters
    ----------
    dither:
        Any ``AbstractDither`` implementation.  Required — no default.
        Typical choices: ``StreamingDither(display)``,
        ``NumbaStreamingDither(display)``, ``UnconstrainedDither(display)``.
    display:
        The target screen model's ``Display`` — supplies panel geometry,
        palette, wire packing, and rotation.  Required — no default.
    """

    def __init__(self, dither: AbstractDither, display: Display) -> None:
        super().__init__(display)
        self._dither = dither

    @staticmethod
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
        fx3 = fx**3
        fz3 = fz**3
        xyz_out[..., 0] = np.where(fx3 > eps, fx3, (f32(116) * fx - f32(16)) / kappa) * ref[0]
        xyz_out[..., 1] = (
            np.where(L > kappa * eps, ((L + f32(16)) / f32(116)) ** 3, L / kappa) * ref[1]
        )
        xyz_out[..., 2] = np.where(fz3 > eps, fz3, (f32(116) * fz - f32(16)) / kappa) * ref[2]
        M_inv = np.array(
            [
                [3.2404542, -1.5371385, -0.4985314],
                [-0.9692660, 1.8760108, 0.0415560],
                [0.0556434, -0.2040259, 1.0572252],
            ],
            dtype=f32,
        )
        linear = np.clip(xyz_out @ M_inv.T, f32(0), f32(1))
        srgb = np.where(
            linear <= f32(0.0031308),
            linear * f32(12.92),
            f32(1.055) * (linear ** f32(1.0 / 2.4)) - f32(0.055),
        )
        return np.clip(srgb * f32(255), f32(0), f32(255))

    @staticmethod
    def compress_dynamic_range(
        img_array,
        *,
        scale_chroma: bool,
        adaptive_vivid: bool,
        vivid_chroma_low: float,
        vivid_chroma_high: float,
        vivid_chroma_low_oklab: float = 0.025,
        vivid_chroma_high_oklab: float = 0.075,
        drc_l_space: DrcSpace = "cielab",
        drc_chroma_space: DrcSpace = "cielab",
    ) -> NDArray[np.float32]:
        """Map source range into the panel's reachable L\\* range.

        L compression and chroma scaling can each run in either CIELAB or
        OKLAB.  When both spaces are the same, the work happens in one pass;
        when they differ, the L stage runs first then we round-trip through
        sRGB into the second space for the chroma stage.
        """
        if drc_l_space not in ("cielab", "oklab"):
            raise ValueError(f"drc_l_space must be 'cielab' or 'oklab', got {drc_l_space!r}")
        if drc_chroma_space not in ("cielab", "oklab"):
            raise ValueError(
                f"drc_chroma_space must be 'cielab' or 'oklab', got {drc_chroma_space!r}"
            )

        f32 = np.float32
        rgb = np.asarray(img_array, dtype=f32)

        # Stage 1: L compression in the requested space.
        if drc_l_space == "cielab":
            rgb = ImageRenderer._drc_cielab_l(rgb)
        else:
            rgb = ImageRenderer._drc_oklab_l(rgb)

        # Stage 2: chroma scaling in the requested space.
        if drc_chroma_space == "cielab":
            return ImageRenderer._drc_cielab_chroma(
                rgb,
                scale_chroma=scale_chroma,
                adaptive_vivid=adaptive_vivid,
                vivid_chroma_low=vivid_chroma_low,
                vivid_chroma_high=vivid_chroma_high,
            )
        return ImageRenderer._drc_oklab_chroma(
            rgb,
            scale_chroma=scale_chroma,
            adaptive_vivid=adaptive_vivid,
            vivid_chroma_low=vivid_chroma_low_oklab,
            vivid_chroma_high=vivid_chroma_high_oklab,
        )

    @staticmethod
    def _drc_cielab_l(rgb: NDArray[np.float32]) -> NDArray[np.float32]:
        """Map source L* into the panel's CIELAB L* range + tanh soft shoulder."""
        f32 = np.float32
        lab = rgb_to_lab(rgb, dtype=f32)
        L = lab[..., 0]
        black_L = f32(PALETTE_LAB[0, 0])
        white_L = f32(PALETTE_LAB[1, 0])
        ratio = f32((float(white_L) - float(black_L)) / 100.0)
        np.multiply(L, ratio, out=L)
        np.add(L, black_L, out=L)
        threshold = black_L + f32(0.85) * (white_L - black_L)
        headroom = white_L - threshold
        above = L > threshold
        if np.any(above):
            delta = L[above] - threshold
            L[above] = (threshold + headroom * np.tanh(delta / headroom)).astype(f32)
        return ImageRenderer._lab_to_rgb(lab)

    @staticmethod
    def _drc_oklab_l(rgb: NDArray[np.float32]) -> NDArray[np.float32]:
        """Map source L into the panel's OKLAB L range + tanh soft shoulder.

        Panel anchors come from PALETTE_OKLAB (black L ≈ 0.085, white ≈ 0.825).
        OKLAB has noticeably better perceived-lightness prediction than CIELAB
        (Bottosson 2020), so the soft shoulder follows perceived brightness
        more faithfully near the panel-white limit.
        """
        f32 = np.float32
        oklab = rgb_to_oklab(rgb, dtype=f32)
        L = oklab[..., 0]
        black_L = f32(PALETTE_OKLAB[0, 0])
        white_L = f32(PALETTE_OKLAB[1, 0])
        # Source L is in [0, 1] in OKLAB — scale to [black_L, white_L].
        ratio = white_L - black_L
        np.multiply(L, ratio, out=L)
        np.add(L, black_L, out=L)
        threshold = black_L + f32(0.85) * (white_L - black_L)
        headroom = white_L - threshold
        above = L > threshold
        if np.any(above):
            delta = L[above] - threshold
            L[above] = (threshold + headroom * np.tanh(delta / headroom)).astype(f32)
        return oklab_to_rgb(oklab, dtype=f32)

    @staticmethod
    def _drc_cielab_chroma(
        rgb: NDArray[np.float32],
        *,
        scale_chroma: bool,
        adaptive_vivid: bool,
        vivid_chroma_low: float,
        vivid_chroma_high: float,
    ) -> NDArray[np.float32]:
        f32 = np.float32
        if not (scale_chroma or adaptive_vivid):
            return rgb
        lab = rgb_to_lab(rgb, dtype=f32)
        a = lab[..., 1]
        b_ch = lab[..., 2]
        black_L = f32(PALETTE_LAB[0, 0])
        white_L = f32(PALETTE_LAB[1, 0])
        c_ratio = f32((float(white_L) - float(black_L)) / 100.0)
        if adaptive_vivid:
            chroma = np.sqrt(a * a + b_ch * b_ch)
            span = f32(vivid_chroma_high - vivid_chroma_low)
            if span <= 0:
                span = f32(1e-6)
            t = np.clip((chroma - f32(vivid_chroma_low)) / span, f32(0.0), f32(1.0))
            c_factor = c_ratio + (f32(1.0) - c_ratio) * t
            np.multiply(a, c_factor, out=a)
            np.multiply(b_ch, c_factor, out=b_ch)
        elif scale_chroma:
            np.multiply(a, c_ratio, out=a)
            np.multiply(b_ch, c_ratio, out=b_ch)
        return ImageRenderer._lab_to_rgb(lab)

    @staticmethod
    def _drc_oklab_chroma(
        rgb: NDArray[np.float32],
        *,
        scale_chroma: bool,
        adaptive_vivid: bool,
        vivid_chroma_low: float,
        vivid_chroma_high: float,
    ) -> NDArray[np.float32]:
        f32 = np.float32
        if not (scale_chroma or adaptive_vivid):
            return rgb
        oklab = rgb_to_oklab(rgb, dtype=f32)
        a = oklab[..., 1]
        b_ch = oklab[..., 2]
        black_L = f32(PALETTE_OKLAB[0, 0])
        white_L = f32(PALETTE_OKLAB[1, 0])
        # c_ratio is the same fractional compression as the CIELAB path —
        # both anchors live on a [0, 1]-ish L axis in OKLAB.
        c_ratio = f32((float(white_L) - float(black_L)) / 1.0)
        if adaptive_vivid:
            chroma = np.sqrt(a * a + b_ch * b_ch)
            span = f32(vivid_chroma_high - vivid_chroma_low)
            if span <= 0:
                span = f32(1e-6)
            t = np.clip((chroma - f32(vivid_chroma_low)) / span, f32(0.0), f32(1.0))
            c_factor = c_ratio + (f32(1.0) - c_ratio) * t
            np.multiply(a, c_factor, out=a)
            np.multiply(b_ch, c_factor, out=b_ch)
        elif scale_chroma:
            np.multiply(a, c_ratio, out=a)
            np.multiply(b_ch, c_ratio, out=b_ch)
        return oklab_to_rgb(oklab, dtype=f32)

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
        clahe_keepout_bboxes_norm: tuple[BoundingBox, ...] | None = None,
    ) -> np.ndarray:
        arr, padding_mask = self._prepare_canvas(
            img,
            cfg,
            orientation,
            canvas_w,
            canvas_h,
            crop_to_fill_threshold,
            release_input=release_input,
            clahe_keepout_bboxes_norm=clahe_keepout_bboxes_norm,
        )

        sat_space = cfg.adaptive_saturate_space
        sat_max = cfg.saturate_max_enhance
        sat_lo_cielab = cfg.saturate_low_chroma_thresh
        sat_hi_cielab = cfg.saturate_high_chroma_thresh
        sat_lo_oklab = cfg.saturate_low_chroma_thresh_oklab
        sat_hi_oklab = cfg.saturate_high_chroma_thresh_oklab
        noise_std = cfg.dither_noise

        def _prep_stripe(stripe_uint8):
            if sat_space == "cielab":
                f32 = adaptive_saturate(stripe_uint8, sat_max, sat_lo_cielab, sat_hi_cielab)
            elif sat_space == "oklab":
                f32 = adaptive_saturate_oklab(stripe_uint8, sat_max, sat_lo_oklab, sat_hi_oklab)
            else:  # "off"
                f32 = stripe_uint8.astype(np.float32)
            f32 = ImageRenderer.compress_dynamic_range(
                f32,
                scale_chroma=cfg.scale_chroma,
                adaptive_vivid=cfg.adaptive_vivid,
                vivid_chroma_low=cfg.vivid_chroma_low,
                vivid_chroma_high=cfg.vivid_chroma_high,
                vivid_chroma_low_oklab=cfg.vivid_chroma_low_oklab,
                vivid_chroma_high_oklab=cfg.vivid_chroma_high_oklab,
                drc_l_space=cfg.drc_l_space,
                drc_chroma_space=cfg.drc_chroma_space,
            )
            if noise_std > 0.0:
                noise = np.random.normal(0.0, noise_std, f32.shape).astype(np.float32)
                f32 = np.clip(f32 + noise, 0.0, 255.0)
            return f32

        result_idx = self._dither.dither_with_prep(arr, cfg.dither, _prep_stripe)
        del arr
        result_idx[padding_mask] = 1
        return result_idx
