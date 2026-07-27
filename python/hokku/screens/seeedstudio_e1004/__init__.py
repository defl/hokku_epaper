"""Server-side support for the Seeed reTerminal E1004 screen (ESP32-S3).

The display (image serving) is a pure model-id override of huessen (identical
wire format). Firmware/OTA serving and USB flashing bind this board's
:class:`Esp32Spec` (32 MB flash, artifact name) to the shared ESP32-S3 layer in
:mod:`hokku.common.esp32`. The public surface mirrors huessen_epf1301.
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
from .display import SeeedstudioE1004Display

SPEC = Esp32Spec(
    model_id="seeedstudio_e1004",
    flash_size=constants.FLASH_SIZE,
    config_version=constants.CONFIG_VERSION,
    nvs_offset=constants.NVS_OFFSET,
    nvs_size=constants.NVS_SIZE,
    app_offset=constants.APP_OFFSET,
    bootloader_offset=constants.BOOTLOADER_OFFSET,
    ota1_offset=constants.OTA1_OFFSET,
    otadata_offset=constants.OTADATA_OFFSET,
    otadata_size=constants.OTADATA_SIZE,
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
list_firmware_files = partial(_firmware.list_firmware_files, SPEC)
app_image_from_file = partial(_firmware.app_image_from_file, SPEC)

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
active_ota_slot = _device.active_ota_slot  # pure otadata parse, takes no spec

# Flashing (spec-bound).
flash_firmware = partial(_flasher.flash_firmware, SPEC)
describe_flash_parts = partial(_flasher.describe_flash_parts, SPEC)
describe_config_part = partial(_flasher.describe_config_part, SPEC)
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
    "SeeedstudioE1004Display",
    "active_ota_slot",
    "app_image_from_file",
    "build_nvs_binary",
    "bundled_firmware_version",
    "constants",
    "describe_config_part",
    "describe_flash_parts",
    "flash_device",
    "flash_firmware",
    "list_firmware_files",
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
