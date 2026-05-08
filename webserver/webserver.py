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
import copy
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

# ── Dither pipeline presets ────────────────────────────────────────
# Every preset is a full, self-contained dither config. The frontend
# displays preset names as a dropdown; touching any knob flips the
# label to "Custom" but keeps the resolved config exactly as the user
# edited it. Adding a new preset is a one-liner here — no other code
# paths need to know its name.

DITHER_PRESETS = {
    "floyd_steinberg_vivid": {
        "label": "Floyd–Steinberg — vivid, warm (pre-2.0)",
        "description": (
            "Classic error diffusion with a flat chroma boost. "
            "Warm skin tones and vivid reds at the cost of visible speckle "
            "in near-white regions. Matches the pre-2.0 default output."
        ),
        "dither": {
            "autocontrast": {"enabled": True, "cutoff": 0.5},
            "gamma":        {"enabled": True, "value": 0.85},
            "brightness":   1.0,
            "contrast":     1.1,
            "sharpness":    1.3,
            "saturation":   {"mode": "global", "value": 1.2,
                             "low": 5.0, "high": 15.0},
            "drc":          {"enabled": True, "chroma_mode": "off",
                             "vivid_low": 5.0, "vivid_high": 15.0},
            "palette_lut":  {"mode": "euclidean",
                             "hue_cutoff_deg": 95.0, "neutral_chroma": 8.0},
            "bw_fallback":  {"enabled": False,
                             "chroma_threshold": 8.0, "percentile": 95},
            "kernel":       "floyd_steinberg",
        },
    },
    "atkinson_soft": {
        "label": "Atkinson — soft, warm",
        "description": (
            "Sparser Atkinson kernel with the same warm/vivid colour "
            "pipeline. Cleaner gradients than Floyd–Steinberg while keeping "
            "the warm skin tones."
        ),
        "dither": {
            "autocontrast": {"enabled": True, "cutoff": 0.5},
            "gamma":        {"enabled": True, "value": 0.85},
            "brightness":   1.0,
            "contrast":     1.1,
            "sharpness":    1.3,
            "saturation":   {"mode": "global", "value": 1.2,
                             "low": 5.0, "high": 15.0},
            "drc":          {"enabled": True, "chroma_mode": "off",
                             "vivid_low": 5.0, "vivid_high": 15.0},
            "palette_lut":  {"mode": "euclidean",
                             "hue_cutoff_deg": 95.0, "neutral_chroma": 8.0},
            "bw_fallback":  {"enabled": False,
                             "chroma_threshold": 8.0, "percentile": 95},
            "kernel":       "atkinson",
        },
    },
    "atkinson_hue_aware": {
        "label": "Atkinson — hue-aware (default)",
        "description": (
            "V10 recipe. Hue-aware palette mapping prevents near-neutral "
            "pixels from snapping to red/blue; adaptive saturation boosts "
            "only already-colourful pixels; adaptive vividness keeps whites "
            "clean. Best for photos; less warm on faces than the legacy "
            "presets."
        ),
        "dither": {
            "autocontrast": {"enabled": True, "cutoff": 0.5},
            "gamma":        {"enabled": True, "value": 0.85},
            "brightness":   1.0,
            "contrast":     1.1,
            "sharpness":    1.3,
            "saturation":   {"mode": "adaptive", "value": 1.25,
                             "low": 5.0, "high": 15.0},
            "drc":          {"enabled": True, "chroma_mode": "adaptive_vivid",
                             "vivid_low": 5.0, "vivid_high": 15.0},
            "palette_lut":  {"mode": "hue_aware",
                             "hue_cutoff_deg": 95.0, "neutral_chroma": 8.0},
            "bw_fallback":  {"enabled": True,
                             "chroma_threshold": 8.0, "percentile": 95},
            "kernel":       "atkinson",
        },
    },
    "stucki_hue_aware": {
        "label": "Stucki — hue-aware",
        "description": (
            "Stucki error diffusion with the same V10 pipeline knobs as "
            "Atkinson + hue-aware (hue-aware LUT, adaptive saturation, "
            "adaptive vividness). The wider 12-neighbor kernel spreads "
            "error farther than Atkinson — often a bit sharper with a "
            "different noise grain."
        ),
        "dither": {
            "autocontrast": {"enabled": True, "cutoff": 0.5},
            "gamma":        {"enabled": True, "value": 0.85},
            "brightness":   1.0,
            "contrast":     1.1,
            "sharpness":    1.3,
            "saturation":   {"mode": "adaptive", "value": 1.25,
                             "low": 5.0, "high": 15.0},
            "drc":          {"enabled": True, "chroma_mode": "adaptive_vivid",
                             "vivid_low": 5.0, "vivid_high": 15.0},
            "palette_lut":  {"mode": "hue_aware",
                             "hue_cutoff_deg": 95.0, "neutral_chroma": 8.0},
            "bw_fallback":  {"enabled": True,
                             "chroma_threshold": 8.0, "percentile": 95},
            "kernel":       "stucki",
        },
    },
    "stucki": {
        "label": "Stucki (no hue correction)",
        "description": (
            "Plain Stucki with Lab-Euclidean palette matching. Same "
            "tradeoffs as plain Atkinson vs hue-aware — sharper detail "
            "but may show blue speckle on warm skin tones."
        ),
        "dither": {
            "autocontrast": {"enabled": True, "cutoff": 0.5},
            "gamma":        {"enabled": True, "value": 0.85},
            "brightness":   1.0,
            "contrast":     1.1,
            "sharpness":    1.3,
            "saturation":   {"mode": "global", "value": 1.2,
                             "low": 5.0, "high": 15.0},
            "drc":          {"enabled": True, "chroma_mode": "off",
                             "vivid_low": 5.0, "vivid_high": 15.0},
            "palette_lut":  {"mode": "euclidean",
                             "hue_cutoff_deg": 95.0, "neutral_chroma": 8.0},
            "bw_fallback":  {"enabled": False,
                             "chroma_threshold": 8.0, "percentile": 95},
            "kernel":       "stucki",
        },
    },
}

DEFAULT_PRESET = "atkinson_hue_aware"


def _default_dither_config():
    """Fresh deep copy of the default preset's dither config."""
    return copy.deepcopy(DITHER_PRESETS[DEFAULT_PRESET]["dither"])


def _normalize_dither_config(value):
    """Fill in any missing keys from the default preset. Returns a fresh dict.
    Ignores keys that aren't part of the schema so bad saved state doesn't
    leak into the pipeline."""
    default = _default_dither_config()
    if not isinstance(value, dict):
        return default
    out = default
    for key, new_val in value.items():
        if key not in out:
            continue
        if isinstance(out[key], dict) and isinstance(new_val, dict):
            for inner_key, inner_val in new_val.items():
                if inner_key in out[key]:
                    out[key][inner_key] = inner_val
        else:
            out[key] = new_val
    return out


