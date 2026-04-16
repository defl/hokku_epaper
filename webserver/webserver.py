#!/usr/bin/env -S python3 -u
"""Hokku e-ink image server for EL133UF1 display (dual-panel, full resolution).

Pre-converts ALL images in the upload directory on startup and when new files
appear. Each GET /hokku/ serves the least-shown image (fair distribution),
with an X-Sleep-Seconds header telling the firmware when to wake next.
Converted results are cached on disk to survive restarts.

Display: 1200x1600 native (portrait data), viewed as 1600x1200 landscape.
Two panels of 1200x800, each 480K at 4bpp. Total output: 960K bytes.

Usage:
    python webserver.py
    # Serves on http://0.0.0.0:8080/hokku/

Endpoints:
    GET /hokku/screen/      — 960,000 byte binary (fair rotation) + X-Sleep-Seconds header
    GET /hokku/ui           — Web GUI for configuration and image management
    GET /hokku/api/...      — JSON API for the web GUI
"""
import hashlib
import json
import os
import time
import random
import threading
from datetime import datetime, timedelta
from pathlib import Path
from io import BytesIO

from flask import Flask, send_file, jsonify, make_response, render_template, request, abort, redirect
from PIL import Image
from pillow_heif import register_heif_opener
import numpy as np

register_heif_opener()

# ── Display parameters ──────────────────────────────────────────────
PANEL_W = 600     # each physical panel: 600 columns
PANEL_H = 1600    # each physical panel: 1600 rows
PANEL_BYTES = PANEL_W * PANEL_H // 2   # 480,000 bytes per panel at 4bpp
TOTAL_BYTES = PANEL_BYTES * 2           # 960,000 bytes for full display
FULL_W = PANEL_W * 2                    # 1200 portrait width (2 panels)
VISUAL_W = 1600   # landscape width
VISUAL_H = 1200   # landscape height

# ── Spectra 6 palette ──────────────────────────────────────────────
PALETTE_MEASURED_RGB = np.array([
    [2, 2, 2],          # 0: Black   (theoretical: 0,0,0)
    [190, 200, 200],    # 1: White   (theoretical: 255,255,255)
    [205, 202, 0],      # 2: Yellow  (theoretical: 255,255,0)
    [135, 19, 0],       # 3: Red     (theoretical: 255,0,0)
    [5, 64, 158],       # 4: Blue    (theoretical: 0,0,255)
    [39, 102, 60],      # 5: Green   (theoretical: 0,255,0)
], dtype=np.float32)

PALETTE_PREVIEW_RGB = np.array([
    [0, 0, 0],
    [255, 255, 255],
    [255, 230, 50],
    [200, 20, 20],
    [30, 80, 200],
    [20, 120, 40],
], dtype=np.uint8)

PALETTE_NIBBLE = np.array([0x0, 0x1, 0x2, 0x3, 0x5, 0x6], dtype=np.uint8)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp", ".gif", ".heic", ".heif", ".avif"}

# ── Configuration ──────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "timezone": "America/Chicago",
    "refresh_image_at_time": ["0600", "1200", "1800"],
    "upload_dir": "/images/upload",
    "cache_dir": "/images/cache",
    "port": 8080,
    "poll_interval_seconds": 10,
    "orientation": "landscape",
}

_config_file_path = None  # set during load, used for saving


def _load_config():
    """Load config from file. Check HOKKU_CONFIG env, then ./config.json, then /etc/hokku/config.json."""
    global _config_file_path
    config = dict(DEFAULT_CONFIG)

    candidates = []
    env_path = os.environ.get("HOKKU_CONFIG")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path("./config.json"))
    candidates.append(Path("/etc/hokku/config.json"))

    for path in candidates:
        if path.exists():
            try:
                with open(path) as f:
                    user_config = json.load(f)
                config.update(user_config)
                _config_file_path = path
                print(f"  Config loaded from: {path}")
                return config
            except (json.JSONDecodeError, OSError) as e:
                print(f"  Warning: failed to load config from {path}: {e}")

    print("  No config file found, using defaults")
    return config


