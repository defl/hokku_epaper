"""EPF1301 (EL133UF1) Hokku frame: detection, NVS config, and USB flashing.

Shared by the webserver flash feature (``hokku.webserver.flashing``) and the
``tools/`` CLI. All esptool / NVS-generator access is via subprocess, so it is
safe to call from a threaded web server.
"""

from __future__ import annotations

from . import constants
from .constants import (
    CONFIG_VERSION,
    ESP32S3_PID,
    ESP32S3_VID,
    NVS_OFFSET,
    NVS_SIZE,
)
from .device import (
    list_serial_ports,
    parse_device_state,
    read_device_flash,
    scan_devices,
)
from .firmware import (
    bundled_firmware_version,
    merged_firmware_file,
    release_app_header,
    release_app_image,
    resolve_firmware_dir,
)
from .flasher import (
    EsptoolError,
    flash_device,
    flash_firmware,
    write_config,
)
from .nvs import (
    NvsToolUnavailable,
    build_nvs_binary,
    migrate_config,
    nvs_tool_available,
    read_nvs,
)

__all__ = [
    "CONFIG_VERSION",
    "ESP32S3_PID",
    "ESP32S3_VID",
    "NVS_OFFSET",
    "NVS_SIZE",
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
