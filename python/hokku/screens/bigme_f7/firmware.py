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

from hokku.common.firmware_paths import BUNDLED_FIRMWARE_DIRS

# Bundled-artifact search dirs (shared with every screen family). Kept as
# module-level names so tests can monkeypatch them per case.
_DEV_FIRMWARE_DIR, _INSTALLED_FIRMWARE_DIR = BUNDLED_FIRMWARE_DIRS

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


def list_firmware_files(directory: Path) -> list[tuple[str, Path]]:
    """Return every ``(version, path)`` Bigme F7 image in *directory*.

    Unlike :func:`firmware_image_file` this returns the full set (not just the
    highest) so the FirmwareStore can present every version for selection."""
    if not directory.exists():
        return []
    out: list[tuple[str, Path]] = []
    for p in directory.glob(_IMG_GLOB):
        m = _IMG_RE.match(p.name)
        if m:
            out.append((m.group(1), p))
    return out


def app_image_from_file(path: Path) -> bytes:
    """Return the full image bytes to stream for OTA from the file at *path*.

    Served verbatim: the device's OTA client skips the leading bootloader and
    writes the remaining app-chain to its inactive slot — no slicing here."""
    return path.read_bytes()


def release_app_image() -> bytes | None:
    """Return the full bundled image bytes to stream for OTA, or None."""
    img = firmware_image_file()
    if img is None:
        return None
    return app_image_from_file(img)