def _save_config(config):
    """Save config to the file it was loaded from (or ./config.json)."""
    path = _config_file_path or Path("./config.json")
    # Only save user-facing config keys
    save_keys = ["timezone", "refresh_image_at_time", "upload_dir", "cache_dir", "port", "poll_interval_seconds", "orientation"]
    save_data = {k: config[k] for k in save_keys if k in config}
    with open(path, "w") as f:
        json.dump(save_data, f, indent=2)
    print(f"  Config saved to: {path}")


def _calculate_sleep_seconds(config):
    """Calculate seconds until next refresh_image_at_time based on server's clock and timezone."""
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(config["timezone"])
    except (ImportError, KeyError, Exception):
        tz = None

    if tz:
        now = datetime.now(tz)
    else:
        now = datetime.now()

    times = config.get("refresh_image_at_time", ["0600", "1200", "1800"])
    if not times:
        return 21600  # fallback: 6 hours

    wake_times = []
    for t in times:
        t = str(t).zfill(4)
        h, m = int(t[:2]), int(t[2:])
        wake_times.append((h, m))
    wake_times.sort()

    for h, m in wake_times:
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate > now:
            delta = (candidate - now).total_seconds()
            return max(60, int(delta))

    h, m = wake_times[0]
    tomorrow = now + timedelta(days=1)
    candidate = tomorrow.replace(hour=h, minute=m, second=0, microsecond=0)
    delta = (candidate - now).total_seconds()
    return max(60, int(delta))


def format_duration_human(minutes):
    """Format minutes into human-readable duration string."""
    if minutes < 0:
        return "0m"
    if minutes < 60:
        return f"{int(minutes)}m"
    hours = minutes / 60
    if hours < 24:
        h = int(hours)
        m = int(minutes % 60)
        return f"{h}h {m}m" if m > 0 else f"{h}h"
    days = hours / 24
    if days < 30:
        d = int(days)
        h = int(hours % 24)
        return f"{d}d {h}h" if h > 0 else f"{d}d"
    if days < 365:
        mo = int(days / 30)
        d = int(days % 30)
        return f"{mo}mo {d}d" if d > 0 else f"{mo}mo"
    years = int(days / 365)
    mo = int((days % 365) / 30)
    return f"{years}y {mo}mo" if mo > 0 else f"{years}y"


app = Flask(__name__)

# ── CIE Lab conversion for perceptual color matching ─────────────────

def _srgb_to_linear(c):
    """Convert sRGB [0-255] to linear RGB [0-1]."""
    c = c / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)

def _linear_to_xyz(rgb):
    """Convert linear RGB to CIE XYZ (D65 illuminant)."""
    M = np.array([
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ])
    return rgb @ M.T

def _xyz_to_lab(xyz):
    """Convert XYZ to CIE Lab."""
    ref = np.array([0.95047, 1.00000, 1.08883])
    xyz = xyz / ref
    f = np.where(xyz > 0.008856, xyz ** (1/3), 7.787 * xyz + 16/116)
    L = 116 * f[..., 1] - 16
    a = 500 * (f[..., 0] - f[..., 1])
    b = 200 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], axis=-1)

def _rgb_to_lab(rgb):
    """Convert sRGB [0-255] to CIE Lab. Clamps input to valid range."""
    linear = _srgb_to_linear(np.clip(np.asarray(rgb, dtype=np.float64), 0, 255))
    xyz = _linear_to_xyz(linear)
    return _xyz_to_lab(xyz)

# Precompute palette Lab values from measured colors
PALETTE_LAB = _rgb_to_lab(PALETTE_MEASURED_RGB)

# Precompute display L* range for dynamic range compression
_DISPLAY_BLACK_L = float(_rgb_to_lab(PALETTE_MEASURED_RGB[0:1])[0, 0])
_DISPLAY_WHITE_L = float(_rgb_to_lab(PALETTE_MEASURED_RGB[1:2])[0, 0])

# ── Dynamic range compression ─────────────────────────────────────
# Remap source image luminance into the display's actual L* range so the
# dithering algorithm doesn't try to reproduce brightness levels the panel
# can't show. Based on esp32-photoframe's preprocessImage approach.

