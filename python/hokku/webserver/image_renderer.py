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
import threading
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

# Hard cap on *declared* pixel count — the decompression-bomb guard. A file
# whose header claims more than this is refused at open(), before a single pixel
# is decoded (a few-hundred-byte PNG can declare 20000x20000). Kept high so it
# only catches genuine bombs; the real per-decode RAM ceiling is
# DECODE_BUDGET_PIXELS below.
MAX_IMAGE_PIXELS = 40_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

# The RAM ceiling that actually matters: how many pixels we are willing to
# *materialise*. A full-resolution decode of N pixels costs ~N*3 bytes for RGB,
# and 2–3x that transiently for HEIF (libheif working memory) — a 24 MP HEIF
# peaked ~170 MB on Debian.
#
# This is DERIVED FROM THE MEMORY BUDGET at startup (see resource_budget.py) and
# installed via set_decode_budget_pixels(): a 464 MB Pi resolves to ~16 MP, a
# 1 GB+ box to the full bomb cap (so a 38.9 MP panorama renders). The value here
# is only the default used until the server configures it (and by unit tests).
#
# Crucially this is checked AFTER draft() (see _open_image_for_render): a huge
# JPEG shrink-on-loads to well under the budget and is accepted, while an
# un-draftable format (PNG / HEIF / etc.) that would decode above the budget is
# refused with a clear message instead of OOM-killing the whole server.
DECODE_BUDGET_PIXELS = 16_000_000


def set_decode_budget_pixels(pixels: int) -> None:
    """Install the runtime decode budget (see resource_budget.compute_budget).

    Called once at startup and on every config reload. The gate functions
    (:func:`decoded_pixels_exceed_budget`, :func:`_open_image_for_render`) read
    this module global at call time, so a reload takes effect immediately.
    """
    global DECODE_BUDGET_PIXELS
    DECODE_BUDGET_PIXELS = int(pixels)


def format_megapixels(pixels: int) -> str:
    """Format a pixel count as megapixels, e.g. ``38.9 MP`` — the unit users see
    in the config (memory budget) and tooltips, so error messages match."""
    return f"{pixels / 1_000_000:.1f} MP"


def decode_budget_error(width: int, height: int) -> str:
    """User-facing reason an image exceeds the runtime decode budget.

    Reported in megapixels (matching the config / GUI) with the raw WxH kept for
    reference, plus the current budget so users know the target to downscale to.
    """
    return (
        f"too large to render on this device: {format_megapixels(width * height)} "
        f"({width}x{height}), over the {format_megapixels(DECODE_BUDGET_PIXELS)} decode "
        f"budget — downscale it, or re-save as JPEG (which decodes at reduced scale)"
    )


# Upload-time caps. Pixel cap matches the bomb guard (the decode budget is
# enforced per-format at render time, post-draft); byte cap is a coarse first
# line of defense before we even decode the header.
MAX_UPLOAD_PIXELS = MAX_IMAGE_PIXELS
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB

GRAYSCALE_CHROMA_THRESHOLD = 8.0


# Screen geometry (the panel is 1200x1600; viewed landscape that's 1600x1200).
# Source images are pre-shrunk to this bbox: enough oversampling for crop-to-fill
# quality, but no more — extra source pixels are just RAM pressure and resize
# temp waiting to OOM on the Pi. 1.5x screen (down from an earlier 2x) roughly
# halves the LANCZOS resize temp with no visible change at panel resolution.
_SCREEN_LONG = max(_SCREEN_W, _SCREEN_H)
_SCREEN_SHORT = min(_SCREEN_W, _SCREEN_H)
_SOURCE_SCREEN_MULT = 1.5
MAX_SOURCE_LONG = int(_SOURCE_SCREEN_MULT * _SCREEN_LONG)  # 2400
MAX_SOURCE_SHORT = int(_SOURCE_SCREEN_MULT * _SCREEN_SHORT)  # 1800
_MAX_SOURCE_LONG_SIDE = MAX_SOURCE_LONG  # single-axis (panorama/portrait) clamp

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


