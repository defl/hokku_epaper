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
from werkzeug.utils import secure_filename
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

VALID_DITHER_ALGORITHMS = ("floyd_steinberg", "atkinson", "atkinson_hue_aware", "fs_hue_aware")

DEFAULT_CONFIG = {
    "timezone": "America/Chicago",
    "refresh_image_at_time": ["0600", "1200", "1800"],
    "upload_dir": "/images/upload",
    "cache_dir": "/images/cache",
    "port": 8080,
    "poll_interval_seconds": 10,
    "orientation": "landscape",
    # Debug-mode flag: when true, every HTTP response to /hokku/screen/
    # sets X-Sleep-Seconds to DEBUG_FAST_REFRESH_SECONDS regardless of
    # the configured refresh schedule. Screens cycle through images
    # fast for visual testing of the dithering pipeline. Drains battery
    # hard — not for production use. Toggled from the web GUI.
    "debug_fast_refresh": False,
    "dither_algorithm": "fs_hue_aware",
}

DEBUG_FAST_REFRESH_SECONDS = 180

_config_file_path = None  # set during load, used for saving


_SAVE_KEYS = ["timezone", "refresh_image_at_time", "upload_dir", "cache_dir",
              "port", "poll_interval_seconds", "orientation",
              "debug_fast_refresh", "dither_algorithm"]


def _load_config():
    """Load config and ensure the write destination is owned by us.

    Search order for loading existing config:
      1. HOKKU_CONFIG env var (set by the Debian service unit)
      2. ./config.json (dev runs)
      3. /var/lib/hokku/config.json (current Debian install path)
      4. /etc/hokku/config.json (legacy pre-2.1.14 install path)

    Write destination: HOKKU_CONFIG if set, else the path we loaded from,
    else ./config.json.

    If the write destination is missing OR not writable by us (common
    after a Debian install where postinst created config.json as root
    before systemd chowned StateDirectory to the DynamicUser), rewrite
    it from the loaded content so it ends up owned by the service user.
    This is what makes saves from the web UI actually work without
    requiring the admin to manually chown anything."""
    global _config_file_path
    config = dict(DEFAULT_CONFIG)

    env_path = os.environ.get("HOKKU_CONFIG")
    candidates = []
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path("./config.json"))
    candidates.append(Path("/var/lib/hokku/config.json"))
    candidates.append(Path("/etc/hokku/config.json"))

    loaded_path = None
    for path in candidates:
        if path.exists():
            try:
                with open(path) as f:
                    user_config = json.load(f)
                config.update(user_config)
                loaded_path = path
                print(f"  Config loaded from: {path}")
                break
            except (json.JSONDecodeError, OSError) as e:
                print(f"  Warning: failed to load config from {path}: {e}")

    if loaded_path is None:
        print("  No config file found, using defaults")

    # Pick where we're going to save. Prefer HOKKU_CONFIG so upgrades
    # migrate legacy /etc/hokku/config.json into the new location.
    if env_path:
        write_path = Path(env_path)
    elif loaded_path is not None:
        write_path = loaded_path
    else:
        write_path = Path("./config.json")

    # Heal ownership / create if missing. os.access follows POSIX perms
    # which is what we care about here (the service's own dynamic UID).
    need_rewrite = (not write_path.exists()) or (not os.access(write_path, os.W_OK))
    if need_rewrite:
        save_data = {k: config[k] for k in _SAVE_KEYS if k in config}
        try:
            write_path.parent.mkdir(parents=True, exist_ok=True)
            if write_path.exists():
                # Root-owned legacy file — unlinking works because the parent
                # (/var/lib/hokku) is the StateDirectory, owned by our UID.
                write_path.unlink()
            with open(write_path, "w") as f:
                json.dump(save_data, f, indent=2)
            action = "created" if loaded_path is None else "rewritten (ownership fix)"
            print(f"  Config {action} at: {write_path}")
        except OSError as e:
            print(f"  Warning: couldn't make config writable at {write_path}: {e}")
            # Fall through — _save_config will surface the real error on first write.

    _config_file_path = write_path
    return config


def _save_config(config):
    """Save config to the file chosen during _load_config."""
    path = _config_file_path or Path("./config.json")
    save_data = {k: config[k] for k in _SAVE_KEYS if k in config}
    with open(path, "w") as f:
        json.dump(save_data, f, indent=2)
    print(f"  Config saved to: {path}")