def _compress_dynamic_range(img_array):
    """Compress image luminance from full [0,100] L* to display's actual range."""
    rgb = np.asarray(img_array, dtype=np.float64)
    linear = _srgb_to_linear(rgb)
    xyz = _linear_to_xyz(linear)
    lab = _xyz_to_lab(xyz)
    lab[..., 0] = _DISPLAY_BLACK_L + (lab[..., 0] / 100.0) * (_DISPLAY_WHITE_L - _DISPLAY_BLACK_L)
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
        [ 3.2404542, -1.5371385, -0.4985314],
        [-0.9692660,  1.8760108,  0.0415560],
        [ 0.0556434, -0.2040259,  1.0572252],
    ])
    linear_out = xyz_out @ M_inv.T
    linear_out = np.clip(linear_out, 0, 1)
    srgb = np.where(linear_out <= 0.0031308,
                    linear_out * 12.92,
                    1.055 * (linear_out ** (1.0 / 2.4)) - 0.055)
    return np.clip(srgb * 255, 0, 255).astype(np.float32)


# ── Disk cache ──────────────────────────────────────────────────────

def _hash_file(img_path):
    h = hashlib.sha1()
    with open(img_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def _cache_key(img_path, content_hash):
    orientation = _config.get("orientation", "landscape")
    return f"{img_path.stem}_{content_hash[:12]}_{orientation[0]}"

def _load_from_cache(cache_dir, img_path, content_hash):
    key = _cache_key(img_path, content_hash)
    bin_path = cache_dir / f"{key}.bin"
    png_path = cache_dir / f"{key}.png"
    if bin_path.exists() and png_path.exists():
        raw_bytes = bin_path.read_bytes()
        if len(raw_bytes) == TOTAL_BYTES:
            preview_bytes = png_path.read_bytes()
            return raw_bytes, preview_bytes
    return None

def _save_to_cache(cache_dir, img_path, content_hash, raw_bytes, preview_bytes):
    key = _cache_key(img_path, content_hash)
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{key}.bin").write_bytes(raw_bytes)
    (cache_dir / f"{key}.png").write_bytes(preview_bytes)

def _purge_stale_cache(cache_dir, valid_keys):
    if not cache_dir.exists():
        return
    valid_files = set()
    for key in valid_keys:
        valid_files.add(f"{key}.bin")
        valid_files.add(f"{key}.png")
    for f in cache_dir.iterdir():
        if f.name not in valid_files and f.name != "database.json":
            f.unlink()
            print(f"  Cache: removed stale {f.name}")

def _clear_cache_files(cache_dir):
    if cache_dir.exists():
        import shutil
        for f in cache_dir.iterdir():
            if f.name == "database.json":
                continue
            if f.is_dir():
                shutil.rmtree(f)
            else:
                f.unlink()
        print("  Cache cleared")

# ── Database (fair distribution tracking) ──────────────────────────

def _load_database(cache_dir):
    db_path = cache_dir / "database.json"
    if db_path.exists():
        try:
            with open(db_path) as f:
                db = json.load(f)
            if "serve_data" in db:
                # Migrate old show_count to show_index
                for fname, entry in db["serve_data"].items():
                    if "show_count" in entry and "show_index" not in entry:
                        entry["show_index"] = entry.pop("show_count")
                    if "total_show_count" not in entry:
                        entry["total_show_count"] = 0
                    if "total_show_minutes" not in entry:
                        entry["total_show_minutes"] = 0.0
                return db
        except (json.JSONDecodeError, OSError) as e:
            print(f"  Warning: failed to load database.json: {e}")
    return {"serve_data": {}}


def _save_database(cache_dir, db):
    db_path = cache_dir / "database.json"
    cache_dir.mkdir(parents=True, exist_ok=True)
    with open(db_path, "w") as f:
        json.dump(db, f, indent=2)


def _pick_next_image(pool, db):
    """Pick the image with lowest show_index. Random tie-breaking.

    Also handles new image leveling: if any pool image is not in serve_data,
    all existing entries with show_index > 0 are set to 1, so new images
    (starting at 0) get shown first.
    """
    if not pool:
        return None

    serve_data = db["serve_data"]
    pool_filenames = {Path(k).name for k in pool}

    # Detect new images
    has_new = any(Path(k).name not in serve_data for k in pool)
    if has_new:
        for fname in list(serve_data.keys()):
            if serve_data[fname]["show_index"] > 0:
                serve_data[fname]["show_index"] = 1
        for k in pool:
            fname = Path(k).name
            if fname not in serve_data:
                serve_data[fname] = {"show_index": 0, "last_request": None,
                                     "total_show_count": 0, "total_show_minutes": 0.0}

    # Remove entries for deleted images
    for fname in list(serve_data.keys()):
        if fname not in pool_filenames:
            del serve_data[fname]

    # Ensure all pool images have entries
    for k in pool:
        fname = Path(k).name
        if fname not in serve_data:
            serve_data[fname] = {"show_index": 0, "last_request": None,
                                 "total_show_count": 0, "total_show_minutes": 0.0}

    # Find minimum show_index
    pool_entries = []
    for k in pool:
        fname = Path(k).name
        idx = serve_data[fname]["show_index"]
        pool_entries.append((k, fname, idx))

    min_idx = min(e[2] for e in pool_entries)
    candidates = [(k, fname) for k, fname, idx in pool_entries if idx == min_idx]

    chosen_key, chosen_fname = random.choice(candidates)

    # Track display time for previously served image
    _track_display_time(serve_data)

    # Update serve_data for chosen image
    serve_data[chosen_fname]["show_index"] += 1
    serve_data[chosen_fname]["last_request"] = datetime.now().isoformat(timespec="seconds")
    serve_data[chosen_fname]["total_show_count"] = serve_data[chosen_fname].get("total_show_count", 0) + 1

    return chosen_key


def _track_display_time(serve_data):
    """Add elapsed display time to the previously served image."""
    global _last_served
    prev_name = _last_served.get("name")
    prev_time = _last_served.get("served_at")
    if prev_name and prev_time and prev_name in serve_data:
        try:
            prev_dt = datetime.fromisoformat(prev_time)
            elapsed_minutes = (datetime.now() - prev_dt).total_seconds() / 60.0
            if 0 < elapsed_minutes < 1440 * 30:  # sanity: less than 30 days
                serve_data[prev_name]["total_show_minutes"] = (
                    serve_data[prev_name].get("total_show_minutes", 0.0) + elapsed_minutes
                )
        except (ValueError, KeyError):
            pass


# ── Image pool (protected by lock) ──────────────────────────────────
_lock = threading.Lock()
_pool = {}
_last_served = {"key": None, "name": None, "binary": None, "preview_png": None, "served_at": None}
_converting_count = 0
_converting_name = None  # name of currently converting image
_config = dict(DEFAULT_CONFIG)
_database = {"serve_data": {}}


def _get_upload_dir():
    return Path(_config["upload_dir"])

def _get_cache_dir():
    return Path(_config["cache_dir"])

def _list_images():
    upload_dir = _get_upload_dir()
    if not upload_dir.exists():
        return []
    return [f for f in upload_dir.iterdir()
            if f.suffix.lower() in IMAGE_EXTENSIONS and f.is_file()]


def _prepare_canvas(img):
    """Scale image to fit display, enhance for e-ink, produce 1200x1600 native buffer.

    In landscape mode: composites on 1600x1200 canvas, then rotates -90° to 1200x1600.
    In portrait mode: composites directly on 1200x1600 canvas, no rotation needed.

    Returns (canvas, padding_mask) where padding_mask is a boolean numpy array
    (True = padding pixel, should be forced to white after dithering).
    """
    from PIL import ImageEnhance, ImageOps
    orientation = _config.get("orientation", "landscape")
    portrait = (orientation == "portrait")

    # Canvas dimensions: landscape = 1600x1200, portrait = 1200x1600
    canvas_w = VISUAL_H if portrait else VISUAL_W   # 1200 or 1600
    canvas_h = VISUAL_W if portrait else VISUAL_H   # 1600 or 1200

    w, h = img.size
    scale = min(canvas_w / w, canvas_h / h)
    new_w, new_h = int(w * scale), int(h * scale)
    img_resized = img.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
    x_off = (canvas_w - new_w) // 2
    y_off = (canvas_h - new_h) // 2
    canvas.paste(img_resized, (x_off, y_off))

    # Build padding mask (True = padding, not image content)
    mask = np.ones((canvas_h, canvas_w), dtype=bool)
    mask[y_off:y_off + new_h, x_off:x_off + new_w] = False

    canvas = ImageOps.autocontrast(canvas, cutoff=0.5)
    gamma = 0.85
    gamma_lut = [int(((i / 255.0) ** gamma) * 255) for i in range(256)] * 3
    canvas = canvas.point(gamma_lut)
    canvas = ImageEnhance.Brightness(canvas).enhance(1.0)
    canvas = ImageEnhance.Contrast(canvas).enhance(1.1)
    canvas = ImageEnhance.Sharpness(canvas).enhance(1.3)
    canvas = ImageEnhance.Color(canvas).enhance(1.2)

    if not portrait:
        # Landscape: rotate -90° CW to get 1200x1600 native format
        canvas = canvas.rotate(-90, expand=True)
        mask = np.rot90(mask, k=3)

    return canvas, mask


def _build_rgb_lut():
    steps = 32
    scale = 256 / steps
    r_vals = np.arange(steps) * scale + scale / 2
    g_vals = np.arange(steps) * scale + scale / 2
    b_vals = np.arange(steps) * scale + scale / 2
    rr, gg, bb = np.meshgrid(r_vals, g_vals, b_vals, indexing='ij')
    rgb_grid = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)
    lab_grid = _rgb_to_lab(rgb_grid)
    dists = np.sum((lab_grid[:, np.newaxis, :] - PALETTE_LAB[np.newaxis, :, :]) ** 2, axis=2)
    lut = np.argmin(dists, axis=1).astype(np.uint8).reshape(steps, steps, steps)
    return lut, scale

