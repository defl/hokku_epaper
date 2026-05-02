"""Spectra 6 dithering pipeline: Lab color, LUTs, error diffusion, panel binary."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from functools import lru_cache
from dataclasses import asdict, dataclass
from typing import Any, Literal, TypeAlias

import numpy as np
from numpy.typing import ArrayLike, NDArray
from PIL import Image

from webserver.display_constants import PALETTE_MEASURED_RGB

# Float / uint pipeline arrays (dtypes vary by stage; use Any for practical hints).
FloatArray: TypeAlias = NDArray[Any]
UInt8Array: TypeAlias = NDArray[np.uint8]

CanvasLike: TypeAlias = Image.Image | ArrayLike


def srgb_to_linear(c: ArrayLike) -> FloatArray:
    c = np.asarray(c, dtype=np.float64) / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def linear_to_xyz(rgb: ArrayLike) -> FloatArray:
    rgb = np.asarray(rgb, dtype=np.float64)
    M = np.array([
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ])
    return rgb @ M.T


def xyz_to_lab(xyz: ArrayLike) -> FloatArray:
    ref = np.array([0.95047, 1.00000, 1.08883])
    xyz = xyz / ref
    f = np.where(xyz > 0.008856, xyz ** (1 / 3), 7.787 * xyz + 16 / 116)
    L = 116 * f[..., 1] - 16
    a = 500 * (f[..., 0] - f[..., 1])
    b = 200 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], axis=-1)


def rgb_to_lab(rgb: ArrayLike) -> FloatArray:
    linear = srgb_to_linear(np.clip(np.asarray(rgb, dtype=np.float64), 0, 255))
    xyz = linear_to_xyz(linear)
    return xyz_to_lab(xyz)


PALETTE_LAB = rgb_to_lab(PALETTE_MEASURED_RGB)


def adaptive_saturate(
    img_array: ArrayLike,
    max_enhance: float,
    low_thresh: float,
    high_thresh: float,
) -> FloatArray:
    rgb = np.asarray(img_array, dtype=np.float64)
    linear = srgb_to_linear(rgb)
    xyz = linear_to_xyz(linear)
    lab = xyz_to_lab(xyz)
    chroma = np.sqrt(lab[..., 1] ** 2 + lab[..., 2] ** 2)
    t = np.clip((chroma - low_thresh) / (high_thresh - low_thresh), 0.0, 1.0)
    factor = 1.0 + (max_enhance - 1.0) * t
    lab[..., 1] *= factor
    lab[..., 2] *= factor
    ref = np.array([0.95047, 1.00000, 1.08883])
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    fy = (L + 16) / 116.0
    fx = a / 500.0 + fy
    fz = fy - b / 200.0
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
    return np.clip(srgb * 255, 0, 255).astype(np.float32)


def build_rgb_lut() -> tuple[UInt8Array, float]:
    steps = 32
    scale = 256 / steps
    r_vals = np.arange(steps) * scale + scale / 2
    g_vals = np.arange(steps) * scale + scale / 2
    b_vals = np.arange(steps) * scale + scale / 2
    rr, gg, bb = np.meshgrid(r_vals, g_vals, b_vals, indexing='ij')
    rgb_grid = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)
    lab_grid = rgb_to_lab(rgb_grid)
    dists = np.sum((lab_grid[:, np.newaxis, :] - PALETTE_LAB[np.newaxis, :, :]) ** 2, axis=2)
    lut = np.argmin(dists, axis=1).astype(np.uint8).reshape(steps, steps, steps)
    return lut, scale


def build_rgb_lut_hue_aware(
    hue_cutoff_deg: float,
    neutral_chroma: float,
) -> tuple[UInt8Array, float]:
    steps = 32
    scale = 256 / steps
    r_vals = np.arange(steps) * scale + scale / 2
    rr, gg, bb = np.meshgrid(r_vals, r_vals, r_vals, indexing='ij')
    rgb_grid = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)
    lab_grid = rgb_to_lab(rgb_grid)

    pal_a = PALETTE_LAB[:, 1]
    pal_b = PALETTE_LAB[:, 2]
    pal_chroma = np.sqrt(pal_a ** 2 + pal_b ** 2)
    pal_hue = np.arctan2(pal_b, pal_a)
    neutral_pal = pal_chroma < neutral_chroma

    pix_a = lab_grid[:, 1]
    pix_b = lab_grid[:, 2]
    pix_chroma = np.sqrt(pix_a ** 2 + pix_b ** 2)
    pix_hue = np.arctan2(pix_b, pix_a)

    dh = pix_hue[:, None] - pal_hue[None, :]
    dh = np.arctan2(np.sin(dh), np.cos(dh))
    dh_deg = np.abs(np.degrees(dh))

    forbidden = (
        (pix_chroma[:, None] > neutral_chroma) &
        (~neutral_pal[None, :]) &
        (dh_deg > hue_cutoff_deg)
    )

    dists = np.sum((lab_grid[:, None, :] - PALETTE_LAB[None, :, :]) ** 2, axis=2)
    dists = np.where(forbidden, np.inf, dists)
    lut = np.argmin(dists, axis=1).astype(np.uint8).reshape(steps, steps, steps)
    return lut, scale


@lru_cache(maxsize=1)
def _cached_euclidean_lut() -> tuple[UInt8Array, float]:
    return build_rgb_lut()


@lru_cache(maxsize=16)
def _cached_hue_aware_lut(hue_cutoff_deg: float, neutral_chroma: float) -> tuple[UInt8Array, float]:
    return build_rgb_lut_hue_aware(hue_cutoff_deg, neutral_chroma)


def _error_diffusion_workspace(
    canvas: CanvasLike,
    lut: NDArray[np.uint8],
    lut_scale: float,
) -> tuple[FloatArray, int, int, UInt8Array, NDArray[np.float32], int]:
    assert isinstance(lut, np.ndarray) and lut.ndim == 3
    n = lut.shape[0]
    assert lut.shape == (n, n, n), "lut must be a cube (n,n,n)"
    assert np.isfinite(lut_scale) and lut_scale > 0, "lut_scale must be positive and finite"
    pixels = np.asarray(canvas, dtype=np.float32)
    assert pixels.ndim == 3 and pixels.shape[2] == 3, "canvas must be H×W×3 RGB"
    h, w = int(pixels.shape[0]), int(pixels.shape[1])
    result_idx = np.zeros((h, w), dtype=np.uint8)
    lut_max = n - 1
    return pixels, h, w, result_idx, PALETTE_MEASURED_RGB, lut_max


def floyd_steinberg_dither(
    canvas: CanvasLike,
    lut: NDArray[np.uint8],
    lut_scale: float,
    serpentine: bool,
) -> UInt8Array:
    pixels, h, w, result_idx, pal_rgb, lut_max = _error_diffusion_workspace(
        canvas, lut, lut_scale,
    )

    for y in range(h):
        reverse = serpentine and (y % 2 == 0)
        x_iter = range(w - 1, -1, -1) if reverse else range(w)
        for x in x_iter:
            r = min(max(pixels[y, x, 0], 0.0), 255.0)
            g = min(max(pixels[y, x, 1], 0.0), 255.0)
            b = min(max(pixels[y, x, 2], 0.0), 255.0)
            ri = min(int(r / lut_scale), lut_max)
            gi = min(int(g / lut_scale), lut_max)
            bi = min(int(b / lut_scale), lut_max)
            idx = int(lut[ri, gi, bi])
            result_idx[y, x] = idx
            er = r - pal_rgb[idx, 0]
            eg = g - pal_rgb[idx, 1]
            eb = b - pal_rgb[idx, 2]
            if reverse:
                if x - 1 >= 0:
                    pixels[y, x - 1, 0] += er * 0.4375
                    pixels[y, x - 1, 1] += eg * 0.4375
                    pixels[y, x - 1, 2] += eb * 0.4375
                if y + 1 < h:
                    if x + 1 < w:
                        pixels[y + 1, x + 1, 0] += er * 0.1875
                        pixels[y + 1, x + 1, 1] += eg * 0.1875
                        pixels[y + 1, x + 1, 2] += eb * 0.1875
                    pixels[y + 1, x, 0] += er * 0.3125
                    pixels[y + 1, x, 1] += eg * 0.3125
                    pixels[y + 1, x, 2] += eb * 0.3125
                    if x - 1 >= 0:
                        pixels[y + 1, x - 1, 0] += er * 0.0625
                        pixels[y + 1, x - 1, 1] += eg * 0.0625
                        pixels[y + 1, x - 1, 2] += eb * 0.0625
            else:
                if x + 1 < w:
                    pixels[y, x + 1, 0] += er * 0.4375
                    pixels[y, x + 1, 1] += eg * 0.4375
                    pixels[y, x + 1, 2] += eb * 0.4375
                if y + 1 < h:
                    if x - 1 >= 0:
                        pixels[y + 1, x - 1, 0] += er * 0.1875
                        pixels[y + 1, x - 1, 1] += eg * 0.1875
                        pixels[y + 1, x - 1, 2] += eb * 0.1875
                    pixels[y + 1, x, 0] += er * 0.3125
                    pixels[y + 1, x, 1] += eg * 0.3125
                    pixels[y + 1, x, 2] += eb * 0.3125
                    if x + 1 < w:
                        pixels[y + 1, x + 1, 0] += er * 0.0625
                        pixels[y + 1, x + 1, 1] += eg * 0.0625
                        pixels[y + 1, x + 1, 2] += eb * 0.0625
        if y % 200 == 0:
            print(f"  Dithering: {y}/{h}")
    return result_idx


_STUCKI_OFFSETS: tuple[tuple[int, int, float], ...] = (
    (1, 0, 8 / 42.0),
    (2, 0, 4 / 42.0),
    (-2, 1, 2 / 42.0),
    (-1, 1, 4 / 42.0),
    (0, 1, 8 / 42.0),
    (1, 1, 4 / 42.0),
    (2, 1, 2 / 42.0),
    (-2, 2, 1 / 42.0),
    (-1, 2, 2 / 42.0),
    (0, 2, 4 / 42.0),
    (1, 2, 2 / 42.0),
    (2, 2, 1 / 42.0),
)


def atkinson_dither(
    canvas: CanvasLike,
    lut: NDArray[np.uint8],
    lut_scale: float,
    serpentine: bool,
) -> UInt8Array:
    pixels, h, w, result_idx, pal_rgb, lut_max = _error_diffusion_workspace(
        canvas, lut, lut_scale,
    )
    offsets = ((1, 0), (2, 0), (-1, 1), (0, 1), (1, 1), (0, 2))
    w_coef = 1.0 / 8.0

    for y in range(h):
        reverse = serpentine and (y % 2 == 0)
        x_iter = range(w - 1, -1, -1) if reverse else range(w)
        for x in x_iter:
            r = min(max(pixels[y, x, 0], 0.0), 255.0)
            g = min(max(pixels[y, x, 1], 0.0), 255.0)
            b = min(max(pixels[y, x, 2], 0.0), 255.0)
            ri = min(int(r / lut_scale), lut_max)
            gi = min(int(g / lut_scale), lut_max)
            bi = min(int(b / lut_scale), lut_max)
            idx = int(lut[ri, gi, bi])
            result_idx[y, x] = idx
            er = (r - pal_rgb[idx, 0]) * w_coef
            eg = (g - pal_rgb[idx, 1]) * w_coef
            eb = (b - pal_rgb[idx, 2]) * w_coef
            for dx, dy in offsets:
                eff_dx = -dx if reverse else dx
                nx, ny = x + eff_dx, y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    pixels[ny, nx, 0] += er
                    pixels[ny, nx, 1] += eg
                    pixels[ny, nx, 2] += eb
        if y % 200 == 0:
            print(f"  Dithering: {y}/{h}")
    return result_idx


def stucki_dither(
    canvas: CanvasLike,
    lut: NDArray[np.uint8],
    lut_scale: float,
    serpentine: bool,
) -> UInt8Array:
    pixels, h, w, result_idx, pal_rgb, lut_max = _error_diffusion_workspace(
        canvas, lut, lut_scale,
    )

    for y in range(h):
        reverse = serpentine and (y % 2 == 0)
        x_iter = range(w - 1, -1, -1) if reverse else range(w)
        for x in x_iter:
            r = min(max(pixels[y, x, 0], 0.0), 255.0)
            g = min(max(pixels[y, x, 1], 0.0), 255.0)
            b = min(max(pixels[y, x, 2], 0.0), 255.0)
            ri = min(int(r / lut_scale), lut_max)
            gi = min(int(g / lut_scale), lut_max)
            bi = min(int(b / lut_scale), lut_max)
            idx = int(lut[ri, gi, bi])
            result_idx[y, x] = idx
            er = r - pal_rgb[idx, 0]
            eg = g - pal_rgb[idx, 1]
            eb = b - pal_rgb[idx, 2]
            for dx, dy, wgt in _STUCKI_OFFSETS:
                eff_dx = -dx if reverse else dx
                nx, ny = x + eff_dx, y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    pixels[ny, nx, 0] += er * wgt
                    pixels[ny, nx, 1] += eg * wgt
                    pixels[ny, nx, 2] += eb * wgt
        if y % 200 == 0:
            print(f"  Dithering: {y}/{h}")
    return result_idx


AlgorithmName = Literal["floyd_steinberg", "atkinson", "stucki"]
LutName = Literal["euclidean", "hue_aware"]


@dataclass(frozen=True)
class DitherConfig:
    """Error-diffusion algorithm, palette LUT, and scan order."""

    algorithm: AlgorithmName
    lut_name: LutName
    serpentine: bool
    hue_cutoff_deg: float
    neutral_chroma: float

    def cache_slug(self) -> str:
        """Path-safe short fingerprint of dither settings (including hue LUT params)."""
        raw = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:14]


def lut_and_scale_for_dither_config(dither_cfg: DitherConfig) -> tuple[NDArray[np.uint8], float]:
    """Resolve the 3D RGB→palette LUT (and grid scale) for ``dither_cfg``."""
    if dither_cfg.lut_name == "euclidean":
        return _cached_euclidean_lut()
    return _cached_hue_aware_lut(dither_cfg.hue_cutoff_deg, dither_cfg.neutral_chroma)


_DITHER_FN: dict[str, Callable[..., UInt8Array]] = {
    "floyd_steinberg": floyd_steinberg_dither,
    "atkinson": atkinson_dither,
    "stucki": stucki_dither,
}


def dither(canvas: CanvasLike, dither_cfg: DitherConfig) -> UInt8Array:
    if dither_cfg.algorithm not in _DITHER_FN:
        raise ValueError(f"Unknown algorithm: {dither_cfg.algorithm!r}")
    if dither_cfg.lut_name not in ("euclidean", "hue_aware"):
        raise ValueError(f"Unknown lut_name: {dither_cfg.lut_name!r}")
    lut, lut_scale = lut_and_scale_for_dither_config(dither_cfg)
    fn = _DITHER_FN[dither_cfg.algorithm]
    return fn(canvas, lut, lut_scale, dither_cfg.serpentine)
