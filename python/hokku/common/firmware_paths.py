"""Shared filesystem locations + version ordering for hokku firmware artifacts.

Both the ESP32-S3 (:mod:`hokku.common.esp32.firmware`) and XR872
(:mod:`hokku.screens.bigme_f7.firmware`) firmware modules search the same two
directories for release artifacts and order versions the same way. Centralising
them here keeps the list — and the numeric version comparison — in one place
instead of copied per screen family.
"""

from __future__ import annotations

from pathlib import Path

# python/hokku/common/firmware_paths.py -> repo root is parents[3]
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: Directories that hold bundled release artifacts, in search order:
#:   1. the repo's shared build output ``firmware/release/`` (dev tree), then
#:   2. ``/usr/share/hokku-server/firmware/`` (installed via the Debian package).
#: The downloaded-firmware directory (``config.firmware_dir``, writable, populated
#: on demand from GitHub) is layered on top of these by the webserver's
#: FirmwareStore — it is intentionally NOT listed here, so the plain bundled
#: lookups used by the ``tools/`` CLI stay offline-only.
BUNDLED_FIRMWARE_DIRS: tuple[Path, ...] = (
    _REPO_ROOT / "firmware" / "release",
    Path("/usr/share/hokku-server/firmware"),
)


def version_key(version: str) -> tuple:
    """A sort key that orders firmware versions numerically, not lexicographically.

    ``"1.2.10"`` must rank above ``"1.2.9"`` (a plain string sort gets this wrong
    and would silently pick the older build). Each dotted component sorts as an
    int when numeric; any non-numeric component sorts after all numeric ones in
    that slot so a malformed name can never outrank a real version.
    """
    key: list = []
    for part in version.split("."):
        key.append((0, int(part)) if part.isdigit() else (1, part))
    return tuple(key)