_RGB_LUT, _LUT_SCALE = _build_rgb_lut()


def _floyd_steinberg_dither(canvas):
    pixels = np.array(canvas, dtype=np.float32)
    h, w, _ = pixels.shape
    result_idx = np.zeros((h, w), dtype=np.uint8)
    pal_rgb = PALETTE_MEASURED_RGB
    lut = _RGB_LUT
    lut_max = lut.shape[0] - 1
    lut_scale = _LUT_SCALE

    for y in range(h):
        for x in range(w):
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


def _convert_image(img_path):
    print(f"Converting: {img_path.name}")
    t0 = time.time()
    from PIL import ImageOps
    img = Image.open(img_path)
    # Apply EXIF orientation before converting — phones and cameras store
    # rotation as metadata rather than rotating the actual pixel data.
    # Without this, portrait photos appear sideways on the display.
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    print(f"  {img_path.name}: {img.size[0]}x{img.size[1]}")
    canvas, padding_mask = _prepare_canvas(img)
    canvas_array = _compress_dynamic_range(np.array(canvas, dtype=np.float32))
    canvas = Image.fromarray(canvas_array.astype(np.uint8))
    result_idx = _floyd_steinberg_dither(canvas)

    # Force padding areas to pure white (palette index 1) — without this,
    # the enhancement + dithering pipeline turns pure white into a slightly
    # off-white that gets dithered as a visible dotted pattern.
    result_idx[padding_mask] = 1
    nibbles = PALETTE_NIBBLE[result_idx]
    panel1_nib = nibbles[:, :PANEL_W]
    panel2_nib = nibbles[:, PANEL_W:]
    panel1_bin = (panel1_nib[:, 0::2] << 4) | panel1_nib[:, 1::2]
    panel2_bin = (panel2_nib[:, 0::2] << 4) | panel2_nib[:, 1::2]
    raw_bytes = panel1_bin.astype(np.uint8).tobytes() + panel2_bin.astype(np.uint8).tobytes()
    assert len(raw_bytes) == TOTAL_BYTES, f"Expected {TOTAL_BYTES}, got {len(raw_bytes)}"
    preview_rgb = PALETTE_PREVIEW_RGB[result_idx]
    orientation = _config.get("orientation", "landscape")
    if orientation == "landscape":
        # Rotate back 90° CCW for human-friendly landscape preview
        preview_img = Image.fromarray(preview_rgb).rotate(90, expand=True)
    else:
        # Portrait: already in natural viewing orientation
        preview_img = Image.fromarray(preview_rgb)
    buf = BytesIO()
    preview_img.save(buf, format="PNG")
    preview_bytes = buf.getvalue()
    elapsed = time.time() - t0
    print(f"  {img_path.name}: done in {elapsed:.1f}s")
    return raw_bytes, preview_bytes


