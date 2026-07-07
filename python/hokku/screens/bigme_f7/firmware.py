"""Locate the bundled Bigme F7 (XR872AT) OTA firmware image.

Unlike the huessen ESP32 firmware (an ESP-IDF app that must be sliced out of a
merged bin), the Bigme image is a single AWIH app-chain — ``xr_system.img`` —
served **verbatim**: the device's OTA client discards the leading bootloader
itself and writes the remaining app-chain into its inactive A/B slot. So there
is no header parsing or slicing here.

Search order:
  1. the repo's ``firmware_bigme_f7/image/xr872/`` directory (dev tree), then
  2. ``/usr/share/hokku-server/firmware/bigme_f7/`` (installed via the package).

The version string has no in-image descriptor to read (AWIH carries none), so it
comes from a ``<img>.version`` sidecar when present (packaged installs), else by
parsing ``FIRMWARE_VERSION`` from ``firmware_bigme_f7/main.c`` in the dev tree.
"""

from __future__ import annotations

import re
from pathlib import Path

# python/hokku/screens/bigme_f7/firmware.py -> repo root is parents[4]
_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEV_IMG = _REPO_ROOT / "firmware_bigme_f7" / "image" / "xr872" / "xr_system.img"
_DEV_MAIN_C = _REPO_ROOT / "firmware_bigme_f7" / "main.c"
_INSTALLED_IMG = Path("/usr/share/hokku-server/firmware/bigme_f7/xr_system.img")


def firmware_image_file() -> Path | None:
    """Return the bundled ``xr_system.img`` path, or None if not present."""
    for p in (_DEV_IMG, _INSTALLED_IMG):
        if p.exists():
            return p
    return None


def _version_from_sidecar(img: Path) -> str | None:
    side = img.with_name(img.name + ".version")
    if side.exists():
        return side.read_text(encoding="utf-8", errors="replace").strip() or None
    return None


def _version_from_main_c() -> str | None:
    if not _DEV_MAIN_C.exists():
        return None
    text = _DEV_MAIN_C.read_text(encoding="utf-8", errors="replace")
    m = re.search(r'#define\s+FIRMWARE_VERSION\s+"([^"]+)"', text)
    return m.group(1) if m else None


def bundled_firmware_version() -> str | None:
    """Version string of the bundled Bigme F7 image, or None if no image exists.

    Prefers a ``<img>.version`` sidecar (packaged installs); falls back to
    parsing ``FIRMWARE_VERSION`` from ``firmware_bigme_f7/main.c`` (dev tree)."""
    img = firmware_image_file()
    if img is None:
        return None
    return _version_from_sidecar(img) or _version_from_main_c()


def release_app_image() -> bytes | None:
    """Return the full ``xr_system.img`` bytes to stream for OTA, or None.

    Served verbatim: the device's OTA client skips the leading bootloader
    (``bl_size`` bytes) and writes the remaining app-chain to its inactive slot."""
    img = firmware_image_file()
    if img is None:
        return None
    return img.read_bytes()