# Image DECODING is bounded across render threads two independent ways:
#
#   1. MEMORY — every decode (any format) takes a permit from _DECODE_SEMAPHORE,
#      whose size is derived from the memory budget (see
#      resource_budget.resolve_decode_concurrency) and installed via
#      set_decode_concurrency(). At most K decodes are ever in flight, and the
#      budget picks K so K*decode_peak stays within RAM. K=1 (a 1-worker box like
#      the Pi) fully serializes decode — identical to the old global lock; K>1
#      lets ordinary JPEG/PNG uploads decode in parallel instead of queuing behind
#      one decode, without risking OOM on multi-worker boxes.
#
#   2. THREAD-SAFETY — the native container codecs in _NATIVE_DECODE_SUFFIXES are
#      not safe to run concurrently with themselves: libheif (pillow_heif,
#      HEIF/HEIC) deadlocks/stalls two-at-a-time (the reason multi-render hung on
#      .heic/.heif sets); libavif (AVIF) and libjxl (JXL) are kept here
#      conservatively. Those formats additionally take _NATIVE_DECODE_LOCK, so at
#      most one native decode runs at a time regardless of K.
#
# Acquisition order is always semaphore → native lock, so the two never deadlock.
# The parallel win downstream (the numba dither in render_panel_bytes) runs
# unlocked; with decode-once each source is decoded a single time per image.
_NATIVE_DECODE_SUFFIXES = frozenset({".heic", ".heif", ".hif", ".avif", ".jxl"})
_NATIVE_DECODE_LOCK = threading.Lock()
_DECODE_SEMAPHORE = threading.BoundedSemaphore(1)


def set_decode_concurrency(max_concurrent_decodes: int) -> None:
    """Install how many source decodes may run at once (see
    resource_budget.resolve_decode_concurrency).

    Called once at startup and on every config reload. ``open_image_for_render``
    reads this module global at ``with`` time, so a reload takes effect for the
    next decode. Always at least 1.
    """
    global _DECODE_SEMAPHORE
    _DECODE_SEMAPHORE = threading.BoundedSemaphore(max(1, int(max_concurrent_decodes)))


def open_image_for_render(path: Path) -> Image.Image:
    """PIL.open + EXIF transpose + RGB convert + size cap.  Caller closes.

    Bounded by :data:`_DECODE_SEMAPHORE` (concurrent-decode memory) and, for the
    native codecs in :data:`_NATIVE_DECODE_SUFFIXES`, :data:`_NATIVE_DECODE_LOCK`
    (their decoders are not thread-safe). The returned image is fully decoded, so
    downstream rendering still parallelises regardless.
    """
    with _DECODE_SEMAPHORE:
        if path.suffix.lower() in _NATIVE_DECODE_SUFFIXES:
            with _NATIVE_DECODE_LOCK:
                return _open_image_for_render(path)
        return _open_image_for_render(path)


