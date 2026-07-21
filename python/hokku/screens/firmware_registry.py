"""Map a screen ``model_id`` to its OTA firmware provider.

A provider is any module exposing:
  * ``bundled_firmware_version() -> str | None``
  * ``release_app_image() -> bytes | None``

Models without OTA firmware are simply absent from the registry (the lookups
below return ``None``). This mirrors ``DISPLAY_REGISTRY``: adding OTA for a new
model means implementing the two functions and adding one entry here.
"""

from __future__ import annotations

from hokku.screens import bigme_f7, huessen_epf1301, seeedstudio_e1004

_PROVIDERS = {
    "huessen_epf1301": huessen_epf1301,
    "seeedstudio_e1004": seeedstudio_e1004,
    "bigme_f7": bigme_f7,
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