def _dither_config_hash(dither_cfg):
    """Stable 8-char hash of a dither config — used in the cache key so any
    knob change invalidates just the affected renders."""
    blob = json.dumps(dither_cfg, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode()).hexdigest()[:8]


# ── Configuration ──────────────────────────────────────────────────

# Single source of truth for where images and cache live. Used by both the
# Debian install (StateDirectory=hokku; DynamicUser can write here) and dev
# runs from source. Dev users who want a different location override in their
# own config.json.
_DEFAULT_UPLOAD_DIR = "/var/lib/hokku/upload"
_DEFAULT_CACHE_DIR = "/var/lib/hokku/cache"

DEFAULT_CONFIG = {
    # Timezone is read from the host OS at runtime (set via `timedatectl` on
    # the Pi install, or the dev's workstation zone otherwise). No longer
    # a stored config key — the server always follows system-local time.
    "refresh_image_at_time": ["0600", "1200", "1800"],
    "upload_dir": _DEFAULT_UPLOAD_DIR,
    "cache_dir": _DEFAULT_CACHE_DIR,
    "port": 8080,
    "poll_interval_seconds": 10,
    "orientation": "landscape",
    # Debug-mode flag: when true, every HTTP response to /hokku/screen/
    # sets X-Sleep-Seconds to DEBUG_FAST_REFRESH_SECONDS regardless of
    # the configured refresh schedule. Screens cycle through images
    # fast for visual testing of the dithering pipeline. Drains battery
    # hard — not for production use. Toggled from the web GUI.
    "debug_fast_refresh": False,
    "dither": _default_dither_config(),
    # Serpentine (boustrophedon) error-diffusion scan: alternate row direction and
    # mirror the kernel so error is not always pushed the same way — reduces
    # directional streaks. Applies to all dither kernels.
    "dither_serpentine": False,
}

DEBUG_FAST_REFRESH_SECONDS = 180

_config_file_path = None  # set during load, used for saving


_SAVE_KEYS = ["refresh_image_at_time", "upload_dir", "cache_dir",
              "port", "poll_interval_seconds", "orientation",
              "debug_fast_refresh", "dither", "dither_serpentine"]


def _load_config(config_path):
    """Load config from *config_path* and ensure it is writable by us.

    If the file doesn't exist it is created with defaults. If it exists
    but isn't writable (common after a Debian install where postinst
    created the file as root before systemd chowned StateDirectory to
    the DynamicUser), it is rewritten so it ends up owned by the service
    user — this is what makes saves from the web UI work without
    requiring the admin to manually chown anything."""
    global _config_file_path
    write_path = Path(config_path)

    if not write_path.exists():
        print(f"Error: config file not found: {write_path}")
        exit(1)

    try:
        with open(write_path) as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Error: failed to load config from {write_path}: {e}")
        exit(1)

    config.pop("dither_algorithm", None)
    config["dither"] = _normalize_dither_config(config.get("dither"))
    config.pop("timezone", None)
    print(f"  Config loaded from: {write_path}")

    if not os.access(write_path, os.W_OK):
        try:
            write_path.unlink()
            with open(write_path, "w") as f:
                json.dump({k: config[k] for k in _SAVE_KEYS if k in config}, f, indent=2)
            print(f"  Config rewritten (ownership fix) at: {write_path}")
        except OSError as e:
            print(f"  Warning: couldn't make config writable at {write_path}: {e}")

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

    # System-local time — host's timezone is set via timedatectl on the Pi,
    # or the dev workstation's zone otherwise. No longer a config setting.
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
    """Convert sRGB [0-255] to linear RGB [0-1]. Preserves float input
    dtype (float32 in, float32 out) so the Lab pipeline can stay in
    float32 end-to-end. Integer inputs are promoted to float64 to avoid
    truncation from dividing uint8 by 255.

    Implementation note: an obvious `np.where(cond, toe, shoulder)` call
    would allocate *both* full-size toe and shoulder arrays before
    selecting. On a 1200×1600×3 float32 canvas that doubles the working
    set. Instead we compute the shoulder into a single output buffer
    in-place and then overwrite the sub-threshold pixels with their toe
    values via a boolean mask.
    """
    c = np.asarray(c)
    if not np.issubdtype(c.dtype, np.floating):
        c = c.astype(np.float64)
    dt = c.dtype
    # Build the output buffer we'll own in-place.
    out = c / np.asarray(255.0, dtype=dt)
    toe_mask = out <= np.asarray(0.04045, dtype=dt)
    # Shoulder branch, in place: ((out + 0.055) / 1.055) ** 2.4
    np.add(out, np.asarray(0.055, dtype=dt), out=out)
    np.divide(out, np.asarray(1.055, dtype=dt), out=out)
    np.power(out, np.asarray(2.4, dtype=dt), out=out)
    # Toe values need a different formula. out still holds their shoulder-
    # branch result; overwrite only those entries with c[toe]/12.92.
    # Slight subtlety: out was computed from (c/255.0 + 0.055)/1.055 so the
    # "original" c/255.0 is no longer available — recompute it just for toe
    # pixels, which is a small fraction of typical photos.
    if toe_mask.any():
        toe_src = c[toe_mask].astype(dt, copy=False) / np.asarray(255.0, dtype=dt)
        out[toe_mask] = toe_src / np.asarray(12.92, dtype=dt)
    return out

def _linear_to_xyz(rgb, in_place=False):
    """Convert linear RGB to CIE XYZ (D65 illuminant). If `in_place`
    is True, the matmul result is written back into `rgb` — numpy
    supports that with overlapping input/output and it saves a
    full-canvas allocation on the hot dither path."""
    M = np.array([
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ], dtype=rgb.dtype)
    if in_place:
        np.matmul(rgb, M.T, out=rgb)
        return rgb
    return rgb @ M.T

