"""Locate the bundled merged firmware image for the huessen_epf1301 frame.

The merged ``hokku-firmware_<version>.bin`` (bootloader + partition table + app)
is searched for in:
  1. the repo's ``firmware/release/`` directory (dev tree), then
  2. ``/usr/share/hokku-server/firmware/`` (installed via the Debian package).
"""

from __future__ import annotations

import struct
from pathlib import Path

from .constants import APP_OFFSET

# python/hokku/screens/huessen_epf1301/firmware.py -> repo root is parents[4]
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


def bundled_firmware_version(directory: Path | None = None) -> str | None:
    """Return the bundled firmware's version string (from the ESP app descriptor),
    or None if no bundled firmware is present. The version lives at bytes [48:80]
    of the app section header (``esp_app_desc_t.version``)."""
    hdr = release_app_header(directory)
    if not hdr:
        return None
    return hdr[48:80].split(b"\x00")[0].decode("ascii", errors="replace").strip() or None


def _esp_app_image_size(data: bytes) -> int | None:
    """Parse an ESP-IDF app image header to determine the image's exact byte length.

    An ESP image is structured as:
      - 24-byte ``esp_image_header_t`` (``segment_count`` at [1], ``hash_appended`` at [23])
      - ``segment_count`` × (8-byte segment header + ``data_len`` bytes of payload)
      - 1-byte XOR checksum
      - 32-byte SHA256 digest if ``hash_appended == 1``

    Returns None when the data does not look like a valid ESP image.
    This is needed because the merged firmware binary includes 0xFF padding and
    the ``ota_data_initial.bin`` after the app, which must not be served for OTA
    (the OTA partition is only 3 MB; writing the full tail overflows it).
    """
    if len(data) < 24 or data[0] != 0xE9:
        return None
    segment_count = data[1]
    hash_appended = data[23]
    pos = 24  # after esp_image_header_t
    for _ in range(segment_count):
        if pos + 8 > len(data):
            return None
        data_len = struct.unpack_from("<I", data, pos + 4)[0]
        pos += 8 + data_len
    # IDF aligns the checksum byte so that it falls on a 16-byte boundary:
    # pad with (15 - pos % 16) bytes, then 1 checksum byte.
    pad = (15 - pos % 16) % 16
    pos += pad + 1  # padding + XOR checksum byte
    if hash_appended:
        pos += 32  # SHA256 digest
    return pos


def release_app_image(directory: Path | None = None) -> bytes | None:
    """Return the OTA-flashable **app-only** image from the bundled merged firmware.

    Parses the ESP image header at ``APP_OFFSET`` to determine the exact app
    byte length and returns only those bytes — not the 0xFF padding or
    ``ota_data_initial.bin`` that follow the app in the merged file. Returns
    None if no bundled firmware is present or the image cannot be parsed."""
    merged = merged_firmware_file(directory)
    if not merged:
        return None
    with open(merged, "rb") as f:
        f.seek(APP_OFFSET)
        app_data = f.read()
    size = _esp_app_image_size(app_data)
    if size is None:
        return None
    return app_data[:size]