def _calculate_sleep_seconds(config):
    """Calculate seconds until next refresh_image_at_time based on server's clock and timezone.

    Debug Fast Refresh: if config["debug_fast_refresh"] is true we short-
    circuit to DEBUG_FAST_REFRESH_SECONDS regardless of schedule. Screens
    then cycle through images fast for visual testing; don't leave enabled
    long — it drains battery hard."""
    if config.get("debug_fast_refresh"):
        return DEBUG_FAST_REFRESH_SECONDS

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


# Look for templates in: ./templates/ (dev), or /usr/share/hokku-server/templates/ (deb install)
_template_dirs = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"),
    "/usr/share/hokku-server/templates",
]
_template_folder = next((d for d in _template_dirs if os.path.isdir(d)), "templates")
app = Flask(__name__, template_folder=_template_folder)

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

def _compress_dynamic_range(img_array, scale_chroma=False):
    """Compress image luminance from full [0,100] L* to display's actual range.

    When scale_chroma is True ("vividness-preserved" mode), a* and b* are scaled
    by the same ratio used on L*. This keeps the chroma-to-lightness ratio of the
    source intact, so saturated mid-tones don't drift out of the palette's
    reachable gamut after L is pulled in. Required for fs_hue_aware; optional
    for the other algorithms.
    """
    rgb = np.asarray(img_array, dtype=np.float64)
    linear = _srgb_to_linear(rgb)
    xyz = _linear_to_xyz(linear)
    lab = _xyz_to_lab(xyz)
    L_ratio = (_DISPLAY_WHITE_L - _DISPLAY_BLACK_L) / 100.0
    lab[..., 0] = _DISPLAY_BLACK_L + lab[..., 0] * L_ratio
    if scale_chroma:
        lab[..., 1] *= L_ratio
        lab[..., 2] *= L_ratio
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

def _cache_key(img_path, content_hash, algorithm=None):
    orientation = _config.get("orientation", "landscape")
    if algorithm is None:
        algorithm = _config.get("dither_algorithm", "fs_hue_aware")
    return f"{img_path.stem}_{content_hash[:12]}_{orientation[0]}_{algorithm}"

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

def _read_cached_binary(img_path, content_hash):
    """Load binary bytes for an image from the disk cache. Returns None if missing."""
    key = _cache_key(img_path, content_hash)
    bin_path = _get_cache_dir() / f"{key}.bin"
    if not bin_path.exists():
        return None
    data = bin_path.read_bytes()
    if len(data) != TOTAL_BYTES:
        return None
    return data

def _read_cached_preview(img_path, content_hash):
    """Load preview PNG bytes for an image from the disk cache. Returns None if missing."""
    key = _cache_key(img_path, content_hash)
    png_path = _get_cache_dir() / f"{key}.png"
    if not png_path.exists():
        return None
    return png_path.read_bytes()

def _purge_stale_cache(cache_dir, valid_keys):
    if not cache_dir.exists():
        return
    valid_files = set()
    for key in valid_keys:
        valid_files.add(f"{key}.bin")
        valid_files.add(f"{key}.png")
    for f in cache_dir.iterdir():
        if f.is_dir() or f.name == "database.json":
            continue
        if f.name not in valid_files:
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
_last_served = {"key": None, "name": None, "served_at": None}
_converting_count = 0
_converting_name = None  # name of currently converting image
_converting_total = 0    # total images needing conversion in current batch
_converting_done = 0     # images completed in current batch
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