def _xyz_to_lab(xyz, in_place=False):
    """Convert XYZ to CIE Lab. Avoids `np.where` on the large branch so a
    full-res canvas doesn't allocate both branches at once.

    If `in_place` is True the xyz buffer is reused for the lab output —
    saves a full-canvas allocation and the `np.stack([L, a, b])` transient
    that would otherwise peak at ~45 MB per 1200×1600 float32 canvas.
    Callers that still need the original xyz should pass False.
    """
    dt = xyz.dtype
    ref = np.array([0.95047, 1.00000, 1.08883], dtype=dt)
    if in_place:
        xyz /= ref  # overwrite caller's buffer
    else:
        xyz = xyz / ref  # 1 full alloc we own
    thresh = np.asarray(0.008856, dtype=dt)
    toe_mask = xyz <= thresh
    # Cube-root branch in place: f = xyz ** (1/3). Toe pixels get an
    # incorrect value; fix via mask below.
    np.power(xyz, np.asarray(1.0 / 3.0, dtype=dt), out=xyz)
    if toe_mask.any():
        f_toe_cubed = xyz[toe_mask] ** 3
        xyz[toe_mask] = np.asarray(7.787, dtype=dt) * f_toe_cubed + np.asarray(16.0 / 116.0, dtype=dt)
    del toe_mask

    # Compute L/a/b in place on the same buffer. Order matters because the
    # formulas share f[1]: compute `a` (needs f[0] + f[1]) first, writing
    # into slot 0 — that frees slot 0 but f[1] is still original. Then `b`
    # (needs f[1] + f[2]), writing into slot 2. Finally L (needs f[1]),
    # writing into slot 1. The buffer now holds [a, L, b] — we have to
    # swap slots 0 and 1 to match the [L, a, b] convention callers expect.
    f = xyz  # rename for clarity
    c116 = np.asarray(116.0, dtype=dt)
    c16 = np.asarray(16.0, dtype=dt)
    c500 = np.asarray(500.0, dtype=dt)
    c200 = np.asarray(200.0, dtype=dt)

    # Peel the per-channel slices as (H, W) views so assignments land in place.
    f0 = f[..., 0]
    f1 = f[..., 1]
    f2 = f[..., 2]

    # b into slot 2: 200 * (f1 - f2)  — uses original f1 and f2
    np.subtract(f1, f2, out=f2)
    f2 *= c200
    # a into slot 0: 500 * (f0 - f1)  — uses original f0 and f1
    np.subtract(f0, f1, out=f0)
    f0 *= c500
    # L into slot 1: 116 * f1 - 16  — uses original f1
    f1 *= c116
    f1 -= c16

    # Buffer is [a, L, b]; swap slots 0 and 1 to produce [L, a, b]. The
    # view-based swap doesn't allocate a new canvas, just copies a single
    # channel's worth (~7.7 MB for the tmp).
    tmp = f0.copy()
    f0[...] = f1
    f1[...] = tmp
    return f

def _rgb_to_lab(rgb):
    """Convert sRGB [0-255] to CIE Lab. Clamps input to valid range.
    Used at module load against the 6-entry palette — intentionally
    float64 for maximum precision in the one-time LUT build."""
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

def _lab_to_srgb_f32(lab):
    """Lab → sRGB [0, 255] as float32. Built to minimise peak allocation
    on a 1200×1600×3 canvas: the Lab buffer is overwritten in place and
    converted to a linear-RGB buffer via a single matmul (not three
    scalar expansions which would hold 6 channel buffers live)."""
    ref = np.array([0.95047, 1.00000, 1.08883], dtype=np.float32)
    eps = np.float32(0.008856)
    kappa = np.float32(903.3)

    # Build xyz in-place on top of the lab buffer. Lab channels are
    # read-only views but we're going to overwrite each in turn.
    L = lab[..., 0]
    a = lab[..., 1]
    b = lab[..., 2]

    # Precompute the f values (lab_to_xyz inversion) into small intermediates.
    fy = (L + 16.0) / 116.0
    fx = a / 500.0 + fy
    fz = fy - b / 200.0
    del a, b

    # Overwrite lab slices with xyz values. `L` here aliases `lab[...,0]`.
    # y-channel uses L directly (standard CIE):
    y_new = ((L + 16.0) / 116.0) ** 3
    toe_y = L <= kappa * eps
    if toe_y.any():
        y_new[toe_y] = L[toe_y] / kappa
    y_new *= ref[1]
    lab[..., 1] = y_new  # park y in slot 1
    del y_new, toe_y, L, fy

    # x from fx
    x_new = fx ** 3
    toe_x = x_new <= eps
    if toe_x.any():
        x_new[toe_x] = (np.float32(116.0) * fx[toe_x] - np.float32(16.0)) / kappa
    x_new *= ref[0]
    lab[..., 0] = x_new
    del x_new, toe_x, fx

    # z from fz
    z_new = fz ** 3
    toe_z = z_new <= eps
    if toe_z.any():
        z_new[toe_z] = (np.float32(116.0) * fz[toe_z] - np.float32(16.0)) / kappa
    z_new *= ref[2]
    lab[..., 2] = z_new
    del z_new, toe_z, fz

    # lab now holds xyz. In-place matmul to linear RGB — saves a 23 MB
    # allocation on a 1200×1600×3 float32 canvas.
    M_inv = np.array([
        [ 3.2404542, -1.5371385, -0.4985314],
        [-0.9692660,  1.8760108,  0.0415560],
        [ 0.0556434, -0.2040259,  1.0572252],
    ], dtype=np.float32)
    np.matmul(lab, M_inv.T, out=lab)
    linear = lab
    np.clip(linear, 0, 1, out=linear)

    # sRGB gamma, in-place on linear buffer with masked toe fixup.
    # Save toe-region originals (small, usually <1% of pixels) before the
    # in-place shoulder chain overwrites them.
    toe_mask = linear <= np.asarray(0.0031308, dtype=np.float32)
    toe_saved = linear[toe_mask].copy() if toe_mask.any() else None
    # Shoulder: 1.055 * linear**(1/2.4) - 0.055 (in place)
    np.power(linear, np.asarray(1.0 / 2.4, dtype=np.float32), out=linear)
    linear *= np.float32(1.055)
    linear -= np.float32(0.055)
    # Toe overwrite: linear * 12.92
    if toe_saved is not None:
        linear[toe_mask] = toe_saved * np.float32(12.92)
    del toe_mask, toe_saved

    linear *= 255.0
    np.clip(linear, 0, 255, out=linear)
    return linear


def _rgb_f32_to_lab(rgb_f32):
    """sRGB [0,255] float32 → Lab float32. Same math as _rgb_to_lab but
    kept in float32 throughout to halve peak footprint on full-res
    canvases. Reuses the linear-RGB buffer as the XYZ buffer via
    in-place matmul (numpy handles overlapping input/output for matmul),
    saving one full-canvas allocation."""
    linear = _srgb_to_linear(rgb_f32)
    xyz = _linear_to_xyz(linear, in_place=True)
    del linear
    lab = _xyz_to_lab(xyz, in_place=True)  # xyz buffer is reused as lab
    del xyz
    return lab.astype(np.float32, copy=False)


