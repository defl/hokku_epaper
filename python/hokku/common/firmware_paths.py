"""Shared filesystem locations + version ordering for hokku firmware artifacts.

Both the ESP32-S3 (:mod:`hokku.common.esp32.firmware`) and XR872
(:mod:`hokku.screens.bigme_f7.firmware`) firmware modules search the same two
directories for release artifacts and order versions the same way. Centralising
them here keeps the list — and the numeric version comparison — in one place
instead of copied per screen family.
"""

from __future__ import annotations

from pathlib import Path

from packaging.version import InvalidVersion, Version

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
    """A sort key that orders firmware versions by version semantics, not by string.

    ``"1.2.10"`` must rank above ``"1.2.9"`` — a plain string sort gets this
    backwards and would silently pick the older build, which is exactly the class
    of bug this exists to prevent.

    Ordering is delegated to :class:`packaging.version.Version` (PEP 440) rather
    than hand-rolled, so it also gets the cases a per-component integer compare
    quietly mishandles: differing component counts (``1.3`` vs ``1.3.0``), and
    pre-releases, which must rank *below* their own final release
    (``1.2.10-beta`` < ``1.2.10``) or a beta would be served as if it superseded
    the real thing.

    Anything unparseable (a truncated download, a hand-renamed file, the empty
    string returned when a filename doesn't match the release convention) sorts
    **below every valid version**, so a malformed name can never outrank a real
    release. Unparseable names are ordered among themselves as plain strings,
    which is arbitrary but stable.
    """
    try:
        return (1, Version(version))
    except InvalidVersion:
        return (0, version)
