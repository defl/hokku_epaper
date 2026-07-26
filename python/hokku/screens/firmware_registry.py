"""Map a screen ``model_id`` to its OTA firmware provider.

A provider is any module exposing:
  * ``bundled_firmware_version() -> str | None``
  * ``release_app_image() -> bytes | None``

Models without OTA firmware are simply absent from the registry (the lookups
below return ``None``). This mirrors ``DISPLAY_REGISTRY``: adding OTA for a new
model means implementing the two functions and adding one entry here.
"""

from __future__ import annotations

from pathlib import Path

from hokku.common.firmware_paths import BUNDLED_FIRMWARE_DIRS
from hokku.screens import bigme_f7, huessen_epf1301, seeedstudio_e1004

_PROVIDERS = {
    "huessen_epf1301": huessen_epf1301,
    "seeedstudio_e1004": seeedstudio_e1004,
    "bigme_f7": bigme_f7,
}

#: File extension of each model's release artifact (esp32 merged bin vs XR872 img).
#: Drives both the GitHub-asset filename match and download validation.
_FIRMWARE_EXT = {
    "huessen_epf1301": "bin",
    "seeedstudio_e1004": "bin",
    "bigme_f7": "img",
}


def firmware_version_for(model_id: str | None) -> str | None:
    """Bundled firmware version for *model_id*, or None if the model has none."""
    provider = _PROVIDERS.get(model_id or "")
    return provider.bundled_firmware_version() if provider else None


def release_app_image_for(model_id: str | None) -> bytes | None:
    """OTA image bytes to stream for *model_id*, or None if the model has none."""
    provider = _PROVIDERS.get(model_id or "")
    return provider.release_app_image() if provider else None


def bundled_firmware_versions() -> dict[str, str | None]:
    """Map of every OTA-capable model_id -> its bundled firmware version."""
    return {model: provider.bundled_firmware_version() for model, provider in _PROVIDERS.items()}


def known_models() -> tuple[str, ...]:
    """The model_ids that have OTA firmware (and thus a downloadable artifact)."""
    return tuple(_PROVIDERS)


def firmware_ext(model_id: str | None) -> str | None:
    """Release-artifact extension for *model_id* (``"bin"`` / ``"img"``), or None."""
    return _FIRMWARE_EXT.get(model_id or "")


def artifact_name(model_id: str, version: str) -> str | None:
    """The release-artifact filename for *model_id* at *version*, or None.

    Mirrors the ``hokku-<model>-<version>.<ext>`` convention that ci-build.sh
    produces and that GitHub Releases carry."""
    ext = _FIRMWARE_EXT.get(model_id)
    return f"hokku-{model_id}-{version}.{ext}" if ext else None


def list_bundled_firmware(model_id: str | None) -> list[tuple[str, Path]]:
    """Every ``(version, path)`` bundled artifact for *model_id* across the
    standard bundled dirs (repo build output + package install dir).

    Unlike :func:`firmware_version_for` this is the full set, not just the
    highest — the FirmwareStore merges it with downloaded artifacts."""
    provider = _PROVIDERS.get(model_id or "")
    if provider is None:
        return []
    out: list[tuple[str, Path]] = []
    for d in BUNDLED_FIRMWARE_DIRS:
        out.extend(provider.list_firmware_files(d))
    return out


def list_model_download_files(model_id: str | None, directory: Path) -> list[tuple[str, Path]]:
    """Every ``(version, path)`` artifact for *model_id* in a single *directory*.

    Used for the writable download dir, which is not one of the bundled dirs."""
    provider = _PROVIDERS.get(model_id or "")
    if provider is None:
        return []
    return provider.list_firmware_files(directory)


def app_image_from_file(model_id: str | None, path: Path) -> bytes | None:
    """The OTA-flashable image bytes for *model_id* read from the file at *path*
    (per-model: sliced ESP app for the S3 boards, verbatim for the F7), or None."""
    provider = _PROVIDERS.get(model_id or "")
    if provider is None:
        return None
    return provider.app_image_from_file(path)


def validate_firmware_content(model_id: str | None, path: Path) -> bool:
    """True if the *contents* of *path* are a plausible firmware image for
    *model_id*, ignoring the filename/extension.

      * ESP32-S3 must parse as a valid app image; XR872 must carry the ``AWIH``
        bootloader-header magic.

    Extension-agnostic on purpose so it can validate a ``*.tmp`` staging file
    before it is renamed into place."""
    ext = _FIRMWARE_EXT.get(model_id or "")
    if ext is None:
        return False
    try:
        if ext == "img":
            with open(path, "rb") as f:
                return f.read(4) == b"AWIH"
        return bool(app_image_from_file(model_id, path))
    except OSError:
        return False


def validate_firmware_file(model_id: str | None, path: Path) -> bool:
    """True if the file at *path* is a plausible firmware image for *model_id* —
    both its extension (matches the model's artifact type) and its content
    (:func:`validate_firmware_content`)."""
    ext = _FIRMWARE_EXT.get(model_id or "")
    if ext is None or path.suffix.lstrip(".").lower() != ext:
        return False
    return validate_firmware_content(model_id, path)