def _compress_dynamic_range(img_array, scale_chroma=False, adaptive_vivid=False,
                             vivid_low=5.0, vivid_high=15.0):
    """Compress image luminance from [0,100] L* into the display's actual L* range.

    scale_chroma: uniformly scale a*/b* by the same L_ratio (old "vividness" mode).
        Prevents saturated mid-tones from drifting out of the palette gamut after
        L compression, but also mutes saturated features.

    adaptive_vivid: chroma-gated scaling. Near-neutral pixels (chroma<vivid_low)
        get full L_ratio compression (keeps tiny warm tints from cascading into
        phantom red/yellow speckle in white regions). Saturated pixels
        (chroma>vivid_high) keep full chroma (preserves tongues, red logos).
        Smooth ramp between. This is the "best of both" behavior.
    """
    rgb = np.asarray(img_array, dtype=np.float32)
    lab = _rgb_f32_to_lab(rgb)
    del rgb
    L_ratio = np.float32((_DISPLAY_WHITE_L - _DISPLAY_BLACK_L) / 100.0)
    lab[..., 0] = np.float32(_DISPLAY_BLACK_L) + lab[..., 0] * L_ratio
    if adaptive_vivid:
        chroma = np.sqrt(lab[..., 1] ** 2 + lab[..., 2] ** 2)
        t = np.clip((chroma - np.float32(vivid_low)) / np.float32(vivid_high - vivid_low), 0.0, 1.0)
        del chroma
        c_factor = L_ratio + (np.float32(1.0) - L_ratio) * t
        del t
        lab[..., 1] *= c_factor
        lab[..., 2] *= c_factor
        del c_factor
    elif scale_chroma:
        lab[..., 1] *= L_ratio
        lab[..., 2] *= L_ratio
    return _lab_to_srgb_f32(lab)


def _adaptive_saturate(img_array, max_enhance=1.25, low_thresh=5.0, high_thresh=15.0):
    """Chroma-gated saturation boost in Lab space.

    PIL's ImageEnhance.Color scales ALL chroma uniformly, which amplifies tiny
    warm tints in near-white regions (like umbrellas, cardigans) enough that
    error diffusion cascades them into visible phantom red/yellow speckle.

    This version leaves near-neutral pixels (chroma<low_thresh) untouched and
    only boosts pixels that already have meaningful chroma. Result: tongues
    and red logos get the full enhance; white umbrellas stay white.
    """
    rgb = np.asarray(img_array, dtype=np.float32)
    lab = _rgb_f32_to_lab(rgb)
    del rgb
    chroma = np.sqrt(lab[..., 1] ** 2 + lab[..., 2] ** 2)
    t = np.clip((chroma - np.float32(low_thresh)) / np.float32(high_thresh - low_thresh), 0.0, 1.0)
    del chroma
    factor = np.float32(1.0) + np.float32(max_enhance - 1.0) * t
    del t
    lab[..., 1] *= factor
    lab[..., 2] *= factor
    del factor
    return _lab_to_srgb_f32(lab)


# ── Disk cache ──────────────────────────────────────────────────────