def _open_image_for_render(path: Path) -> Image.Image:
    """Decode a source into a memory-bounded, EXIF-corrected, white-flattened RGB
    image sized to the source bbox.  Caller closes.  SVG is rasterised via resvg.

    Memory discipline for the 464 MB Pi, in order:
      * reject a decompression bomb above ``MAX_IMAGE_PIXELS`` at header read;
      * ``draft`` shrink-on-load for JPEG, then reject anything still above
        ``DECODE_BUDGET_PIXELS`` (un-draftable formats) so one oversized image
        can't OOM the server — it is marked "failed" and the rest keep serving;
      * shrink to the source bbox BEFORE EXIF-transpose / RGB-convert / white
        composite, so those run on the small image, not a full-frame buffer.

    Raises ``ValueError`` for both rejection cases.
    """
    if path.suffix.lower() == ".svg":
        # Already RGB and bounded at screen res by _rasterize_svg; the common
        # shrink below is a cheap guard.
        return _shrink_to_source_bbox(_rasterize_svg(path))

    img = Image.open(path)
    w0, h0 = img.size
    if w0 * h0 > MAX_IMAGE_PIXELS:
        img.close()
        raise ValueError(
            f"image {path.name} is too large: {format_megapixels(w0 * h0)} "
            f"({w0}x{h0}), exceeds the {format_megapixels(MAX_IMAGE_PIXELS)} limit"
        )
    _jpeg_draft_downscale(img, w0, h0)

    # Post-draft decode budget (see DECODE_BUDGET_PIXELS). A big JPEG has
    # shrink-on-loaded to well under the budget and passes here; an un-draftable
    # format (PNG/HEIF/...) that would materialise above the budget is refused
    # NOW — before the decode — so one oversized image can't OOM-kill the whole
    # server. Raised as ValueError so the manager marks that image "failed" and
    # keeps serving everything else.
    dw, dh = img.size
    if dw * dh > DECODE_BUDGET_PIXELS:
        img.close()
        raise ValueError(f"image {path.name} {decode_budget_error(dw, dh)}")

    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    try:
        if has_alpha:
            # Transparency path. Flatten onto WHITE so a transparent background
            # becomes white (the panel/letterbox colour) not the black a plain
            # convert("RGB") leaves (see fb1fdd4) — but SHRINK FIRST so the
            # white composite (which needs several full-frame RGBA buffers) runs
            # on the small image, not the full-res one. Normalise to RGBA up front
            # so reduce()/thumbnail behave; the one full-res RGBA buffer is freed
            # by the shrink before the composite. Any faint edge-bleed from
            # downscaling semi-transparent pixels is imperceptible at panel res.
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            img = _shrink_to_source_bbox(img)  # forces decode, then shrinks
            img = ImageOps.exif_transpose(img)  # now on the small image
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
            img = Image.alpha_composite(bg, img).convert("RGB")
            return img

        # Common path (JPEG / HEIF / opaque PNG) — the memory-critical one on the
        # 464 MB Pi. Shrink FIRST, so exif_transpose and the RGB convert operate
        # on the already-small image. Previously all three ran at source
        # resolution, holding three-plus full-frame buffers alive at once (decode
        # + transpose copy + convert copy) — enough to tip the box into the OOM
        # killer on a big JPEG or a HEIF panorama. Shrinking first caps the peak
        # to a single transient full-res decode buffer.
        #
        # reduce()/thumbnail resample pixel values, which is meaningless on a
        # palette ("P") image — convert those to RGB before shrinking. P-mode
        # sources are palette graphics (small), so the full-res convert is cheap
        # and rare. RGB/L resample cleanly and skip it.
        if img.mode == "P":
            img = img.convert("RGB")
        img = _shrink_to_source_bbox(img)  # forces the one full-res decode, then shrinks
        img = ImageOps.exif_transpose(img)  # now cheap — operates on the small image
        if img.mode != "RGB":
            img = img.convert("RGB")
        return img
    except Image.DecompressionBombError as exc:
        raise ValueError(f"image {path.name} is too large to decode") from exc


def _jpeg_draft_factor(long_side: int) -> int:
    """The power-of-two downscale libjpeg ``draft`` applies for *long_side*.

    Steps in powers of two (libjpeg supports 1/2, 1/4, 1/8), stopping at the
    smallest reduction that still leaves the long side at/above the source cap so
    we never under-sample below what rendering needs. 1 = no reduction.
    """
    if long_side <= _MAX_SOURCE_LONG_SIDE:
        return 1
    k = 1
    while long_side / (k * 2) >= _MAX_SOURCE_LONG_SIDE / 2 and k < 8:
        k *= 2
    return k