def _convert_and_store(img_path, content_hash):
    global _converting_count, _converting_name
    cache_dir = _get_cache_dir()
    try:
        cached = _load_from_cache(cache_dir, img_path, content_hash)
        if cached:
            raw_bytes, preview_bytes = cached
            print(f"  Cache hit: {img_path.name}")
        else:
            with _lock:
                _converting_count += 1
                _converting_name = img_path.name
            try:
                raw_bytes, preview_bytes = _convert_image(img_path)
                _save_to_cache(cache_dir, img_path, content_hash, raw_bytes, preview_bytes)
            finally:
                with _lock:
                    _converting_count -= 1
                    if _converting_count == 0:
                        _converting_name = None

        with _lock:
            _pool[str(img_path)] = {
                "binary": raw_bytes,
                "preview_png": preview_bytes,
                "hash": content_hash,
            }
        print(f"  Pool: {img_path.name} ready ({len(_pool)} total)")
    except Exception as e:
        print(f"  Error converting {img_path.name}: {e}")


_sync_lock = threading.Lock()

def _sync_pool():
    if not _sync_lock.acquire(blocking=False):
        print("  Sync already running, skipping")
        return
    try:
        _sync_pool_inner()
    finally:
        _sync_lock.release()

def _sync_pool_inner():
    cache_dir = _get_cache_dir()
    image_paths = _list_images()
    current_paths = set()

    for img_path in image_paths:
        key = str(img_path)
        current_paths.add(key)
        content_hash = _hash_file(img_path)
        with _lock:
            existing = _pool.get(key)
            if existing and existing["hash"] == content_hash:
                continue
        print(f"  Processing: {img_path.name}")
        _convert_and_store(img_path, content_hash)

    with _lock:
        for key in list(_pool.keys()):
            if key not in current_paths:
                del _pool[key]
                print(f"  Pool: removed deleted file {key}")

    with _lock:
        valid_cache_keys = set()
        for key, entry in _pool.items():
            valid_cache_keys.add(_cache_key(Path(key), entry["hash"]))
    _purge_stale_cache(cache_dir, valid_cache_keys)


