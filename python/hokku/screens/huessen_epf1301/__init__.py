"""EPF1301 (EL133UF1) Hokku frame: detection, NVS config, and USB flashing.

A thin binding of the shared ESP32-S3 flash/OTA/NVS layer
(:mod:`hokku.common.esp32`) to this board's :class:`Esp32Spec`. The public
surface below (consumed by ``hokku.webserver`` and the ``tools/`` CLI by name) is
unchanged; each callable is the shared implementation with ``SPEC`` pre-bound.
"""

from __future__ import annotations

from functools import partial

from hokku.common.esp32 import device as _device
from hokku.common.esp32 import firmware as _firmware
from hokku.common.esp32 import flasher as _flasher
from hokku.common.esp32 import nvs as _nvs
from hokku.common.esp32.spec import Esp32Spec

from . import constants
from .constants import (
    CONFIG_VERSION,
    ESP32S3_PID,
    ESP32S3_VID,
    NVS_OFFSET,
    NVS_SIZE,
)

SPEC = Esp32Spec(
    model_id="huessen_epf1301",
    flash_size=constants.FLASH_SIZE,
    config_version=constants.CONFIG_VERSION,
    nvs_offset=constants.NVS_OFFSET,
    nvs_size=constants.NVS_SIZE,
    app_offset=constants.APP_OFFSET,
    bootloader_offset=constants.BOOTLOADER_OFFSET,
    vid=constants.ESP32S3_VID,
    pid=constants.ESP32S3_PID,
    baud=constants.ESPTOOL_BAUD,
    nvs_namespace=constants.NVS_NAMESPACE,
)

# Exceptions (shared).
EsptoolError = _flasher.EsptoolError
NvsToolUnavailable = _nvs.NvsToolUnavailable

# Firmware / OTA (spec-bound).
merged_firmware_file = partial(_firmware.merged_firmware_file, SPEC)
release_app_header = partial(_firmware.release_app_header, SPEC)
bundled_firmware_version = partial(_firmware.bundled_firmware_version, SPEC)
release_app_image = partial(_firmware.release_app_image, SPEC)
resolve_firmware_dir = partial(_firmware.resolve_firmware_dir, SPEC)

# NVS (spec-bound; nvs_tool_available takes no spec).
build_nvs_binary = partial(_nvs.build_nvs_binary, SPEC)
migrate_config = partial(_nvs.migrate_config, SPEC)
read_nvs = partial(_nvs.read_nvs, SPEC)
nvs_tool_available = _nvs.nvs_tool_available

# Device detection (spec-bound).
list_serial_ports = partial(_device.list_serial_ports, SPEC)
read_device_flash = partial(_device.read_device_flash, SPEC)
parse_device_state = partial(_device.parse_device_state, SPEC)
scan_devices = partial(_device.scan_devices, SPEC)

# Flashing (spec-bound).
flash_firmware = partial(_flasher.flash_firmware, SPEC)
write_config = partial(_flasher.write_config, SPEC)
flash_device = partial(_flasher.flash_device, SPEC)

__all__ = [
    "CONFIG_VERSION",
    "ESP32S3_PID",
    "ESP32S3_VID",
    "NVS_OFFSET",
    "NVS_SIZE",
    "SPEC",
    "EsptoolError",
    "NvsToolUnavailable",
    "build_nvs_binary",
    "bundled_firmware_version",
    "constants",
    "flash_device",
    "flash_firmware",
    "list_serial_ports",
    "merged_firmware_file",
    "migrate_config",
    "nvs_tool_available",
    "parse_device_state",
    "read_device_flash",
    "read_nvs",
    "release_app_header",
    "release_app_image",
    "resolve_firmware_dir",
    "scan_devices",
    "write_config",
]