def _jpeg_draft_downscale(img: Image.Image, w0: int, h0: int) -> None:
    """Cheap JPEG-only header-time downscale (no-op for PNG/HEIC/etc.).

    ``draft`` sets the libjpeg scale so the *next* ``load()`` decodes a reduced
    buffer directly — the full-resolution array is never materialised for a huge
    JPEG.
    """
    k = _jpeg_draft_factor(max(w0, h0))
    if k > 1:
        try:
            img.draft("RGB", (w0 // k, h0 // k))
        except (AttributeError, OSError):
            pass


# JPEG-family suffixes libjpeg can shrink-on-load (draft). A large one of these
# still decodes small; every other format decodes at full source resolution and
# is bounded by DECODE_BUDGET_PIXELS.
JPEG_SUFFIXES = frozenset({".jpg", ".jpeg", ".jpe", ".jfif", ".mpo"})


def decoded_pixels_exceed_budget(width: int, height: int, *, is_jpeg: bool) -> bool:
    """Would decoding a *width* x *height* source blow the RAM budget?

    Dimensions only — no pixel decode — so it can gate an image out of the whole
    pipeline (thumbnail / classify / render) before any subsystem tries to load
    it. Mirrors ``_open_image_for_render``'s accept/reject exactly: a JPEG
    shrink-on-loads via draft, so its *post-draft* pixel count is what matters;
    every other format decodes at full resolution.
    """
    if width <= 0 or height <= 0:
        return False
    if width * height > MAX_IMAGE_PIXELS:
        return True  # decompression bomb — refused before decode regardless of format
    if is_jpeg:
        k = _jpeg_draft_factor(max(width, height))
        width, height = width // k, height // k
    return width * height > DECODE_BUDGET_PIXELS


def _shrink_to_source_bbox(img: Image.Image) -> Image.Image:
    """Shrink an oversized decoded image down to the RAM-bounded source bbox.

    Two rules (see the MAX_SOURCE_* constants):
      * oversized in BOTH directions → shrink into the (long, short) screen-1.5x
        bbox (more detail than dithering can use is just RAM pressure);
      * oversized on only the long side (tall/wide panoramas) → clamp only the
        long side to ``_MAX_SOURCE_LONG_SIDE`` and keep the short-axis detail.

    Two-stage downscale so the LANCZOS resize temp never balloons off the full
    decode buffer (on Linux a 15 MP source thumbnailed straight to the box peaked
    ~100 MB — too close to the Pi's ceiling): an integer ``reduce()`` (a cheap box
    filter, small intermediate) knocks the long side down to just above the cap,
    then LANCZOS does the final fractional step off the already-small image. Cuts
    the peak roughly in half. ``reduce`` and ``thumbnail`` both preserve
    ``img.info`` (so a later ``exif_transpose`` still sees the orientation tag).
    Returns the shrunk image.
    """
    img_long = max(img.size)
    img_short = min(img.size)
    if img_long > MAX_SOURCE_LONG and img_short > MAX_SOURCE_SHORT:
        # Match orientation: thumbnail takes (w_cap, h_cap), so route long/short.
        cap_long = MAX_SOURCE_LONG
        w_cap, h_cap = (
            (MAX_SOURCE_LONG, MAX_SOURCE_SHORT)
            if img.size[0] >= img.size[1]
            else (MAX_SOURCE_SHORT, MAX_SOURCE_LONG)
        )
    elif img_long > _MAX_SOURCE_LONG_SIDE:
        cap_long = _MAX_SOURCE_LONG_SIDE
        w_cap = h_cap = _MAX_SOURCE_LONG_SIDE
    else:
        return img  # already within the bbox

    # Stage 1: integer box-reduce to just above the target long side. reduce(k)
    # allocates only the (source / k) output, so the big source buffer is freed
    # before the LANCZOS pass — the peak is one decode buffer, not decode + a
    # full-frame resize temp.
    factor = img_long // cap_long
    if factor >= 2:
        img = img.reduce(factor)
    # Stage 2: LANCZOS for the final sub-integer step to the exact bbox.
    img.thumbnail((w_cap, h_cap), Image.Resampling.LANCZOS)
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
        crop_anchor_bboxes_norm: tuple[BoundingBox, ...] | None = None,
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
            crop_anchor_bboxes_norm=crop_anchor_bboxes_norm,
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