def _hash_file(img_path):
    h = hashlib.sha1()
    with open(img_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

# Bump whenever algorithm implementations change in a way that affects output.
# Old cached renders with a different version are ignored and re-rendered.
_CACHE_VERSION = "v3"

def _cache_key(img_path, content_hash):
    orientation = _config.get("orientation", "landscape")
    dither_hash = _dither_config_hash(_config["dither"])
    serp_tag = "s1" if _config.get("dither_serpentine", False) else "s0"
    return f"{img_path.stem}_{content_hash[:12]}_{orientation[0]}_{dither_hash}_{serp_tag}_{_CACHE_VERSION}"

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

def _safe_lookup_name(filename):
    """Validate a filename from a URL path parameter for a lookup endpoint
    (DELETE / retry / thumbnail / original-fetch).

    Unlike upload — where we want to RENAME unsafe characters via
    werkzeug.secure_filename so the on-disk name is sanitised — lookup
    endpoints must match the filename EXACTLY against whatever is on disk.
    Files dropped via Samba / SCP / SSH keep spaces, accents, parentheses,
    etc., which secure_filename would mutate (spaces → underscores), causing
    a 404 even though we just listed the file in /api/status.

    Returns the filename unchanged if safe, or None if it could escape
    the upload dir (path separators, null byte, '.' / '..').
    """
    if not filename or filename in (".", ".."):
        return None
    if any(c in filename for c in "\x00/\\"):
        return None
    # Defense in depth: even if the routing converter let something weird
    # through, Path(name).name strips any trailing dir components.
    if Path(filename).name != filename:
        return None
    return filename


def _failed_marker(img_path):
    return img_path.with_name(img_path.name + ".failed")


def _processing_marker(img_path):
    return img_path.with_name(img_path.name + ".processing")


def _promote_processing_markers_to_failed():
    """At startup: any leftover .processing markers mean the last run crashed
    mid-conversion. Rename each to .failed so _list_images excludes them and
    the service stops restart-looping on the same image."""
    upload_dir = _get_upload_dir()
    if not upload_dir.exists():
        return
    for f in upload_dir.iterdir():
        if f.is_file() and f.name.endswith(".processing"):
            img_name = f.name[:-len(".processing")]
            failed = upload_dir / (img_name + ".failed")
            try:
                f.replace(failed)
                print(f"  WARNING: {img_name} failed on a previous run "
                      f"(crash or OOM mid-convert). Marked as .failed; "
                      f"use the Web UI's 'Retry' button or delete the .failed "
                      f"marker to retry.")
            except OSError as e:
                print(f"  Could not promote {f.name} -> {failed.name}: {e}")


def _scan_upload_dir():
    """Single pass over the upload dir, returning (image_files, failed_names).

    image_files: list of Path objects for live image files (extension matches
        IMAGE_EXTENSIONS) that don't have a .failed sibling.
    failed_names: sorted list of image filenames that have a .failed sibling
        AND a corresponding live image file. Markers without an underlying
        image are skipped so they don't render as un-retryable ghost rows.

    One pass + a set lookup beats N stat() calls per listing — material on a
    Pi Zero W with SD-card storage and 100+ images.
    """
    upload_dir = _get_upload_dir()
    if not upload_dir.exists():
        return [], []
    images = []
    failed_markers = set()
    image_names = set()
    for f in upload_dir.iterdir():
        if not f.is_file():
            continue
        if f.name.endswith(".failed"):
            failed_markers.add(f.name[:-len(".failed")])
        elif f.suffix.lower() in IMAGE_EXTENSIONS:
            images.append(f)
            image_names.add(f.name)
    # Live images = image files without a .failed marker.
    live = [f for f in images if f.name not in failed_markers]
    # Failed = .failed markers whose underlying image still exists.
    failed = sorted(name for name in failed_markers if name in image_names)
    return live, failed


def _list_failed():
    """Return the names of images quarantined after a prior crash that still
    have an underlying image file to retry."""
    return _scan_upload_dir()[1]


def _list_images():
    """List image files in the upload dir, excluding anything quarantined
    after a previous crashed conversion. A .failed sibling next to an image
    (e.g. photo.jpg.failed) means the dither crashed before; the user has
    to hit Retry in the UI (or delete the .failed marker) to try again."""
    return _scan_upload_dir()[0]


def _prepare_canvas(img, dither_cfg, portrait, canvas_w, canvas_h):
    """Composite img onto a canvas of the given size, then run the pre-dither
    tonal/colour stages (autocontrast, gamma, brightness, contrast, sharpness,
    saturation) as driven by dither_cfg.

    canvas_w/canvas_h are the natural post-rotation buffer dims for production
    (1200x1600) or a smaller multiple (e.g. 600x800) when rendering a preview.

    Returns (canvas, padding_mask). padding_mask is True on padding pixels,
    which get forced to pure white after dithering so the enhancement chain
    can't turn them into a dotted off-white pattern.
    """
    from PIL import ImageEnhance, ImageOps

    # _convert_image passes canvas_w/canvas_h pre-rotation for landscape
    # (wide canvas, then rotated -90°) and post-rotation for portrait.
    composite_w = canvas_h if not portrait else canvas_w
    composite_h = canvas_w if not portrait else canvas_h

    w, h = img.size
    scale = min(composite_w / w, composite_h / h)
    new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
    img_resized = img.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGB", (composite_w, composite_h), (255, 255, 255))
    x_off = (composite_w - new_w) // 2
    y_off = (composite_h - new_h) // 2
    canvas.paste(img_resized, (x_off, y_off))

    mask = np.ones((composite_h, composite_w), dtype=bool)
    mask[y_off:y_off + new_h, x_off:x_off + new_w] = False

    ac = dither_cfg.get("autocontrast", {})
    if ac.get("enabled", True):
        canvas = ImageOps.autocontrast(canvas, cutoff=float(ac.get("cutoff", 0.5)))

    g = dither_cfg.get("gamma", {})
    if g.get("enabled", True):
        gamma_val = float(g.get("value", 0.85))
        gamma_lut = [int(((i / 255.0) ** gamma_val) * 255) for i in range(256)] * 3
        canvas = canvas.point(gamma_lut)

    canvas = ImageEnhance.Brightness(canvas).enhance(float(dither_cfg.get("brightness", 1.0)))
    canvas = ImageEnhance.Contrast(canvas).enhance(float(dither_cfg.get("contrast", 1.1)))
    canvas = ImageEnhance.Sharpness(canvas).enhance(float(dither_cfg.get("sharpness", 1.3)))

    sat = dither_cfg.get("saturation", {})
    sat_mode = sat.get("mode", "adaptive")
    sat_value = float(sat.get("value", 1.25))
    if sat_mode == "global":
        canvas = ImageEnhance.Color(canvas).enhance(sat_value)
    # sat_mode == "adaptive" is deferred to _dither_prepared_canvas so
    # callers can drop their source-image reference before the Lab-space
    # transforms allocate. Running it here holds source+canvas+lab
    # simultaneously and peaks ~50 MB above the target ceiling on large
    # JPEGs. sat_mode == "off" simply skips saturation entirely.

    if not portrait:
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


def _floyd_steinberg_dither(canvas, lut=None, lut_scale=None, serpentine=False):
    """Floyd–Steinberg error diffusion. Full error distributed to 4 neighbors
    (7/16 right, 3/16 down-left, 5/16 down, 1/16 down-right).

    serpentine: on even scanlines (y=0,2,4,...) process right-to-left and mirror
    the kernel so diffusion still targets unvisited pixels (reduces streaking).
    """
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


def _atkinson_dither(canvas, lut=None, lut_scale=None, serpentine=False):
    """Atkinson dither. Distributes only 6/8 of the quantization error, 1/8 each
    into 6 neighbors: (+1,0), (+2,0), (-1,+1), (0,+1), (+1,+1), (0,+2). Softer
    gradients, less hue-shift cascade, slightly lower max contrast than FS.

    serpentine: alternate row direction and negate horizontal offsets on even
    scanlines (see _floyd_steinberg_dither).
    """
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


# Memoized hue-aware LUTs — keyed by (hue_cutoff_deg, neutral_chroma). Building
# one is expensive (32³ pixels × 6 palette entries + hue math), so users who
# tweak a knob shouldn't pay that cost on every render.
_hue_aware_lut_cache = {}
_hue_aware_lut_lock = threading.Lock()

# Stucki error diffusion — weights sum to 42/42 (full error).
_STUCKI_OFFSETS = (
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


def _stucki_dither(canvas, lut=None, lut_scale=None, serpentine=False):
    """Stucki error diffusion — larger neighborhood than Floyd–Steinberg; tends
    toward sharper detail and a slightly different noise grain.

    serpentine: alternate row direction and negate horizontal offsets on even
    scanlines (same convention as Atkinson).
    """
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


class _AlgoConfig:
    """Bundle of per-algorithm pipeline parameters."""
    __slots__ = ("dither_fn", "lut", "color_enhance", "scale_chroma",
                 "adaptive_saturate", "adaptive_vivid")
    def __init__(self, dither_fn, lut, color_enhance, scale_chroma,
                 adaptive_saturate, adaptive_vivid):
        self.dither_fn = dither_fn
        self.lut = lut
        self.color_enhance = color_enhance
        self.scale_chroma = scale_chroma
        self.adaptive_saturate = adaptive_saturate
        self.adaptive_vivid = adaptive_vivid


def _get_lut(dither_cfg):
    """Pick the RGB→palette-index LUT for this pipeline config. Memoizes
    hue-aware LUTs by (cutoff, neutral_chroma) so repeated tweaks are cheap."""
    lut_cfg = dither_cfg.get("palette_lut", {})
    mode = lut_cfg.get("mode", "hue_aware")
    if mode == "euclidean":
        return _RGB_LUT_EUCLID
    cutoff = float(lut_cfg.get("hue_cutoff_deg", 95.0))
    neutral = float(lut_cfg.get("neutral_chroma", 8.0))
    if cutoff == 95.0 and neutral == 8.0:
        return _RGB_LUT_HUE_AWARE
    key = (round(cutoff, 3), round(neutral, 3))
    with _hue_aware_lut_lock:
        lut = _hue_aware_lut_cache.get(key)
        if lut is None:
            lut, _ = _build_rgb_lut_hue_aware(hue_cutoff_deg=cutoff, neutral_chroma=neutral)
            _hue_aware_lut_cache[key] = lut
        return lut


def _get_kernel_fn(dither_cfg):
    kernel = dither_cfg.get("kernel", "atkinson")
    if kernel == "floyd_steinberg":
        return _floyd_steinberg_dither
    elif kernel == "stucki":
        return _stucki_dither
    return _atkinson_dither


def _maybe_apply_bw_fallback(dither_cfg, img):
    """If bw_fallback is enabled and the image is near-grayscale, return a
    derived config that disables adaptive saturation/vividness (they amplify
    tiny chroma deviations in B&W images into a visible pink cast). Otherwise
    returns dither_cfg unchanged.

    Conservative override: flat Color(1.05) saturation, no DRC chroma
    compression. Palette LUT, kernel, and tonal stages stay as configured.
    """
    fb = dither_cfg.get("bw_fallback", {})
    if not fb.get("enabled"):
        return dither_cfg, False
    threshold = float(fb.get("chroma_threshold", 8.0))
    percentile = int(fb.get("percentile", 95))
    thumb = img.copy()
    thumb.thumbnail((200, 200), Image.LANCZOS)
    arr = np.asarray(thumb.convert("RGB"), dtype=np.float64)
    lab = _xyz_to_lab(_linear_to_xyz(_srgb_to_linear(arr)))
    chroma = np.sqrt(lab[..., 1] ** 2 + lab[..., 2] ** 2)
    if float(np.percentile(chroma, percentile)) >= threshold:
        return dither_cfg, False
    override = copy.deepcopy(dither_cfg)
    override["saturation"] = {"mode": "global", "value": 1.05,
                              "low": 5.0, "high": 15.0}
    override.setdefault("drc", {})["chroma_mode"] = "off"
    return override, True


def _apply_lab_stage(canvas, sat_cfg, drc_cfg):
    """Fused adaptive-saturation + DRC in a single Lab round-trip.

    The two stages operate on the same lab buffer so we do one
    rgb→lab conversion and one lab→rgb conversion for both together,
    instead of two round-trips. On a 1200×1600 float32 canvas each
    round-trip peaks at ~50 MB of intermediate allocations; fusing
    cuts peak by that full second trip.

    Execution order matches the old sequential path: adaptive saturation
    runs first (operates on a* and b*), then DRC L* remap + optional
    chroma compression. Global/off saturation modes were already applied
    during canvas prep; this helper skips them.

    Returns a new PIL Image with the transformed pixels, or the input
    canvas if no Lab-space work is needed.
    """
    sat_mode = sat_cfg.get("mode")
    drc_enabled = drc_cfg.get("enabled", True)
    sat_active = sat_mode == "adaptive"

    if not (sat_active or drc_enabled):
        return canvas  # nothing to do in Lab space

    rgb = np.array(canvas, dtype=np.float32)
    lab = _rgb_f32_to_lab(rgb)
    del rgb

    # (1) adaptive saturation on a*, b*
    if sat_active:
        max_enhance = np.float32(sat_cfg.get("value", 1.25))
        low = np.float32(sat_cfg.get("low", 5.0))
        high = np.float32(sat_cfg.get("high", 15.0))
        chroma = np.sqrt(lab[..., 1] ** 2 + lab[..., 2] ** 2)
        t = np.clip((chroma - low) / (high - low), 0.0, 1.0)
        del chroma
        factor = np.float32(1.0) + (max_enhance - np.float32(1.0)) * t
        del t
        lab[..., 1] *= factor
        lab[..., 2] *= factor
        del factor

    # (2) DRC L* remap and optional chroma handling
    if drc_enabled:
        L_ratio = np.float32((_DISPLAY_WHITE_L - _DISPLAY_BLACK_L) / 100.0)
        lab[..., 0] = np.float32(_DISPLAY_BLACK_L) + lab[..., 0] * L_ratio
        chroma_mode = drc_cfg.get("chroma_mode", "adaptive_vivid")
        if chroma_mode == "adaptive_vivid":
            vivid_low = np.float32(drc_cfg.get("vivid_low", 5.0))
            vivid_high = np.float32(drc_cfg.get("vivid_high", 15.0))
            chroma = np.sqrt(lab[..., 1] ** 2 + lab[..., 2] ** 2)
            t = np.clip((chroma - vivid_low) / (vivid_high - vivid_low), 0.0, 1.0)
            del chroma
            c_factor = L_ratio + (np.float32(1.0) - L_ratio) * t
            del t
            lab[..., 1] *= c_factor
            lab[..., 2] *= c_factor
            del c_factor
        elif chroma_mode == "flat":
            lab[..., 1] *= L_ratio
            lab[..., 2] *= L_ratio

    out_arr = _lab_to_srgb_f32(lab)
    del lab
    result = Image.fromarray(out_arr.astype(np.uint8))
    del out_arr
    return result


def _dither_prepared_canvas(canvas, padding_mask, dither_cfg, serpentine=False):
    """Run the Lab-stage pipeline (fused saturation+DRC) then the dither
    kernel on an already-prepared canvas. Split out from
    _run_dither_pipeline so callers can drop their source-image
    reference before the Lab-space transforms allocate — on large JPEGs
    that saves 20–50 MB of peak footprint.

    serpentine: pass True to enable boustrophedon scan (alternate row
    direction) in the dither kernel.
    """
    canvas = _apply_lab_stage(canvas, dither_cfg.get("saturation", {}),
                               dither_cfg.get("drc", {}))

    kernel_fn = _get_kernel_fn(dither_cfg)
    lut = _get_lut(dither_cfg)
    result_idx = kernel_fn(canvas, lut, _LUT_SCALE, serpentine=serpentine)
    del canvas
    result_idx[padding_mask] = 1  # force padding to pure white
    return result_idx


def _run_dither_pipeline(img, dither_cfg, orientation, canvas_w, canvas_h):
    """Convenience wrapper: prepare_canvas → _dither_prepared_canvas. Used
    by the preview endpoint where the source image is always small. The
    full-resolution _convert_image path splits the two calls manually so
    it can drop its source-image reference before DRC peaks."""
    portrait = (orientation == "portrait")
    canvas, padding_mask = _prepare_canvas(img, dither_cfg, portrait,
                                           canvas_w=FULL_W, canvas_h=PANEL_H)
    del img  # source no longer needed; release its RGB buffer
    serpentine = bool(_config.get("dither_serpentine", False))
    result_idx = _dither_prepared_canvas(canvas, padding_mask, dither_cfg, serpentine=serpentine)
    return result_idx, padding_mask


def _render_preview_png(result_idx, orientation):
    """Convert a palette-index buffer into a human-viewable PNG (bytes)."""
    preview_rgb = PALETTE_PREVIEW_RGB[result_idx]
    if orientation == "landscape":
        preview_img = Image.fromarray(preview_rgb).rotate(90, expand=True)
    else:
        preview_img = Image.fromarray(preview_rgb)
    buf = BytesIO()
    preview_img.save(buf, format="PNG")
    return buf.getvalue()


def _convert_image(img_path):
    """Full-resolution render to panel bytes + preview PNG. Uses the saved
    _config['dither'] pipeline. Applies B&W fallback when the input image
    qualifies."""
    print(f"Converting: {img_path.name}")
    t0 = time.time()
    from PIL import ImageOps
    img = Image.open(img_path)
    # Early downsample: a 32-megapixel phone JPEG would otherwise sit in
    # RAM as 96 MB of RGB alongside the DRC float arrays and OOM the Pi.
    # JPEG's draft() asks the decoder to emit a smaller image by skipping
    # DCT coefficients — free, no full-size decode ever happens. Other
    # formats fall back to thumbnail() after load, which still caps the
    # peak by releasing the full-size copy. The ceiling is 3× the panel
    # long-edge so LANCZOS resampling to canvas still has ample detail.
    max_side = 3 * max(FULL_W, PANEL_H)
    try:
        img.draft(None, (max_side, max_side))
    except Exception:
        pass  # non-JPEG or decoder without draft — tolerable
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    print(f"  {img_path.name}: {img.size[0]}x{img.size[1]}")

    dither_cfg = _config["dither"]
    dither_cfg, bw_used = _maybe_apply_bw_fallback(dither_cfg, img)
    if bw_used:
        print(f"  {img_path.name}: B&W detected, applying conservative fallback")

    orientation = _config.get("orientation", "landscape")
    # Full-resolution panel buffer is 1200×1600 (portrait) regardless of
    # orientation — landscape rotates into the same buffer shape.
    # We run _prepare_canvas inline (not via _run_dither_pipeline) so we
    # can free the full-size source image BEFORE the DRC arrays allocate.
    # On a 32 MP source that's the difference between a 450 MB peak and
    # a ~50 MB peak.
    portrait = (orientation == "portrait")
    canvas, padding_mask = _prepare_canvas(img, dither_cfg, portrait,
                                           canvas_w=FULL_W, canvas_h=PANEL_H)
    del img  # source no longer needed; release its RGB buffer
    serpentine = bool(_config.get("dither_serpentine", False))
    result_idx = _dither_prepared_canvas(canvas, padding_mask, dither_cfg, serpentine=serpentine)
    del canvas, padding_mask

    nibbles = PALETTE_NIBBLE[result_idx]
    panel1_nib = nibbles[:, :PANEL_W]
    panel2_nib = nibbles[:, PANEL_W:]
    panel1_bin = (panel1_nib[:, 0::2] << 4) | panel1_nib[:, 1::2]
    panel2_bin = (panel2_nib[:, 0::2] << 4) | panel2_nib[:, 1::2]
    raw_bytes = panel1_bin.astype(np.uint8).tobytes() + panel2_bin.astype(np.uint8).tobytes()
    assert len(raw_bytes) == TOTAL_BYTES, f"Expected {TOTAL_BYTES}, got {len(raw_bytes)}"

    preview_bytes = _render_preview_png(result_idx, orientation)
    elapsed = time.time() - t0
    print(f"  {img_path.name}: done in {elapsed:.1f}s")
    return raw_bytes, preview_bytes


# Preview button downsamples the source to this long-edge size before running
# the full pipeline. ~1 s render (vs ~10 s full-res) — trades dither-pattern
# fidelity for interactive feedback while tweaking knobs.
_PREVIEW_LONG_EDGE = 800


def _render_dither_preview(img_path, dither_cfg):
    """Small-canvas render used by /api/dither/preview. Returns PNG bytes.
    Does NOT touch _config, _pool, or the disk cache — callers can safely
    pass the unsaved form state."""
    from PIL import ImageOps
    img = Image.open(img_path)
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    img.thumbnail((_PREVIEW_LONG_EDGE, _PREVIEW_LONG_EDGE), Image.LANCZOS)

    dither_cfg = _normalize_dither_config(dither_cfg)
    dither_cfg, _ = _maybe_apply_bw_fallback(dither_cfg, img)

    orientation = _config.get("orientation", "landscape")
    # Half-resolution canvas preserves the same aspect ratio as full render.
    canvas_w = FULL_W // 2
    canvas_h = PANEL_H // 2
    result_idx, _ = _run_dither_pipeline(img, dither_cfg, orientation,
                                         canvas_w=canvas_w, canvas_h=canvas_h)
    return _render_preview_png(result_idx, orientation)


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
            # Drop a .processing marker BEFORE attempting conversion. If
            # the process gets OOM-killed mid-dither this marker survives
            # the crash; startup then promotes it to .failed so we don't
            # restart-loop on the same image.
            marker = _processing_marker(img_path)
            try:
                marker.touch()
            except OSError:
                pass
            try:
                raw_bytes, preview_bytes = _convert_image(img_path)
                _save_to_cache(cache_dir, img_path, content_hash, raw_bytes, preview_bytes)
                try:
                    marker.unlink(missing_ok=True)
                except OSError:
                    pass
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
            # Keep cached renders for both serpentine states so toggling
            # the serpentine checkbox is instant on a re-visit.
            for serp in (False, True):
                old_serp = _config.get("dither_serpentine", False)
                _config["dither_serpentine"] = serp
                valid_cache_keys.add(_cache_key(Path(key), entry["hash"]))
                _config["dither_serpentine"] = old_serp
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
            if _converting_count > 0:
                status = 503
                msg = "Converting images, try again shortly"
                label = "Converting"
            else:
                status = 404
                msg = "No images in upload directory"
                label = "No images"
            resp = make_response(msg, status)
            resp.headers["X-Sleep-Seconds"] = str(sleep_seconds)
            print(f"  {label}: {screen_name} told to retry in {sleep_seconds}s")
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
    # Time only — the top-right display doesn't need the date.
    now_str = datetime.now().strftime("%H:%M:%S %Z").strip()

    with _lock:
        pool_files = sorted(Path(k).name for k in _pool.keys())
        pool_set = set(pool_files)
        serve_data = _database.get("serve_data", {})

        # Single iterdir() pass — produces both the live image list and the
        # quarantined-failed list. Avoids two stat-storming loops over the
        # upload dir per status poll on a Pi Zero W.
        live_images, failed_files = _scan_upload_dir()
        all_upload = sorted(p.name for p in live_images)
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
            "failed_files": failed_files,
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
        })