def _background_watcher():
    while True:
        try:
            _sync_pool()
        except Exception as e:
            print(f"  Watcher error: {e}")
        time.sleep(_config.get("poll_interval_seconds", 10))


# ── Flask routes: device endpoints ─────────────────────────────────

@app.route("/hokku/screen/", strict_slashes=False)
def serve_binary():
    global _database
    with _lock:
        if not _pool:
            if _converting_count > 0:
                return "Converting images, try again shortly", 503
            return "No images in upload directory", 404

        key = _pick_next_image(_pool, _database)
        if key is None:
            return "No images available", 404

        entry = _pool[key]
        _last_served["key"] = key
        _last_served["name"] = Path(key).name
        _last_served["binary"] = entry["binary"]
        _last_served["preview_png"] = entry["preview_png"]
        _last_served["served_at"] = datetime.now().isoformat(timespec="seconds")

        # Track the calling screen
        screen_name = request.headers.get("X-Screen-Name", "unnamed")
        screen_ip = request.remote_addr or "unknown"
        if "screens" not in _database:
            _database["screens"] = {}
        screens = _database["screens"]
        if screen_name not in screens:
            screens[screen_name] = {"ip": screen_ip, "request_count": 0, "last_seen": None}
        screens[screen_name]["ip"] = screen_ip
        screens[screen_name]["request_count"] += 1
        screens[screen_name]["last_seen"] = datetime.now().isoformat(timespec="seconds")

        _save_database(_get_cache_dir(), _database)

    sleep_seconds = _calculate_sleep_seconds(_config)
    print(f"  Serving: {_last_served['name']} to {screen_name} (sleep_seconds={sleep_seconds})")

    response = make_response(entry["binary"])
    response.headers["Content-Type"] = "application/octet-stream"
    response.headers["X-Sleep-Seconds"] = str(sleep_seconds)
    response.headers["Content-Disposition"] = "attachment; filename=hokku.bin"
    return response


