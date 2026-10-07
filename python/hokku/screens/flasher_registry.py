"""Map a screen ``model_id`` to its USB-flashable ESP32-S3 screen module.

An ESP32 screen module exposes the full flash + NVS-config surface bound to its
own :class:`~hokku.common.esp32.spec.Esp32Spec` (flash size, offsets, artifact
name): ``flash_device``, ``merged_firmware_file``, ``scan_devices``,
``migrate_config``, ``build_nvs_binary``, ``CONFIG_VERSION``, ``NvsToolUnavailable``.

Only ESP32-S3 boards live here — the Bigme F7 (XR872 mask-BROM + device-local
FDCM config) is flashed via its own bootstrap path and is deliberately absent.
The two ESP32 boards enumerate differently (huessen as native USB Serial/JTAG,
the E1004 through its CH340K bridge), so a scan passing :func:`esp32_specs`
reports each device's model; the operator's model choice still keys the flash.
"""

from __future__ import annotations

from hokku.screens import huessen_epf1301, seeedstudio_e1004

_ESP32_SCREENS = {
    "huessen_epf1301": huessen_epf1301,
    "seeedstudio_e1004": seeedstudio_e1004,
}


def esp32_screen(model_id: str | None):
    """Return the ESP32 screen module for *model_id*, or ``None`` if not an
    ESP32-S3 screen (unknown model, or the F7)."""
    return _ESP32_SCREENS.get(model_id or "")


def esp32_models() -> list[str]:
    """Every USB-flashable ESP32-S3 model_id, in registration order."""
    return list(_ESP32_SCREENS)


def esp32_screens() -> list:
    """Every USB-flashable ESP32-S3 screen module, in registration order."""
    return list(_ESP32_SCREENS.values())


def esp32_specs() -> list:
    """Every ESP32-S3 screen's :class:`Esp32Spec`, for recognising any of them by
    USB id without knowing the model up front."""
    return [screen.SPEC for screen in _ESP32_SCREENS.values()]