@app.route("/hokku/api/config", methods=["GET"])
def api_config_get():
    """Return the full config + preset catalog. Intended to be fetched ONCE
    when the settings form first renders — the /api/status poll deliberately
    omits config so it can't overwrite fields the user is editing."""
    presets = {
        name: {"label": p["label"], "description": p["description"],
               "dither": p["dither"]}
        for name, p in DITHER_PRESETS.items()
    }
    return jsonify({
        "config": {
            "refresh_image_at_time": _config["refresh_image_at_time"],
            "port": _config["port"],
            "poll_interval_seconds": _config.get("poll_interval_seconds", 10),
            "orientation": _config.get("orientation", "landscape"),
            "debug_fast_refresh": bool(_config.get("debug_fast_refresh", False)),
            "debug_fast_refresh_seconds": DEBUG_FAST_REFRESH_SECONDS,
            "dither": _config["dither"],
            "upload_dir": str(_get_upload_dir()),
            "cache_dir": str(_get_cache_dir()),
        },
        "dither_presets": presets,
        "default_preset": DEFAULT_PRESET,
    })


# Formats browsers can display natively — everything else gets converted to JPEG
_BROWSER_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".avif"}

@app.route("/hokku/api/original/<filename>")
def api_original(filename):
    """Serve the original uploaded image, converting to JPEG if the browser can't display it."""
    safe_name = _safe_lookup_name(filename)
    if not safe_name:
        abort(400)
    img_path = _get_upload_dir() / safe_name
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
            # Hint libjpeg to decode at 1/4 or so — we only need 300x300 out,
            # full decode of a 12 MP source is pure RAM waste on a Pi Zero 2 W.
            try:
                img.draft("RGB", (600, 600))
            except Exception:
                pass
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
    safe_name = _safe_lookup_name(filename)
    if not safe_name:
        abort(400)
    img_path = _get_upload_dir() / safe_name
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
    # Timezone is no longer configurable here — host OS owns it. Silently
    # ignore stale clients still posting it.
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

    dither_changed = False
    if "dither" in data:
        new_dither = _normalize_dither_config(data["dither"])
        if _dither_config_hash(_config["dither"]) != _dither_config_hash(new_dither):
            dither_changed = True
        _config["dither"] = new_dither
        changed = True

    serpentine_changed = False
    if "dither_serpentine" in data:
        val = bool(data["dither_serpentine"])
        if bool(_config.get("dither_serpentine", False)) != val:
            serpentine_changed = True
        _config["dither_serpentine"] = val
        changed = True

    if changed:
        _save_config(_config)

    if orientation_changed:
        # Orientation affects image processing, so clear cache and re-convert
        _clear_cache_files(_get_cache_dir())
        with _lock:
            _pool.clear()
        threading.Thread(target=_sync_pool, daemon=True).start()
    elif dither_changed or serpentine_changed:
        # Dither config or serpentine change: cache key changes so pool entries
        # won't find matching renders. Drop the pool to force _sync_pool to
        # re-run _convert_and_store.
        with _lock:
            _pool.clear()
        threading.Thread(target=_sync_pool, daemon=True).start()

    return jsonify({"status": "ok", "config": {
        "refresh_image_at_time": _config["refresh_image_at_time"],
        "poll_interval_seconds": _config.get("poll_interval_seconds", 10),
        "orientation": _config.get("orientation", "landscape"),
        "debug_fast_refresh": bool(_config.get("debug_fast_refresh", False)),
        "dither": _config["dither"],
        "dither_serpentine": bool(_config.get("dither_serpentine", False)),
    }})


