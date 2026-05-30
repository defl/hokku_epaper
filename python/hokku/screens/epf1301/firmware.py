"""Locate the bundled merged firmware image for the EPF1301 frame.

The merged ``hokku-firmware_<version>.bin`` (bootloader + partition table + app)
is searched for in:
  1. the repo's ``firmware/release/`` directory (dev tree), then
  2. ``/usr/share/hokku-server/firmware/`` (installed via the Debian package).
"""

from __future__ import annotations

from pathlib import Path

from .constants import APP_OFFSET

# python/hokku/screens/epf1301/firmware.py -> repo root is parents[4]
_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEV_FIRMWARE_DIR = _REPO_ROOT / "firmware" / "release"
_INSTALLED_FIRMWARE_DIR = Path("/usr/share/hokku-server/firmware")


def resolve_firmware_dir() -> Path | None:
    """Return the first directory that holds a merged firmware bin, or None."""
    for d in (_DEV_FIRMWARE_DIR, _INSTALLED_FIRMWARE_DIR):
        if merged_firmware_file(d) is not None:
            return d
    return None


def merged_firmware_file(directory: Path | None = None) -> Path | None:
    """Return the merged ``hokku-firmware_<version>.bin`` in *directory*, or None.

    With no argument, searches the resolved firmware dir. Picks the highest
    version by filename sort when several are present.
    """
    if directory is None:
        directory = resolve_firmware_dir()
    if directory is None or not directory.exists():
        return None
    matches = sorted(directory.glob("hokku-firmware_*.bin"))
    return matches[-1] if matches else None


def release_app_header(directory: Path | None = None) -> bytes | None:
    """Return the first 256 bytes of the app section (at APP_OFFSET) of the
    bundled merged firmware image — used to read the release version string and
    compare against what is on a connected device."""
    merged = merged_firmware_file(directory)
    if not merged:
        return None
    with open(merged, "rb") as f:
        f.seek(APP_OFFSET)
        return f.read(256)
