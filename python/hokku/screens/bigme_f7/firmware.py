"""Locate the bundled Bigme F7 (XR872AT) firmware image.

The flashable blob is a single AWIH ``xr_system.img``; the release artifact is
``hokku-bigme_f7-<version>.img`` (the filename carries the version — the build
names it from ``firmware/bigme_f7/main.c`` ``FIRMWARE_VERSION``, see
``firmware/bigme_f7/ci-build.sh``). It is served **verbatim**: the device's OTA
client discards the leading bootloader itself and writes the rest into its inactive
A/B slot, so there is no header parsing or slicing here.

Searched for in:
  1. the repo's shared ``firmware/release/`` directory (dev tree), then
  2. ``/usr/share/hokku-server/firmware/`` (installed via the Debian package).
"""

from __future__ import annotations

import re
from pathlib import Path

# python/hokku/screens/bigme_f7/firmware.py -> repo root is parents[4]
_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEV_FIRMWARE_DIR = _REPO_ROOT / "firmware" / "release"
_INSTALLED_FIRMWARE_DIR = Path("/usr/share/hokku-server/firmware")

_IMG_GLOB = "hokku-bigme_f7-*.img"
_IMG_RE = re.compile(r"^hokku-bigme_f7-(.+)\.img$")


def firmware_image_file() -> Path | None:
    """Return the bundled ``hokku-bigme_f7-<version>.img`` path, or None.

    Picks the highest version by filename sort when several are present."""
    for d in (_DEV_FIRMWARE_DIR, _INSTALLED_FIRMWARE_DIR):
        if d.exists():
            matches = sorted(d.glob(_IMG_GLOB))
            if matches:
                return matches[-1]
    return None


def bundled_firmware_version() -> str | None:
    """Version of the bundled Bigme F7 image, parsed from the release filename, or
    None if no image is present."""
    img = firmware_image_file()
    if img is None:
        return None
    m = _IMG_RE.match(img.name)
    return m.group(1) if m else None


def release_app_image() -> bytes | None:
    """Return the full image bytes to stream for OTA, or None.

    Served verbatim: the device's OTA client skips the leading bootloader and
    writes the remaining app-chain to its inactive slot."""
    img = firmware_image_file()
    if img is None:
        return None
    return img.read_bytes()