@app.route("/hokku/api/dither/preview", methods=["POST"])
def api_dither_preview():
    """Render a preview PNG using an *unsaved* dither config.

    Body: {"filename": "<image in upload dir>", "dither": {<dither config>}}
    Returns: PNG bytes. Does not touch the pool or disk cache."""
    data = request.get_json(silent=True) or {}
    filename = data.get("filename")
    dither_cfg = data.get("dither")
    if not filename or not isinstance(dither_cfg, dict):
        return jsonify({"error": "Expected JSON {filename, dither}"}), 400

    upload_dir = _get_upload_dir()
    img_path = upload_dir / filename
    if not img_path.exists() or not img_path.is_file():
        abort(404)
    if img_path.suffix.lower() not in IMAGE_EXTENSIONS:
        abort(404)

    try:
        preview_bytes = _render_dither_preview(img_path, dither_cfg)
    except Exception as e:
        print(f"  Dither preview error for {filename}: {e}")
        return jsonify({"error": str(e)}), 500
    return send_file(BytesIO(preview_bytes), mimetype="image/png")


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
    safe_name = _safe_lookup_name(filename)
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

    # Also clean up the crash-quarantine sibling markers so we don't leave
    # orphan .failed / .processing files behind. An orphaned .failed would
    # show up as a ghost row in the UI's failed list with no retryable image
    # behind it; an orphaned .processing would get promoted to .failed at
    # the next startup, again as a ghost.
    for sib in (_failed_marker(img_path), _processing_marker(img_path)):
        try:
            sib.unlink(missing_ok=True)
        except OSError:
            pass
    print(f"  Delete: removed {safe_name}")

    # _sync_pool drops the pool entry and purges the matching cache .bin/.png
    threading.Thread(target=_sync_pool, daemon=True).start()

    return jsonify({"status": "ok", "deleted": safe_name})