def _prepare_canvas(img, color_enhance=1.2):
    """Scale image to fit display, enhance for e-ink, produce 1200x1600 native buffer.

    In landscape mode: composites on 1600x1200 canvas, then rotates -90° to 1200x1600.
    In portrait mode: composites directly on 1200x1600 canvas, no rotation needed.

    Returns (canvas, padding_mask) where padding_mask is a boolean numpy array
    (True = padding pixel, should be forced to white after dithering).

    color_enhance is the PIL ImageEnhance.Color factor. Hue-aware dithering
    benefits from a lower saturation boost (~1.05) because it can reproduce warm
    mid-tones without residual error; classic FS needs the full 1.2 to compensate
    for the dithering's tendency to mute color.
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
    canvas = ImageEnhance.Color(canvas).enhance(color_enhance)

    if not portrait:
        # Landscape: rotate -90° CW to get 1200x1600 native format
        canvas = canvas.rotate(-90, expand=True)
        mask = np.rot90(mask, k=3)

    return canvas, mask


def _build_rgb_lut():
    """Nearest-palette LUT by Lab-Euclidean distance (classic)."""
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


def _build_rgb_lut_hue_aware(hue_cutoff_deg=95.0, neutral_chroma=8.0):
    """Nearest-palette LUT that forbids palette picks whose hue differs by more
    than hue_cutoff_deg from the source pixel. Neutrals (palette or pixel with
    chroma < neutral_chroma) are always allowed — black/white stay candidates
    for every pixel.

    Fixes the common failure mode where error diffusion overshoots Red for warm
    skin tones and cascades into Blue picks on neighboring pixels.
    """
    steps = 32
    scale = 256 / steps
    r_vals = np.arange(steps) * scale + scale / 2
    rr, gg, bb = np.meshgrid(r_vals, r_vals, r_vals, indexing='ij')
    rgb_grid = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)
    lab_grid = _rgb_to_lab(rgb_grid)

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


_RGB_LUT_EUCLID, _LUT_SCALE = _build_rgb_lut()
_RGB_LUT_HUE_AWARE, _ = _build_rgb_lut_hue_aware()


def _floyd_steinberg_dither(canvas, lut=None, lut_scale=None):
    """Floyd–Steinberg error diffusion. Full error distributed to 4 neighbors
    (7/16 right, 3/16 down-left, 5/16 down, 1/16 down-right)."""
    if lut is None:
        lut = _RGB_LUT_EUCLID
    if lut_scale is None:
        lut_scale = _LUT_SCALE
    pixels = np.array(canvas, dtype=np.float32)
    h, w, _ = pixels.shape
    result_idx = np.zeros((h, w), dtype=np.uint8)
    pal_rgb = PALETTE_MEASURED_RGB
    lut_max = lut.shape[0] - 1

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


def _atkinson_dither(canvas, lut=None, lut_scale=None):
    """Atkinson dither. Distributes only 6/8 of the quantization error, 1/8 each
    into 6 neighbors: (+1,0), (+2,0), (-1,+1), (0,+1), (+1,+1), (0,+2). Softer
    gradients, less hue-shift cascade, slightly lower max contrast than FS."""
    if lut is None:
        lut = _RGB_LUT_EUCLID
    if lut_scale is None:
        lut_scale = _LUT_SCALE
    pixels = np.array(canvas, dtype=np.float32)
    h, w, _ = pixels.shape
    result_idx = np.zeros((h, w), dtype=np.uint8)
    pal_rgb = PALETTE_MEASURED_RGB
    lut_max = lut.shape[0] - 1
    # (dx, dy)
    offsets = ((1, 0), (2, 0), (-1, 1), (0, 1), (1, 1), (0, 2))
    w_coef = 1.0 / 8.0

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
            er = (r - pal_rgb[idx, 0]) * w_coef
            eg = (g - pal_rgb[idx, 1]) * w_coef
            eb = (b - pal_rgb[idx, 2]) * w_coef
            for dx, dy in offsets:
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    pixels[ny, nx, 0] += er
                    pixels[ny, nx, 1] += eg
                    pixels[ny, nx, 2] += eb
        if y % 200 == 0:
            print(f"  Dithering: {y}/{h}")
    return result_idx


def _dither_for_algorithm(algorithm):
    """Return (dither_fn, lut, color_enhance, scale_chroma) for the algorithm.

    scale_chroma enables vividness-preserved dynamic-range compression (scales
    a*/b* by the same ratio as L*). fs_hue_aware relies on this to keep
    saturated mid-tones reachable after L compression; older algorithms keep
    the legacy L-only behavior.
    """
    if algorithm == "floyd_steinberg":
        return _floyd_steinberg_dither, _RGB_LUT_EUCLID, 1.2, False
    if algorithm == "atkinson":
        return _atkinson_dither, _RGB_LUT_EUCLID, 1.2, False
    if algorithm == "atkinson_hue_aware":
        return _atkinson_dither, _RGB_LUT_HUE_AWARE, 1.05, False
    if algorithm == "fs_hue_aware":
        return _floyd_steinberg_dither, _RGB_LUT_HUE_AWARE, 1.2, True
    # Fallback matches new default so unknown config values don't hard-fail.
    return _floyd_steinberg_dither, _RGB_LUT_HUE_AWARE, 1.2, True


# Images whose max chroma stays below this threshold are treated as near-grayscale
# and rendered with the conservative preset (no vividness scaling, modest enhance).
# Without this, fs_hue_aware's chroma amplification gives B&W photos a pink cast.
_GRAYSCALE_CHROMA_THRESHOLD = 8.0


def _is_near_grayscale(img):
    """Detect near-B&W source images by sampling Lab chroma.

    Downsamples to 200px for speed, converts to Lab, and returns True if the
    95th-percentile chroma is below _GRAYSCALE_CHROMA_THRESHOLD. Downsampling
    rather than sampling avoids being fooled by a single chromatic pixel in
    an otherwise grayscale image.
    """
    thumb = img.copy()
    thumb.thumbnail((200, 200), Image.LANCZOS)
    arr = np.asarray(thumb.convert("RGB"), dtype=np.float64)
    lab = _xyz_to_lab(_linear_to_xyz(_srgb_to_linear(arr)))
    chroma = np.sqrt(lab[..., 1] ** 2 + lab[..., 2] ** 2)
    return float(np.percentile(chroma, 95)) < _GRAYSCALE_CHROMA_THRESHOLD


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

    algorithm = _config.get("dither_algorithm", "fs_hue_aware")
    dither_fn, lut, color_enhance, scale_chroma = _dither_for_algorithm(algorithm)

    # fs_hue_aware's 1.2 color enhance + vividness compression amplifies tiny
    # chroma deviations in near-grayscale images into a visible color cast.
    # Detect B&W inputs and fall back to the conservative preset.
    if scale_chroma and _is_near_grayscale(img):
        print(f"  {img_path.name}: B&W detected, using conservative preset")
        color_enhance, scale_chroma = 1.05, False

    print(f"  {img_path.name}: algorithm={algorithm} enhance={color_enhance} scale_chroma={scale_chroma}")

    canvas, padding_mask = _prepare_canvas(img, color_enhance=color_enhance)
    canvas_array = _compress_dynamic_range(np.array(canvas, dtype=np.float32), scale_chroma=scale_chroma)
    canvas = Image.fromarray(canvas_array.astype(np.uint8))
    result_idx = dither_fn(canvas, lut, _LUT_SCALE)

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
            # Only store metadata in RAM; actual bytes live on disk.
            # This keeps memory flat regardless of pool size.
            _pool[str(img_path)] = {"hash": content_hash}
        print(f"  Pool: {img_path.name} ready ({len(_pool)} total)")
    except Exception as e:
        print(f"  Error converting {img_path.name}: {e}")


_sync_lock = threading.Lock()
_sync_state_lock = threading.Lock()
_sync_pending = False

def _sync_pool():
    """Run a pool sync. If one is already running, mark a rerun-pending flag
    so the in-progress sync loops once more — this guarantees changes made
    during a sync (uploads, deletions) are never silently dropped, while
    still serializing actual sync work."""
    global _sync_pending
    with _sync_state_lock:
        if not _sync_lock.acquire(blocking=False):
            _sync_pending = True
            print("  Sync already running, queued rerun")
            return
        _sync_pending = False
    try:
        while True:
            _sync_pool_inner()
            with _sync_state_lock:
                if not _sync_pending:
                    break
                _sync_pending = False
    finally:
        _sync_lock.release()

def _sync_pool_inner():
    global _converting_total, _converting_done
    cache_dir = _get_cache_dir()
    image_paths = _list_images()
    current_paths = set()

    # Fast pre-pass: build any missing thumbnails before the slow dither loop.
    # Cheap (~tens of ms each) and lets the web UI show previews for every
    # uploaded image immediately instead of waiting for dithering to reach it.
    for img_path in image_paths:
        if not _thumb_path_for(img_path).exists():
            _ensure_thumbnail(img_path)

    # Count how many need conversion
    to_convert = []
    for img_path in image_paths:
        key = str(img_path)
        current_paths.add(key)
        content_hash = _hash_file(img_path)
        with _lock:
            existing = _pool.get(key)
            if existing and existing["hash"] == content_hash:
                continue
        to_convert.append((img_path, content_hash))

    with _lock:
        _converting_total = len(to_convert)
        _converting_done = 0

    for img_path, content_hash in to_convert:
        print(f"  Processing: {img_path.name}")
        _convert_and_store(img_path, content_hash)
        with _lock:
            _converting_done += 1

    with _lock:
        _converting_total = 0
        _converting_done = 0

    with _lock:
        for key in list(_pool.keys()):
            if key not in current_paths:
                del _pool[key]
                print(f"  Pool: removed deleted file {key}")

    with _lock:
        valid_cache_keys = set()
        for key, entry in _pool.items():
            # Keep cached renders for every algorithm, not just the active one,
            # so toggling the dither dropdown is instant on a re-visit.
            for algo in VALID_DITHER_ALGORITHMS:
                valid_cache_keys.add(_cache_key(Path(key), entry["hash"], algorithm=algo))
    _purge_stale_cache(cache_dir, valid_cache_keys)


def _background_watcher():
    while True:
        try:
            _sync_pool()
        except Exception as e:
            print(f"  Watcher error: {e}")
        time.sleep(_config.get("poll_interval_seconds", 10))


# ── Flask routes: device endpoints ─────────────────────────────────

def _record_screen_call(screen_name, screen_ip, sleep_seconds, served_name=None,
                        battery_mv=None, frame_state=None):
    """Track a call from a screen: IP, count, last_seen, and the next update
    time implied by the sleep_seconds we just handed it. Caller must hold _lock.

    `frame_state` is the parsed X-Frame-State dict (or None). Stored as a
    whole-dict snapshot under screens[name]["state"] so new firmware keys
    surface in the UI automatically. battery_mv can come in separately
    (legacy header) or from frame_state["bat_mv"] — either way it ends up
    in the same top-level fields used by the existing Battery column."""
    if "screens" not in _database:
        _database["screens"] = {}
    screens = _database["screens"]
    if screen_name not in screens:
        screens[screen_name] = {"ip": screen_ip, "request_count": 0, "last_seen": None}
    now = datetime.now()
    screens[screen_name]["ip"] = screen_ip
    screens[screen_name]["request_count"] += 1
    screens[screen_name]["last_seen"] = now.isoformat(timespec="seconds")
    screens[screen_name]["last_sleep_seconds"] = int(sleep_seconds)
    # The firmware sleeps for sleep_seconds after this call, so the next
    # attempted fetch lands around now + sleep_seconds. Shown in the Web GUI
    # so you can tell at a glance whether a screen is about to wake up.
    screens[screen_name]["next_update_at"] = (now + timedelta(seconds=int(sleep_seconds))).isoformat(timespec="seconds")
    if served_name is not None:
        screens[screen_name]["last_served"] = served_name

    # Prefer battery from frame_state (current firmware). Fall back to the
    # legacy X-Battery-mV header if that's all we got (older firmware).
    if frame_state and isinstance(frame_state.get("bat_mv"), (int, float)):
        fs_mv = _parse_battery_header(str(int(frame_state["bat_mv"])))
        if fs_mv is not None:
            battery_mv = fs_mv

    if battery_mv is not None and battery_mv > 0:
        screens[screen_name]["battery_mv"] = int(battery_mv)
        screens[screen_name]["battery_percent"] = _battery_percent(battery_mv)
        screens[screen_name]["battery_seen_at"] = now.isoformat(timespec="seconds")

    if frame_state:
        # Store the whole dict so new firmware keys surface automatically.
        # Compute clk_drift_s by comparing the firmware's reported clock
        # (clk_now — set from the previous X-Server-Time-Epoch and then
        # free-running across deep sleep) to our own wall clock. Positive
        # = firmware thinks it's later than it actually is. Gives us a
        # direct measurement of RTC-slow-clock drift over the sleep period.
        state_with_meta = dict(frame_state)
        clk_now = frame_state.get("clk_now")
        if isinstance(clk_now, (int, float)) and clk_now > 0:
            state_with_meta["clk_drift_s"] = int(clk_now - time.time())
        state_with_meta["seen_at"] = now.isoformat(timespec="seconds")
        screens[screen_name]["state"] = state_with_meta


# Li-ion voltage → percentage mapping. Chosen without a discharge
# experiment, based on two known anchors:
#   - 3400 mV: firmware's BATT_LOW_MV — below this the display refresh
#     draws enough current to risk brownout, so we call it "0%"
#   - 4100 mV: HARDWARE_FACTS ADC calibration reference ("battery is 4.1V");
#     conservative full-charge target for longevity
# Linear in between. Li-ion discharge is actually S-shaped (flat middle,
# steep ends), so this over-reports near full and under-reports near empty
# by ~10–20 pp mid-range, which is a fine trade-off for a user-facing
# "is my battery OK" indicator. The absolute mV is also shown for anyone
# who wants the precise value.
BATTERY_MV_EMPTY = 3400
BATTERY_MV_FULL = 4100

def _battery_percent(mv):
    if mv is None or mv <= 0:
        return None
    pct = round((mv - BATTERY_MV_EMPTY) * 100 / (BATTERY_MV_FULL - BATTERY_MV_EMPTY))
    return max(0, min(100, pct))


def _parse_battery_header(raw):
    """Tolerant parse of the X-Battery-mV header. Returns int mV or None.

    Kept for backwards compatibility with firmware that still sends the
    standalone X-Battery-mV header. Current firmware folds battery into
    the X-Frame-State JSON (bat_mv field); _parse_frame_state extracts
    it from there. Either path populates the same screens[name] fields."""
    if not raw:
        return None
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    # Reasonable sanity bounds: anything outside [2000, 5000] is garbage.
    if v < 2000 or v > 5000:
        return None
    return v


def _parse_frame_state(raw):
    """Parse the X-Frame-State JSON header from firmware.

    Returns a dict of whatever keys were present (firmware may add new
    fields over time; we just store them all). None if the header was
    missing, empty, or not valid JSON. Malformed headers log a warning
    rather than raising, so a misbehaving frame doesn't take down serve_binary."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as e:
        print(f"  Warning: X-Frame-State not valid JSON ({e}): {str(raw)[:120]}")
        return None
    if not isinstance(data, dict):
        print(f"  Warning: X-Frame-State is not a JSON object: {str(raw)[:120]}")
        return None
    return data