# ── Flask routes: Web GUI ──────────────────────────────────────────

@app.route("/")
def root():
    return redirect("/hokku/ui")

@app.route("/hokku/ui")
def web_gui():
    return render_template("index.html")


@app.route("/hokku/api/status")
def api_status():
    """JSON API for the web GUI — includes enriched serve_data with formatted durations."""
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(_config["timezone"])
        now_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _lock:
        pool_files = sorted(Path(k).name for k in _pool.keys())
        serve_data = _database.get("serve_data", {})

        enriched = {}
        for fname in pool_files:
            entry = serve_data.get(fname, {})
            enriched[fname] = {
                "show_index": entry.get("show_index", 0),
                "last_request": entry.get("last_request"),
                "total_show_count": entry.get("total_show_count", 0),
                "total_show_minutes": entry.get("total_show_minutes", 0.0),
                "total_show_formatted": format_duration_human(entry.get("total_show_minutes", 0.0)),
            }

        screens = _database.get("screens", {})

        return jsonify({
            "pool_files": pool_files,
            "pool_size": len(_pool),
            "serve_data": enriched,
            "screens": screens,
            "last_served": _last_served["name"],
            "converting": _converting_count,
            "converting_name": _converting_name,
            "server_time": now_str,
            "config": {
                "timezone": _config["timezone"],
                "refresh_image_at_time": _config["refresh_image_at_time"],
                "port": _config["port"],
                "poll_interval_seconds": _config.get("poll_interval_seconds", 10),
                "orientation": _config.get("orientation", "landscape"),
                "upload_dir": str(_get_upload_dir()),
                "cache_dir": str(_get_cache_dir()),
            },
        })


# Formats browsers can display natively — everything else gets converted to JPEG
_BROWSER_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".avif"}

@app.route("/hokku/api/original/<filename>")
def api_original(filename):
    """Serve the original uploaded image, converting to JPEG if the browser can't display it."""
    img_path = _get_upload_dir() / filename
    if not img_path.exists() or not img_path.is_file():
        abort(404)
    if img_path.suffix.lower() in _BROWSER_IMAGE_EXTS:
        return send_file(img_path)
    # Convert non-browser formats (HEIC, TIFF, etc.) to JPEG
    try:
        from PIL import ImageOps
        img = Image.open(img_path)
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=92)
        buf.seek(0)
        return send_file(buf, mimetype="image/jpeg")
    except Exception:
        # Fall back to serving the raw file
        return send_file(img_path)


_thumb_lock = threading.Lock()

@app.route("/hokku/api/thumbnail/<filename>")
def api_thumbnail(filename):
    """Serve a cached thumbnail of the original image (~300px wide)."""
    img_path = _get_upload_dir() / filename
    if not img_path.exists() or not img_path.is_file():
        abort(404)

    # Serve from disk cache if available
    thumb_dir = _get_cache_dir() / "thumbs"
    thumb_path = thumb_dir / (img_path.stem + "_thumb.jpg")
    if thumb_path.exists() and thumb_path.stat().st_mtime >= img_path.stat().st_mtime:
        return send_file(thumb_path, mimetype="image/jpeg")

    # Generate and cache — serialize to avoid memory spikes from parallel decodes
    with _thumb_lock:
        # Re-check after acquiring lock (another request may have generated it)
        if thumb_path.exists() and thumb_path.stat().st_mtime >= img_path.stat().st_mtime:
            return send_file(thumb_path, mimetype="image/jpeg")
        try:
            from PIL import ImageOps
            thumb_dir.mkdir(parents=True, exist_ok=True)
            img = Image.open(img_path)
            img = ImageOps.exif_transpose(img)
            img.thumbnail((300, 300), Image.LANCZOS)
            img.save(thumb_path, format="JPEG", quality=80)
            return send_file(thumb_path, mimetype="image/jpeg")
        except Exception:
            abort(500)


