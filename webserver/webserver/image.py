"""Raster image ingest: allowed extensions, grayscale heuristic, panel conversion."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, fields, replace
from io import BytesIO
from pathlib import Path
from typing import Any, Literal

import numpy as np
from numpy.typing import ArrayLike, NDArray
from PIL import Image, ImageEnhance, ImageOps

from webserver.display_constants import FULL_W, PANEL_H, VISUAL_H, VISUAL_W

# Quick browser preview: letterbox / dither at reduced resolution (see :func:`convert_image_preview_png`).
PREVIEW_PNG_MAX_INPUT_SIDE = 800
PREVIEW_PNG_MAX_PANEL_SIDE = 800
from webserver.dither import (
    DitherConfig,
    PALETTE_LAB,
    adaptive_saturate,
    dither,
    linear_to_xyz,
    rgb_to_lab,
    srgb_to_linear,
    xyz_to_lab,
)
from webserver.panel_format import (
    indices_to_panel_bytes,
    indices_to_preview_rgb,
    panel_bytes_to_indices,
    preview_rgb_from_indices,
)

# Source files we treat as raster images (pool scan, upload, PIL open).
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp", ".gif",
    ".heic", ".heif", ".avif",
}

GRAYSCALE_CHROMA_THRESHOLD = 8.0


@dataclass(frozen=True)
class ImageConfig:
    """Prepare / enhance chain, Lab step before diffusion, and :class:`~webserver.dither.DitherConfig`."""

    dither: DitherConfig
    prepare_autocontrast_cutoff: float
    prepare_gamma: float
    prepare_brightness: float
    prepare_contrast: float
    prepare_sharpness: float
    color_enhance: float
    use_adaptive_saturate: bool
    saturate_max_enhance: float
    saturate_low_chroma_thresh: float
    saturate_high_chroma_thresh: float
    scale_chroma: bool
    adaptive_vivid: bool
    vivid_chroma_low: float
    vivid_chroma_high: float

    def cache_slug(self) -> str:
        """Path-safe short fingerprint of ingest + dither fields (not orientation)."""
        raw = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:14]


@dataclass(frozen=True)
class DisplayImageConfig:
    """Panel-facing settings: :class:`ImageConfig` plus physical display orientation."""

    image: ImageConfig
    orientation: Literal["landscape", "portrait"]

    def cache_slug(self) -> str:
        """Path-safe short fingerprint of cached panel output (image pipeline + orientation)."""
        raw = json.dumps(
            {"image": self.image.cache_slug(), "orientation": self.orientation},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:14]


from webserver.display_image_config_presets import PRESET_DISPLAY_IMAGE_CONFIGS

PRESET_IMAGE_CONFIGS: dict[str, ImageConfig] = {
    k: v.image for k, v in PRESET_DISPLAY_IMAGE_CONFIGS.items()
}

_DISPLAY_BLACK_L = float(PALETTE_LAB[0, 0])
_DISPLAY_WHITE_L = float(PALETTE_LAB[1, 0])


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
    linear_out = np.clip(xyz_out @ M_inv.T, 0, 1)
    srgb = np.where(linear_out <= 0.0031308, linear_out * 12.92,
                    1.055 * (linear_out ** (1.0 / 2.4)) - 0.055)
    return np.clip(srgb * 255, 0, 255)


def compress_dynamic_range(
    img_array: ArrayLike,
    *,
    scale_chroma: bool,
    adaptive_vivid: bool,
    vivid_chroma_low: float,
    vivid_chroma_high: float,
) -> NDArray[Any]:
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
    lab_out = np.stack([L_out, a_out, b_out], axis=-1)
    return _lab_to_rgb(lab_out).astype(np.float32)


def default_display_image_config() -> DisplayImageConfig:
    return PRESET_DISPLAY_IMAGE_CONFIGS["atkinson_hue_aware"]


def default_image_config() -> ImageConfig:
    return default_display_image_config().image


def merge_dither_slim(base: DitherConfig, patch: dict) -> DitherConfig:
    names = {f.name for f in fields(DitherConfig)}
    kwargs = {k: patch[k] for k in patch if k in names}
    return replace(base, **kwargs)


def merge_image(base: ImageConfig, patch: dict) -> ImageConfig:
    names = {f.name for f in fields(ImageConfig)}
    flat = {k: v for k, v in patch.items() if k in names and k != "dither"}
    dither_cfg = (
        merge_dither_slim(base.dither, patch["dither"])
        if isinstance(patch.get("dither"), dict)
        else base.dither
    )
    return replace(base, dither=dither_cfg, **flat)


def merge_display_image(base: DisplayImageConfig, patch: dict) -> DisplayImageConfig:
    """Apply optional ``image`` sub-dict (via :func:`merge_image`) and/or ``orientation``."""
    ori = base.orientation
    img = base.image
    if isinstance(patch.get("image"), dict):
        ip = dict(patch["image"])
        o = ip.pop("orientation", None)
        img = merge_image(base.image, ip)
        if isinstance(o, str) and o in ("landscape", "portrait"):
            ori = o  # type: ignore[assignment]
    if "orientation" in patch:
        v = patch["orientation"]
        if isinstance(v, str) and v in ("landscape", "portrait"):
            ori = v  # type: ignore[assignment]
    return replace(base, image=img, orientation=ori)


_DITHER_SLIM_KEYS = frozenset({
    "algorithm", "lut_name", "serpentine", "hue_cutoff_deg", "neutral_chroma",
})


def split_legacy_flat_dither_dict(blob: dict) -> dict:
    """Split a monolithic saved ``dither`` dict into keys for :func:`merge_image`."""
    out: dict = {}
    slim = {k: blob[k] for k in _DITHER_SLIM_KEYS if k in blob}
    if slim:
        out["dither"] = slim
    for k, v in blob.items():
        if k not in _DITHER_SLIM_KEYS:
            out[k] = v
    return out


def image_config_from_legacy_fat_dither_dict(blob: dict) -> ImageConfig:
    return merge_image(default_image_config(), split_legacy_flat_dither_dict(blob))


def preset_name_for_display(disp: DisplayImageConfig) -> str | None:
    for name, preset in PRESET_DISPLAY_IMAGE_CONFIGS.items():
        cand = replace(
            preset,
            image=replace(
                preset.image,
                dither=replace(preset.image.dither, serpentine=disp.image.dither.serpentine),
            ),
            orientation=disp.orientation,
        )
        if cand == disp:
            return name
    return None


def apply_prepare_enhancements(canvas: Image.Image, image_cfg: ImageConfig) -> Image.Image:
    """Autocontrast → gamma → brightness / contrast / sharpness → color or adaptive saturate."""
    canvas = ImageOps.autocontrast(canvas, cutoff=image_cfg.prepare_autocontrast_cutoff)
    gamma_lut = [int(((i / 255.0) ** image_cfg.prepare_gamma) * 255) for i in range(256)] * 3
    canvas = canvas.point(gamma_lut)
    canvas = ImageEnhance.Brightness(canvas).enhance(image_cfg.prepare_brightness)
    canvas = ImageEnhance.Contrast(canvas).enhance(image_cfg.prepare_contrast)
    canvas = ImageEnhance.Sharpness(canvas).enhance(image_cfg.prepare_sharpness)
    if image_cfg.use_adaptive_saturate:
        arr = adaptive_saturate(
            np.array(canvas, dtype=np.float64),
            image_cfg.saturate_max_enhance,
            image_cfg.saturate_low_chroma_thresh,
            image_cfg.saturate_high_chroma_thresh,
        )
        canvas = Image.fromarray(arr.astype(np.uint8))
    else:
        canvas = ImageEnhance.Color(canvas).enhance(image_cfg.color_enhance)
    return canvas


def _preview_letterbox_size(orientation: Literal["landscape", "portrait"], *, max_panel_side: int) -> tuple[int, int]:
    """Scaled (visual) letterbox dimensions so panel memory raster max side ≤ ``max_panel_side``."""
    s = min(1.0, float(max_panel_side) / float(max(FULL_W, PANEL_H)))
    if orientation == "portrait":
        return int(VISUAL_H * s), int(VISUAL_W * s)
    return int(VISUAL_W * s), int(VISUAL_H * s)


def _prepare_panel_sized(
    img: Image.Image,
    display: DisplayImageConfig,
    canvas_w: int,
    canvas_h: int,
) -> tuple[Image.Image, np.ndarray]:
    """Letterbox / pillarbox at ``canvas_w``×``canvas_h``, enhance, rotate to panel memory layout."""
    portrait = display.orientation == "portrait"
    w, h = img.size
    scale = min(canvas_w / w, canvas_h / h)
    new_w, new_h = int(w * scale), int(h * scale)
    img_resized = img.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
    x_off = (canvas_w - new_w) // 2
    y_off = (canvas_h - new_h) // 2
    canvas.paste(img_resized, (x_off, y_off))

    mask = np.ones((canvas_h, canvas_w), dtype=bool)
    mask[y_off:y_off + new_h, x_off:x_off + new_w] = False

    canvas = apply_prepare_enhancements(canvas, display.image)

    if not portrait:
        canvas = canvas.rotate(-90, expand=True)
        mask = np.rot90(mask, k=3)

    return canvas, mask


def prepare_panel_canvas(
    img: Image.Image,
    display: DisplayImageConfig,
) -> tuple[Image.Image, np.ndarray]:
    """Letterbox / pillarbox, enhance, rotate to panel memory layout; mask True = padding."""
    portrait = display.orientation == "portrait"
    canvas_w = VISUAL_H if portrait else VISUAL_W
    canvas_h = VISUAL_W if portrait else VISUAL_H
    return _prepare_panel_sized(img, display, canvas_w, canvas_h)


def preview_panel_rgb_to_png(
    preview_rgb: np.ndarray,
    orientation: Literal["landscape", "portrait"],
) -> bytes:
    """Panel-memory RGB preview → PNG bytes (browser / cache display orientation)."""
    preview_img = Image.fromarray(np.asarray(preview_rgb, dtype=np.uint8))
    if orientation == "landscape":
        preview_img = preview_img.rotate(90, expand=True)
    buf = BytesIO()
    preview_img.save(buf, format="PNG")
    return buf.getvalue()


def is_near_grayscale(img: Image.Image) -> bool:
    thumb = img.copy()
    thumb.thumbnail((200, 200), Image.LANCZOS)
    arr = np.asarray(thumb.convert("RGB"), dtype=np.float64)
    lab = xyz_to_lab(linear_to_xyz(srgb_to_linear(arr)))
    chroma = np.sqrt(lab[..., 1] ** 2 + lab[..., 2] ** 2)
    return float(np.percentile(chroma, 95)) < GRAYSCALE_CHROMA_THRESHOLD


def preview_png_for_panel_bytes(panel_bytes: bytes, display: DisplayImageConfig) -> bytes:
    """Decode packed panel bytes to a browser-oriented PNG preview."""
    idx = panel_bytes_to_indices(panel_bytes)
    return preview_panel_rgb_to_png(indices_to_preview_rgb(idx), display.orientation)


def convert_image(
    img_path: Path,
    display: DisplayImageConfig,
) -> bytes:
    """Full image path → raw panel bytes for the device."""
    print(f"Converting: {img_path.name}")
    t0 = time.time()
    img = Image.open(img_path)
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    print(f"  {img_path.name}: {img.size[0]}x{img.size[1]}")

    image_cfg = display.image
    if (image_cfg.use_adaptive_saturate or image_cfg.adaptive_vivid) and is_near_grayscale(img):
        print(f"  {img_path.name}: B&W detected, using conservative preset")
        image_cfg = replace(
            image_cfg,
            color_enhance=1.05,
            use_adaptive_saturate=False,
            adaptive_vivid=False,
            scale_chroma=False,
        )

    print(
        f"  {img_path.name}: algorithm={image_cfg.dither.algorithm} enhance={image_cfg.color_enhance} "
        f"adaptive_sat={image_cfg.use_adaptive_saturate} adaptive_vivid={image_cfg.adaptive_vivid} "
        f"scale_chroma={image_cfg.scale_chroma}"
    )

    work_display = replace(display, image=image_cfg)
    canvas, padding_mask = prepare_panel_canvas(img, work_display)
    arr = np.asarray(canvas, dtype=np.float32)
    compressed = compress_dynamic_range(
        arr,
        scale_chroma=image_cfg.scale_chroma,
        adaptive_vivid=image_cfg.adaptive_vivid,
        vivid_chroma_low=image_cfg.vivid_chroma_low,
        vivid_chroma_high=image_cfg.vivid_chroma_high,
    )
    canvas_d = Image.fromarray(compressed.astype(np.uint8))
    result_idx = dither(canvas_d, image_cfg.dither)
    result_idx[padding_mask] = 1
    raw_bytes = indices_to_panel_bytes(result_idx)
    print(f"  {img_path.name}: done in {time.time() - t0:.1f}s")
    return raw_bytes


def convert_image_preview_png(
    img_path: Path,
    display: DisplayImageConfig,
) -> bytes:
    """Fast PNG preview: downscale input (max ``PREVIEW_PNG_MAX_INPUT_SIDE`` per side), short dither path."""
    img = Image.open(img_path)
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    img.thumbnail((PREVIEW_PNG_MAX_INPUT_SIDE, PREVIEW_PNG_MAX_INPUT_SIDE), Image.LANCZOS)

    image_cfg = display.image
    if (image_cfg.use_adaptive_saturate or image_cfg.adaptive_vivid) and is_near_grayscale(img):
        image_cfg = replace(
            image_cfg,
            color_enhance=1.05,
            use_adaptive_saturate=False,
            adaptive_vivid=False,
            scale_chroma=False,
        )

    work_display = replace(display, image=image_cfg)
    cw, ch = _preview_letterbox_size(display.orientation, max_panel_side=PREVIEW_PNG_MAX_PANEL_SIDE)
    canvas, padding_mask = _prepare_panel_sized(img, work_display, cw, ch)
    arr = np.asarray(canvas, dtype=np.float32)
    compressed = compress_dynamic_range(
        arr,
        scale_chroma=image_cfg.scale_chroma,
        adaptive_vivid=image_cfg.adaptive_vivid,
        vivid_chroma_low=image_cfg.vivid_chroma_low,
        vivid_chroma_high=image_cfg.vivid_chroma_high,
    )
    canvas_d = Image.fromarray(compressed.astype(np.uint8))
    result_idx = dither(canvas_d, image_cfg.dither)
    result_idx[padding_mask] = 1
    preview_rgb = preview_rgb_from_indices(result_idx)
    return preview_panel_rgb_to_png(preview_rgb, display.orientation)