@app.route("/hokku/api/image/<filename>/retry", methods=["POST"])
def api_retry_image(filename):
    """Remove the .failed quarantine marker for an image and re-trigger a sync
    so the dither pipeline picks it up again."""
    safe_name = _safe_lookup_name(filename)
    if not safe_name:
        return jsonify({"error": "Invalid filename"}), 400
    img_path = _get_upload_dir() / safe_name
    if not img_path.exists() or not img_path.is_file():
        return jsonify({"error": "Image not found"}), 404
    marker = _failed_marker(img_path)
    if not marker.exists():
        return jsonify({"error": "Image is not in the failed state"}), 400
    try:
        marker.unlink()
    except OSError as e:
        return jsonify({"error": f"Failed to remove marker: {e.strerror or e}"}), 500
    print(f"  Retry: unquarantined {safe_name}")
    threading.Thread(target=_sync_pool, daemon=True).start()
    return jsonify({"status": "ok", "retrying": safe_name})


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
    """Return current server time in the system timezone."""
    now = datetime.now()
    try:
        import time as _time
        tz_name = _time.tzname[_time.daylight and _time.localtime().tm_isdst > 0]
    except Exception:
        tz_name = ""
    return jsonify({
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": tz_name,
    })


# ── Main ───────────────────────────────────────────────────────────

def main():
    global _config, _database

    import argparse
    parser = argparse.ArgumentParser(description="Hokku Spectra 6 image server")
    parser.add_argument("config", help="Path to config.json")
    args = parser.parse_args()

    _config = _load_config(args.config)
    upload_dir = _get_upload_dir()
    cache_dir = _get_cache_dir()

    if not upload_dir.is_dir():
        print(f"Error: upload_dir does not exist: {upload_dir}")
        exit(1)
    if not cache_dir.is_dir():
        print(f"Error: cache_dir does not exist: {cache_dir}")
        exit(1)
    if not os.access(cache_dir, os.W_OK):
        print(f"Error: cache_dir is not writable: {cache_dir}")
        exit(1)
    _database = _load_database(cache_dir)

    port = _config["port"]
    poll = _config.get("poll_interval_seconds", 10)

    print(f"Hokku image server (full resolution: {VISUAL_W}x{VISUAL_H})")
    print(f"  Upload dir: {upload_dir}")
    print(f"  Cache dir:  {cache_dir}")
    print(f"  Timezone:   system ({datetime.now().astimezone().tzinfo})")
    print(f"  Refresh at: {_config['refresh_image_at_time']}")
    print(f"  Poll interval: {poll}s")
    print(f"  Output: {TOTAL_BYTES} bytes per image ({PANEL_BYTES} per panel)")
    print(f"  Endpoints:")
    print(f"    GET /hokku/screen/      — 960K binary (fair rotation) + X-Sleep-Seconds header")
    print(f"    GET /hokku/ui           — Web GUI")

    # Quarantine any image whose .processing marker survived a crash —
    # otherwise the service restart-loops on a bad image forever.
    _promote_processing_markers_to_failed()

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