@app.route("/hokku/api/dithered/<filename>")
def api_dithered(filename):
    """Serve the dithered preview PNG for an image."""
    with _lock:
        for key, entry in _pool.items():
            if Path(key).name == filename:
                return send_file(BytesIO(entry["preview_png"]), mimetype="image/png")
    abort(404)


@app.route("/hokku/api/show_next/<filename>", methods=["POST"])
def api_show_next(filename):
    """Set an image's show_index to min-1 so it's served next."""
    with _lock:
        serve_data = _database.get("serve_data", {})
        if filename not in serve_data:
            return jsonify({"error": "Image not found in database"}), 404

        # Find current minimum show_index
        min_idx = min(e["show_index"] for e in serve_data.values()) if serve_data else 0
        serve_data[filename]["show_index"] = min_idx - 1

        _save_database(_get_cache_dir(), _database)

    return jsonify({"status": "ok", "filename": filename, "new_show_index": min_idx - 1})


@app.route("/hokku/api/config", methods=["POST"])
def api_config():
    """Update server configuration."""
    global _config
    data = request.get_json()
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    changed = False
    if "timezone" in data:
        _config["timezone"] = data["timezone"]
        changed = True
    if "refresh_image_at_time" in data:
        _config["refresh_image_at_time"] = data["refresh_image_at_time"]
        changed = True
    if "poll_interval_seconds" in data:
        val = int(data["poll_interval_seconds"])
        if val >= 1:
            _config["poll_interval_seconds"] = val
            changed = True

    orientation_changed = False
    if "orientation" in data:
        val = data["orientation"]
        if val in ("landscape", "portrait"):
            if _config.get("orientation", "landscape") != val:
                orientation_changed = True
            _config["orientation"] = val
            changed = True

    if changed:
        _save_config(_config)

    if orientation_changed:
        # Orientation affects image processing, so clear cache and re-convert
        _clear_cache_files(_get_cache_dir())
        with _lock:
            _pool.clear()
        threading.Thread(target=_sync_pool, daemon=True).start()

    return jsonify({"status": "ok", "config": {
        "timezone": _config["timezone"],
        "refresh_image_at_time": _config["refresh_image_at_time"],
        "poll_interval_seconds": _config.get("poll_interval_seconds", 10),
        "orientation": _config.get("orientation", "landscape"),
    }})


@app.route("/hokku/api/clear_cache", methods=["POST"])
def api_clear_cache():
    """Wipe cache and trigger re-conversion."""
    _clear_cache_files(_get_cache_dir())
    with _lock:
        _pool.clear()
    threading.Thread(target=_sync_pool, daemon=True).start()
    return jsonify({"status": "cache cleared, re-converting"})


@app.route("/hokku/api/time")
def api_time():
    """Return current server time in configured timezone."""
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(_config["timezone"])
        now = datetime.now(tz)
    except Exception:
        now = datetime.now()
    return jsonify({
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": _config["timezone"],
    })


# ── Main ───────────────────────────────────────────────────────────

def main():
    global _config, _database

    _config = _load_config()
    upload_dir = _get_upload_dir()
    cache_dir = _get_cache_dir()
    upload_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    _database = _load_database(cache_dir)

    port = _config["port"]
    poll = _config.get("poll_interval_seconds", 10)

    print(f"Hokku image server (full resolution: {VISUAL_W}x{VISUAL_H})")
    print(f"  Upload dir: {upload_dir}")
    print(f"  Cache dir:  {cache_dir}")
    print(f"  Timezone:   {_config['timezone']}")
    print(f"  Refresh at: {_config['refresh_image_at_time']}")
    print(f"  Poll interval: {poll}s")
    print(f"  Output: {TOTAL_BYTES} bytes per image ({PANEL_BYTES} per panel)")
    print(f"  Endpoints:")
    print(f"    GET /hokku/screen/      — 960K binary (fair rotation) + X-Sleep-Seconds header")
    print(f"    GET /hokku/ui           — Web GUI")

    images = _list_images()
    if images:
        print(f"  Found {len(images)} image(s), converting in background...")
    else:
        print(f"  No images found yet, waiting for uploads...")

    watcher = threading.Thread(target=_background_watcher, daemon=True)
    watcher.start()

    print(f"  Starting server on port {port}...")
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
