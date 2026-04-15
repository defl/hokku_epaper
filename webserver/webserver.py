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
    GET /hokku/             — 960,000 byte binary (fair rotation) + X-Sleep-Seconds header
    GET /hokku/preview      — PNG preview of last served image
    GET /hokku/status       — JSON status info
    GET /hokku/clear_cache  — Wipe disk cache and re-convert all images
"""
import hashlib
import json
import time
import random
import threading
from datetime import datetime, timedelta
from pathlib import Path
from io import BytesIO

from flask import Flask, send_file, jsonify, make_response
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
# Measured palette: what the display actually renders (for dithering & error diffusion).
# Source: esp32-photoframe project (photographed from real Spectra 6 panel).
# These values differ significantly from theoretical values and produce more
# accurate dithering because error diffusion is computed against actual output.
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
POLL_INTERVAL = 10  # seconds between file checks

# ── Configuration ──────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "timezone": "America/Chicago",
    "refresh_image_at_time": ["0600", "1200", "1800"],
    "upload_dir": "/images/upload",
    "cache_dir": "/images/cache",
    "port": 8080,
}


def _load_config():
    """Load config from file. Check HOKKU_CONFIG env, then ./config.json, then /etc/hokku/config.json."""
    import os
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
                print(f"  Config loaded from: {path}")
                return config
            except (json.JSONDecodeError, OSError) as e:
                print(f"  Warning: failed to load config from {path}: {e}")

    print("  No config file found, using defaults")
    return config


def _calculate_sleep_seconds(config):
    """Calculate seconds until next refresh_image_at_time based on server's clock and timezone."""
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(config["timezone"])
    except (ImportError, KeyError, Exception):
        # Fallback: use local system time
        tz = None

    if tz:
        now = datetime.now(tz)
    else:
        now = datetime.now()

    times = config.get("refresh_image_at_time", ["0600", "1200", "1800"])
    if not times:
        return 21600  # fallback: 6 hours

    # Parse HHMM strings into (hour, minute) tuples
    wake_times = []
    for t in times:
        t = str(t).zfill(4)
        h, m = int(t[:2]), int(t[2:])
        wake_times.append((h, m))
    wake_times.sort()

    # Find next wake time
    for h, m in wake_times:
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate > now:
            delta = (candidate - now).total_seconds()
            return max(60, int(delta))  # minimum 60 seconds

    # All times today have passed, next is first time tomorrow
    h, m = wake_times[0]
    tomorrow = now + timedelta(days=1)
    candidate = tomorrow.replace(hour=h, minute=m, second=0, microsecond=0)
    delta = (candidate - now).total_seconds()
    return max(60, int(delta))


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

    # Remap L* from [0, 100] to [display_black_L, display_white_L]
    lab[..., 0] = _DISPLAY_BLACK_L + (lab[..., 0] / 100.0) * (_DISPLAY_WHITE_L - _DISPLAY_BLACK_L)

    # Convert back: Lab -> XYZ -> linear RGB -> sRGB
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
    """Compute SHA-1 hash of file contents."""
    h = hashlib.sha1()
    with open(img_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def _cache_key(img_path, content_hash):
    """Generate a cache key from filename + content hash."""
    return f"{img_path.stem}_{content_hash[:12]}"

def _load_from_cache(cache_dir, img_path, content_hash):
    """Try to load converted data from disk cache. Returns (binary, preview_png) or None."""
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
    """Save converted data to disk cache."""
    key = _cache_key(img_path, content_hash)
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{key}.bin").write_bytes(raw_bytes)
    (cache_dir / f"{key}.png").write_bytes(preview_bytes)

def _purge_stale_cache(cache_dir, valid_keys):
    """Remove cache files that don't correspond to any current source image."""
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
    """Remove all cached files (but keep database.json)."""
    if cache_dir.exists():
        for f in cache_dir.iterdir():
            if f.name != "database.json":
                f.unlink()
        print("  Cache cleared")

# ── Database (fair distribution tracking) ──────────────────────────

def _load_database(cache_dir):
    """Load database.json from cache dir. Returns dict with 'serve_data' key."""
    db_path = cache_dir / "database.json"
    if db_path.exists():
        try:
            with open(db_path) as f:
                db = json.load(f)
            if "serve_data" in db:
                return db
        except (json.JSONDecodeError, OSError) as e:
            print(f"  Warning: failed to load database.json: {e}")
    return {"serve_data": {}}


def _save_database(cache_dir, db):
    """Save database.json to cache dir."""
    db_path = cache_dir / "database.json"
    cache_dir.mkdir(parents=True, exist_ok=True)
    with open(db_path, "w") as f:
        json.dump(db, f, indent=2)


def _pick_next_image(pool, db):
    """Pick the least-shown image from pool. Random tie-breaking. Returns pool key or None.

    Also handles new image leveling: if any pool image is not in serve_data,
    all existing entries with show_count > 0 are set to 1, so new images
    (starting at 0) get shown first.
    """
    if not pool:
        return None

    serve_data = db["serve_data"]
    pool_filenames = {Path(k).name for k in pool}

    # Detect new images (in pool but not in serve_data)
    has_new = any(Path(k).name not in serve_data for k in pool)
    if has_new:
        # Level existing entries: set all show_count > 0 to 1
        for fname in list(serve_data.keys()):
            if serve_data[fname]["show_count"] > 0:
                serve_data[fname]["show_count"] = 1
        # Initialize new images at 0
        for k in pool:
            fname = Path(k).name
            if fname not in serve_data:
                serve_data[fname] = {"show_count": 0, "last_request": None}

    # Remove entries for images no longer in pool
    for fname in list(serve_data.keys()):
        if fname not in pool_filenames:
            del serve_data[fname]

    # Ensure all pool images have entries
    for k in pool:
        fname = Path(k).name
        if fname not in serve_data:
            serve_data[fname] = {"show_count": 0, "last_request": None}

    # Find minimum show_count among pool images
    pool_entries = []
    for k in pool:
        fname = Path(k).name
        count = serve_data[fname]["show_count"]
        pool_entries.append((k, fname, count))

    min_count = min(e[2] for e in pool_entries)
    candidates = [(k, fname) for k, fname, count in pool_entries if count == min_count]

    # Random tie-breaking
    chosen_key, chosen_fname = random.choice(candidates)

    # Update serve_data
    serve_data[chosen_fname]["show_count"] += 1
    serve_data[chosen_fname]["last_request"] = datetime.now().isoformat(timespec="seconds")

    return chosen_key


# ── Image pool (protected by lock) ──────────────────────────────────
_lock = threading.Lock()
_pool = {}
_last_served = {"key": None, "name": None, "binary": None, "preview_png": None}
_converting_count = 0
_config = dict(DEFAULT_CONFIG)
_database = {"serve_data": {}}


def _get_upload_dir():
    return Path(_config["upload_dir"])


def _get_cache_dir():
    return Path(_config["cache_dir"])


def _list_images():
    """Return list of image paths in upload dir."""
    upload_dir = _get_upload_dir()
    if not upload_dir.exists():
        return []
    return [f for f in upload_dir.iterdir()
            if f.suffix.lower() in IMAGE_EXTENSIONS and f.is_file()]


def _prepare_canvas(img):
    """Scale image to fit 1600x1200 landscape, enhance for e-ink, then rotate 90 CW for panel data."""
    from PIL import ImageEnhance, ImageOps
    w, h = img.size
    scale = min(VISUAL_W / w, VISUAL_H / h)
    new_w, new_h = int(w * scale), int(h * scale)
    img_resized = img.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGB", (VISUAL_W, VISUAL_H), (255, 255, 255))
    x_off = (VISUAL_W - new_w) // 2
    y_off = (VISUAL_H - new_h) // 2
    canvas.paste(img_resized, (x_off, y_off))

    # E-ink compensation: lift midtones with gamma, then mild contrast/saturation
    # Gamma < 1.0 brightens midtones without blowing out highlights
    canvas = ImageOps.autocontrast(canvas, cutoff=0.5)  # normalize histogram
    gamma = 0.85
    gamma_lut = [int(((i / 255.0) ** gamma) * 255) for i in range(256)] * 3
    canvas = canvas.point(gamma_lut)
    canvas = ImageEnhance.Brightness(canvas).enhance(1.0)
    canvas = ImageEnhance.Contrast(canvas).enhance(1.1)
    canvas = ImageEnhance.Sharpness(canvas).enhance(1.3)
    canvas = ImageEnhance.Color(canvas).enhance(1.2)

    canvas = canvas.rotate(-90, expand=True)  # -> 1200x1600 portrait (CW rotation)
    return canvas


def _build_rgb_lut():
    """Precompute RGB -> nearest palette index lookup table (32 steps per channel)."""
    steps = 32
    scale = 256 / steps  # 8
    r_vals = np.arange(steps) * scale + scale / 2  # center of each bin
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
    """Floyd-Steinberg dithering with precomputed Lab LUT and clamped error diffusion."""
    pixels = np.array(canvas, dtype=np.float32)
    h, w, _ = pixels.shape
    result_idx = np.zeros((h, w), dtype=np.uint8)

    pal_rgb = PALETTE_MEASURED_RGB
    lut = _RGB_LUT
    lut_max = lut.shape[0] - 1
    lut_scale = _LUT_SCALE

    for y in range(h):
        for x in range(w):
            # Clamp pixel to valid range BEFORE matching and error computation
            r = min(max(pixels[y, x, 0], 0.0), 255.0)
            g = min(max(pixels[y, x, 1], 0.0), 255.0)
            b = min(max(pixels[y, x, 2], 0.0), 255.0)
            # LUT lookup instead of per-pixel Lab conversion
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
    """Convert a single image to Spectra 6 binary + preview. Thread-safe."""
    print(f"Converting: {img_path.name}")
    t0 = time.time()

    img = Image.open(img_path).convert("RGB")
    print(f"  {img_path.name}: {img.size[0]}x{img.size[1]}")

    canvas = _prepare_canvas(img)

    # Compress dynamic range to match display's actual luminance capabilities
    canvas_array = _compress_dynamic_range(np.array(canvas, dtype=np.float32))
    canvas = Image.fromarray(canvas_array.astype(np.uint8))

    result_idx = _floyd_steinberg_dither(canvas)

    nibbles = PALETTE_NIBBLE[result_idx]  # shape (1600, 1200)
    # Split into two panels: left 600 cols -> CTRL1, right 600 cols -> CTRL2
    panel1_nib = nibbles[:, :PANEL_W]     # (1600, 600)
    panel2_nib = nibbles[:, PANEL_W:]     # (1600, 600)
    panel1_bin = (panel1_nib[:, 0::2] << 4) | panel1_nib[:, 1::2]  # (1600, 300)
    panel2_bin = (panel2_nib[:, 0::2] << 4) | panel2_nib[:, 1::2]  # (1600, 300)
    raw_bytes = panel1_bin.astype(np.uint8).tobytes() + panel2_bin.astype(np.uint8).tobytes()
    assert len(raw_bytes) == TOTAL_BYTES, f"Expected {TOTAL_BYTES}, got {len(raw_bytes)}"

    preview_rgb = PALETTE_PREVIEW_RGB[result_idx]
    preview_img = Image.fromarray(preview_rgb).rotate(90, expand=True)
    buf = BytesIO()
    preview_img.save(buf, format="PNG")
    preview_bytes = buf.getvalue()

    elapsed = time.time() - t0
    print(f"  {img_path.name}: done in {elapsed:.1f}s")

    return raw_bytes, preview_bytes


def _convert_and_store(img_path, content_hash):
    """Convert one image (or load from cache) and store in pool."""
    global _converting_count
    cache_dir = _get_cache_dir()
    try:
        # Try disk cache first
        cached = _load_from_cache(cache_dir, img_path, content_hash)
        if cached:
            raw_bytes, preview_bytes = cached
            print(f"  Cache hit: {img_path.name}")
        else:
            with _lock:
                _converting_count += 1
            try:
                raw_bytes, preview_bytes = _convert_image(img_path)
                _save_to_cache(cache_dir, img_path, content_hash, raw_bytes, preview_bytes)
            finally:
                with _lock:
                    _converting_count -= 1

        with _lock:
            _pool[str(img_path)] = {
                "binary": raw_bytes,
                "preview_png": preview_bytes,
                "hash": content_hash,
            }
        print(f"  Pool: {img_path.name} ready ({len(_pool)} total)")
    except Exception as e:
        print(f"  Error converting {img_path.name}: {e}")


def _sync_pool():
    """Convert any new/changed images, remove deleted ones from pool and cache."""
    cache_dir = _get_cache_dir()
    image_paths = _list_images()
    current_paths = set()

    for img_path in image_paths:
        key = str(img_path)
        current_paths.add(key)

        # Hash one file at a time to keep memory predictable
        content_hash = _hash_file(img_path)
        with _lock:
            existing = _pool.get(key)
            if existing and existing["hash"] == content_hash:
                continue

        print(f"  Processing: {img_path.name}")
        _convert_and_store(img_path, content_hash)

    # Remove pool entries for deleted files
    with _lock:
        for key in list(_pool.keys()):
            if key not in current_paths:
                del _pool[key]
                print(f"  Pool: removed deleted file {key}")

    # Purge stale cache files that no longer match any current source image
    with _lock:
        valid_cache_keys = set()
        for key, entry in _pool.items():
            valid_cache_keys.add(_cache_key(Path(key), entry["hash"]))
    _purge_stale_cache(cache_dir, valid_cache_keys)


def _background_watcher():
    """Background thread: polls for new/changed/deleted images."""
    while True:
        try:
            _sync_pool()
        except Exception as e:
            print(f"  Watcher error: {e}")
        time.sleep(POLL_INTERVAL)


# ── Flask routes ───────────────────────────────────────────────────

@app.route("/hokku/")
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

        # Save database after update
        _save_database(_get_cache_dir(), _database)

    sleep_seconds = _calculate_sleep_seconds(_config)
    print(f"  Serving: {_last_served['name']} (sleep_seconds={sleep_seconds})")

    response = make_response(entry["binary"])
    response.headers["Content-Type"] = "application/octet-stream"
    response.headers["X-Sleep-Seconds"] = str(sleep_seconds)
    response.headers["Content-Disposition"] = "attachment; filename=hokku.bin"
    return response


@app.route("/hokku/preview")
def serve_preview():
    with _lock:
        data = _last_served["preview_png"]
    if data is None:
        return "No image served yet", 404
    return send_file(
        BytesIO(data),
        mimetype="image/png",
        download_name="preview.png",
    )


@app.route("/hokku/status")
def serve_status():
    with _lock:
        pool_files = [Path(k).name for k in _pool.keys()]
        serve_data = _database.get("serve_data", {})
        return jsonify({
            "upload_dir": str(_get_upload_dir()),
            "cache_dir": str(_get_cache_dir()),
            "pool_size": len(_pool),
            "pool_files": pool_files,
            "last_served": _last_served["name"],
            "converting": _converting_count,
            "ready": len(_pool) > 0,
            "serve_data": serve_data,
            "config": {
                "timezone": _config["timezone"],
                "refresh_image_at_time": _config["refresh_image_at_time"],
                "port": _config["port"],
            },
        })


@app.route("/hokku/clear_cache")
def clear_cache():
    _clear_cache_files(_get_cache_dir())
    with _lock:
        _pool.clear()
    # Trigger re-conversion in background
    threading.Thread(target=_sync_pool, daemon=True).start()
    return jsonify({"status": "cache cleared, re-converting"})


# ── Main ───────────────────────────────────────────────────────────

def main():
    global _config, _database

    _config = _load_config()

    upload_dir = _get_upload_dir()
    cache_dir = _get_cache_dir()

    upload_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Load database
    _database = _load_database(cache_dir)

    port = _config["port"]

    print(f"Hokku image server (full resolution: {VISUAL_W}x{VISUAL_H})")
    print(f"  Upload dir: {upload_dir}")
    print(f"  Cache dir:  {cache_dir}")
    print(f"  Timezone:   {_config['timezone']}")
    print(f"  Refresh at: {_config['refresh_image_at_time']}")
    print(f"  Poll interval: {POLL_INTERVAL}s")
    print(f"  Output: {TOTAL_BYTES} bytes per image ({PANEL_BYTES} per panel)")
    print(f"  Endpoints:")
    print(f"    GET /hokku/             — 960K binary (fair rotation) + X-Sleep-Seconds header")
    print(f"    GET /hokku/preview      — PNG preview of last served")
    print(f"    GET /hokku/status       — JSON pool status")
    print(f"    GET /hokku/clear_cache  — Wipe cache and re-convert")

    # Load from cache or convert on startup
    images = _list_images()
    if images:
        print(f"  Found {len(images)} image(s), loading...")
        _sync_pool()
        print(f"  Pool ready: {len(_pool)} image(s)")
    else:
        print(f"  No images found yet, waiting for uploads...")

    # Start background watcher thread
    watcher = threading.Thread(target=_background_watcher, daemon=True)
    watcher.start()

    print(f"  Starting server on port {port}...")
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