def _busy_retry_seconds():
    """Sleep hint to return when the server can't serve an image yet (pool
    empty while conversion is running). Short enough that the screen comes
    back quickly once conversion finishes, but capped at the next scheduled
    refresh so we never push a screen PAST its configured wake time."""
    return min(300, _calculate_sleep_seconds(_config))


@app.route("/hokku/screen/", strict_slashes=False)
def serve_binary():
    global _database
    screen_name = request.headers.get("X-Screen-Name", "unnamed")
    screen_ip = request.remote_addr or "unknown"
    battery_mv = _parse_battery_header(request.headers.get("X-Battery-mV"))
    frame_state = _parse_frame_state(request.headers.get("X-Frame-State"))

    with _lock:
        if not _pool:
            # No image available yet — still record the call and hand out a
            # sensible retry hint so the firmware doesn't fall back to its
            # 3h hard-coded default (which drifts off the refresh schedule).
            sleep_seconds = _busy_retry_seconds()
            _record_screen_call(screen_name, screen_ip, sleep_seconds,
                                battery_mv=battery_mv, frame_state=frame_state)
            _save_database(_get_cache_dir(), _database)
            status = 503 if _converting_count > 0 else 404
            msg = "Converting images, try again shortly" if status == 503 else "No images in upload directory"
            resp = make_response(msg, status)
            resp.headers["X-Sleep-Seconds"] = str(sleep_seconds)
            print(f"  Busy: {screen_name} told to retry in {sleep_seconds}s (status={status})")
            return resp

        key = _pick_next_image(_pool, _database)
        if key is None:
            sleep_seconds = _busy_retry_seconds()
            _record_screen_call(screen_name, screen_ip, sleep_seconds,
                                battery_mv=battery_mv, frame_state=frame_state)
            _save_database(_get_cache_dir(), _database)
            resp = make_response("No images available", 404)
            resp.headers["X-Sleep-Seconds"] = str(sleep_seconds)
            return resp

        entry = _pool[key]
        content_hash = entry["hash"]

        sleep_seconds = _calculate_sleep_seconds(_config)
        _record_screen_call(screen_name, screen_ip, sleep_seconds,
                            served_name=Path(key).name, battery_mv=battery_mv,
                            frame_state=frame_state)
        _save_database(_get_cache_dir(), _database)

    # Load binary from disk cache outside the lock
    binary = _read_cached_binary(Path(key), content_hash)
    if binary is None:
        # Cache was purged between _pool lookup and read — tell the screen
        # to retry soon rather than using its 3h fallback.
        resp = make_response("Cached binary missing, try again shortly", 503)
        resp.headers["X-Sleep-Seconds"] = str(_busy_retry_seconds())
        return resp

    # Update _last_served for the UI preview (no need to hold the binary here)
    _last_served["key"] = key
    _last_served["name"] = Path(key).name
    _last_served["served_at"] = datetime.now().isoformat(timespec="seconds")

    print(f"  Serving: {Path(key).name} to {screen_name} (sleep_seconds={sleep_seconds})")

    response = make_response(binary)
    response.headers["Content-Type"] = "application/octet-stream"
    response.headers["X-Sleep-Seconds"] = str(sleep_seconds)
    response.headers["X-Server-Time-Epoch"] = str(int(time.time()))
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
        pool_set = set(pool_files)
        serve_data = _database.get("serve_data", {})

        # Include every file in the upload dir, marking whether the dithered
        # conversion is ready yet. The frontend uses this to render a card
        # for pending images with a "generating preview" badge.
        all_upload = sorted(p.name for p in _list_images())
        upload_files = [{"name": n, "dithered": n in pool_set} for n in all_upload]

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
            "upload_files": upload_files,
            "pool_size": len(_pool),
            "upload_size": len(upload_files),
            "serve_data": enriched,
            "screens": screens,
            "last_served": _last_served["name"],
            "converting": _converting_count,
            "converting_name": _converting_name,
            "converting_total": _converting_total,
            "converting_done": _converting_done,
            "server_time": now_str,
            "config": {
                "timezone": _config["timezone"],
                "refresh_image_at_time": _config["refresh_image_at_time"],
                "port": _config["port"],
                "poll_interval_seconds": _config.get("poll_interval_seconds", 10),
                "orientation": _config.get("orientation", "landscape"),
                "debug_fast_refresh": bool(_config.get("debug_fast_refresh", False)),
                "debug_fast_refresh_seconds": DEBUG_FAST_REFRESH_SECONDS,
                "dither_algorithm": _config.get("dither_algorithm", "fs_hue_aware"),
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

def _thumb_path_for(img_path):
    return _get_cache_dir() / "thumbs" / (img_path.stem + "_thumb.jpg")

def _ensure_thumbnail(img_path):
    """Build the thumbnail for img_path if it doesn't exist or is stale.
    Returns the cached thumbnail Path on success, None on failure. Cheap
    enough (~tens of ms) to call eagerly during _sync_pool so the grid
    can show previews before the slow dither finishes."""
    thumb_path = _thumb_path_for(img_path)
    try:
        if thumb_path.exists() and thumb_path.stat().st_mtime >= img_path.stat().st_mtime:
            return thumb_path
    except OSError:
        pass
    with _thumb_lock:
        try:
            if thumb_path.exists() and thumb_path.stat().st_mtime >= img_path.stat().st_mtime:
                return thumb_path
            from PIL import ImageOps
            thumb_path.parent.mkdir(parents=True, exist_ok=True)
            img = Image.open(img_path)
            img = ImageOps.exif_transpose(img)
            # JPEG can't encode alpha — flatten RGBA / LA / P-with-transparency
            # onto a white canvas before saving.
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                img = img.convert("RGBA")
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((300, 300), Image.LANCZOS)
            img.save(thumb_path, format="JPEG", quality=80)
            return thumb_path
        except Exception as e:
            print(f"  Thumbnail error: {img_path.name}: {e}")
            return None

@app.route("/hokku/api/thumbnail/<filename>")
def api_thumbnail(filename):
    """Serve a cached thumbnail of the original image (~300px wide)."""
    img_path = _get_upload_dir() / filename
    if not img_path.exists() or not img_path.is_file():
        abort(404)
    thumb_path = _ensure_thumbnail(img_path)
    if thumb_path is None:
        abort(500)
    return send_file(thumb_path, mimetype="image/jpeg")


@app.route("/hokku/api/dithered/<filename>")
def api_dithered(filename):
    """Serve the dithered preview PNG for an image (loaded from disk cache on demand)."""
    with _lock:
        match = None
        for key, entry in _pool.items():
            if Path(key).name == filename:
                match = (key, entry["hash"])
                break
    if match is None:
        abort(404)
    key, content_hash = match
    preview = _read_cached_preview(Path(key), content_hash)
    if preview is None:
        abort(404)
    return send_file(BytesIO(preview), mimetype="image/png")


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

    if "debug_fast_refresh" in data:
        _config["debug_fast_refresh"] = bool(data["debug_fast_refresh"])
        changed = True

    algorithm_changed = False
    if "dither_algorithm" in data:
        val = data["dither_algorithm"]
        if val not in VALID_DITHER_ALGORITHMS:
            return jsonify({"error": f"Unknown dither_algorithm: {val}"}), 400
        if _config.get("dither_algorithm", "fs_hue_aware") != val:
            algorithm_changed = True
        _config["dither_algorithm"] = val
        changed = True

    if changed:
        _save_config(_config)

    if orientation_changed:
        # Orientation affects image processing, so clear cache and re-convert
        _clear_cache_files(_get_cache_dir())
        with _lock:
            _pool.clear()
        threading.Thread(target=_sync_pool, daemon=True).start()
    elif algorithm_changed:
        # Algorithm change: cache key changes so pool entries won't find matching
        # renders. Drop the pool to force _sync_pool to re-run _convert_and_store,
        # which hits cache for algorithms we've rendered before (instant) and
        # falls through to _convert_image for new ones.
        with _lock:
            _pool.clear()
        threading.Thread(target=_sync_pool, daemon=True).start()

    return jsonify({"status": "ok", "config": {
        "timezone": _config["timezone"],
        "refresh_image_at_time": _config["refresh_image_at_time"],
        "poll_interval_seconds": _config.get("poll_interval_seconds", 10),
        "orientation": _config.get("orientation", "landscape"),
        "debug_fast_refresh": bool(_config.get("debug_fast_refresh", False)),
        "dither_algorithm": _config.get("dither_algorithm", "fs_hue_aware"),
    }})


@app.route("/hokku/api/upload", methods=["POST"])
def api_upload():
    """Accept one or more uploaded image files. Triggers background re-sync."""
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files provided"}), 400

    upload_dir = _get_upload_dir()
    try:
        upload_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return jsonify({"error": f"Upload directory not writable ({upload_dir}): {e.strerror or e}"}), 500

    saved = []
    skipped = []
    for f in files:
        if not f or not f.filename:
            continue
        # secure_filename strips path components and unsafe chars but preserves extension
        safe_name = secure_filename(f.filename)
        if not safe_name:
            skipped.append({"filename": f.filename, "reason": "invalid filename"})
            continue
        ext = Path(safe_name).suffix.lower()
        if ext not in IMAGE_EXTENSIONS:
            skipped.append({"filename": f.filename, "reason": f"unsupported type {ext}"})
            continue

        # Avoid clobbering an existing file by suffixing _1, _2, ...
        dest = upload_dir / safe_name
        if dest.exists():
            stem = dest.stem
            n = 1
            while dest.exists():
                dest = upload_dir / f"{stem}_{n}{ext}"
                n += 1

        try:
            f.save(dest)
        except OSError as e:
            # Most common culprit: systemd ProtectSystem=strict blocking writes
            # outside StateDirectory. Return a structured error so the UI can show
            # something more useful than Flask's HTML 500 page.
            print(f"  Upload error: {dest}: {e}")
            return jsonify({
                "error": f"Failed to save {dest.name}: {e.strerror or e}",
                "saved": saved,
                "skipped": skipped,
            }), 500
        saved.append(dest.name)
        print(f"  Upload: saved {dest.name}")

    if saved:
        threading.Thread(target=_sync_pool, daemon=True).start()

    return jsonify({"status": "ok", "saved": saved, "skipped": skipped})


@app.route("/hokku/api/image/<filename>", methods=["DELETE"])
def api_delete_image(filename):
    """Delete an uploaded image and its associated cache files."""
    safe_name = secure_filename(filename)
    if not safe_name:
        return jsonify({"error": "Invalid filename"}), 400

    img_path = _get_upload_dir() / safe_name
    if not img_path.exists() or not img_path.is_file():
        return jsonify({"error": "Image not found"}), 404

    # Remove the cached thumbnail (matched by stem; _sync_pool ignores the thumbs/ dir)
    thumb_path = _get_cache_dir() / "thumbs" / (img_path.stem + "_thumb.jpg")
    if thumb_path.exists():
        try:
            thumb_path.unlink()
        except OSError:
            pass

    try:
        img_path.unlink()
    except OSError as e:
        print(f"  Delete error: {img_path}: {e}")
        return jsonify({"error": f"Failed to delete {safe_name}: {e.strerror or e}"}), 500
    print(f"  Delete: removed {safe_name}")

    # _sync_pool drops the pool entry and purges the matching cache .bin/.png
    threading.Thread(target=_sync_pool, daemon=True).start()

    return jsonify({"status": "ok", "deleted": safe_name})


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
