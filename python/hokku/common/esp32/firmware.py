"""Locate + serve the bundled merged firmware image for an ESP32-S3 hokku screen.

The merged ``hokku-<model>-<version>.bin`` (bootloader + partition table + app)
is searched for in:
  1. the repo's shared ``firmware/release/`` directory (dev tree), then
  2. ``/usr/share/hokku-server/firmware/`` (installed via the Debian package).

The release filename carries the version (see firmware/*/ci-build.sh). Everything
here is ESP-IDF-generic; the screen's :class:`Esp32Spec` supplies the artifact
name (via ``model_id``) and the app offset.
"""

from __future__ import annotations

import struct
from pathlib import Path

from hokku.common.esp32.spec import Esp32Spec
from hokku.common.firmware_paths import BUNDLED_FIRMWARE_DIRS, version_key

# Bundled-artifact search dirs (shared with every screen family). Kept as
# module-level names so tests can monkeypatch them per case.
_DEV_FIRMWARE_DIR, _INSTALLED_FIRMWARE_DIR = BUNDLED_FIRMWARE_DIRS

# Version ordering lives in ``firmware_paths``; alias it under the historical
# private name used throughout this module.
_version_key = version_key


def resolve_firmware_dir(spec: Esp32Spec) -> Path | None:
    """Return the first directory that holds a merged firmware bin, or None."""
    for d in (_DEV_FIRMWARE_DIR, _INSTALLED_FIRMWARE_DIR):
        if merged_firmware_file(spec, d) is not None:
            return d
    return None


def merged_firmware_file(spec: Esp32Spec, directory: Path | None = None) -> Path | None:
    """Return the merged ``hokku-<model>-<version>.bin`` in *directory*, or None.

    With no argument, searches the resolved firmware dir. Picks the highest
    version (compared as a version, so 1.2.10 > 1.2.9) when several are present —
    ``firmware/release/`` is not pruned between builds, so multiple versions of a
    model can coexist there.
    """
    if directory is None:
        directory = resolve_firmware_dir(spec)
    if directory is None or not directory.exists():
        return None

    def version_of(path: Path) -> str:
        m = spec.merged_re.match(path.name)
        return m.group(1) if m else ""

    matches = sorted(directory.glob(spec.merged_glob), key=lambda p: _version_key(version_of(p)))
    return matches[-1] if matches else None


def list_firmware_files(spec: Esp32Spec, directory: Path) -> list[tuple[str, Path]]:
    """Return every ``(version, path)`` merged image for *spec* in *directory*.

    Unlike :func:`merged_firmware_file` this does not collapse to the highest
    version — the caller (FirmwareStore) needs the full set so it can present
    every downloaded/bundled version for selection."""
    if not directory.exists():
        return []
    out: list[tuple[str, Path]] = []
    for p in directory.glob(spec.merged_glob):
        m = spec.merged_re.match(p.name)
        if m:
            out.append((m.group(1), p))
    return out


def release_app_header(spec: Esp32Spec, directory: Path | None = None) -> bytes | None:
    """Return the first 256 bytes of the app section (at ``spec.app_offset``) of
    the bundled merged firmware image — used to read the release version string
    and compare against what is on a connected device."""
    merged = merged_firmware_file(spec, directory)
    if not merged:
        return None
    with open(merged, "rb") as f:
        f.seek(spec.app_offset)
        return f.read(256)


def bundled_firmware_version(spec: Esp32Spec, directory: Path | None = None) -> str | None:
    """Return the bundled firmware's version, parsed from the release filename
    (``hokku-<model>-<version>.bin``), or None if not present. The build embeds
    the same version in both the filename and the app descriptor."""
    merged = merged_firmware_file(spec, directory)
    if not merged:
        return None
    m = spec.merged_re.match(merged.name)
    return m.group(1) if m else None


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
    (writing the full tail overflows the device's OTA partition).
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


def app_image_from_file(spec: Esp32Spec, path: Path) -> bytes | None:
    """Return the OTA-flashable **app-only** image sliced from the merged file at
    *path*.

    Parses the ESP image header at ``spec.app_offset`` to determine the exact app
    byte length and returns only those bytes — not the 0xFF padding or
    ``ota_data_initial.bin`` that follow the app in the merged file. Returns None
    if the image cannot be parsed (also used to validate a freshly-downloaded
    artifact before it is admitted to the firmware library)."""
    with open(path, "rb") as f:
        f.seek(spec.app_offset)
        app_data = f.read()
    size = _esp_app_image_size(app_data)
    if size is None:
        return None
    return app_data[:size]


def release_app_image(spec: Esp32Spec, directory: Path | None = None) -> bytes | None:
    """Return the OTA-flashable **app-only** image from the bundled merged firmware.

    Returns None if no bundled firmware is present or the image cannot be parsed.
    See :func:`app_image_from_file` for the header-slicing details."""
    merged = merged_firmware_file(spec, directory)
    if not merged:
        return None
    return app_image_from_file(spec, merged)
