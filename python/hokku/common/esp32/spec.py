"""Per-screen specification for the shared ESP32-S3 flash/OTA layer.

Everything an ESP32-S3 hokku screen needs to differ on is captured here. The NVS
binary format, ESP-IDF image parsing, and esptool invocation are identical across
screens; only these values change:
  - ``flash_size``      — esptool ``--flash-size`` ("16MB" / "32MB")
  - ``config_version``  — NVS schema version (must equal the firmware's CONFIG_VERSION)
  - partition offsets   — from the screen's ``firmware/<model>/partitions.csv``
  - ``model_id``        — also derives the release-artifact name ``hokku-<model>-<ver>.bin``

The USB VID:PID and NVS-format constants are the same for every ESP32-S3 board,
so they default; a screen only overrides them if its hardware genuinely differs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Esp32Spec:
    model_id: str
    flash_size: str
    config_version: int
    nvs_offset: int
    nvs_size: int
    app_offset: int
    bootloader_offset: int = 0x0

    # ESP32-S3 USB Serial/JTAG VID:PID (same for every board here).
    vid: int = 0x303A
    pid: int = 0x1001
    baud: str = "921600"

    # NVS namespace + ESP-IDF NVS binary-format constants (identical everywhere).
    nvs_namespace: str = "hokku"
    nvs_page_size: int = 4096
    nvs_entry_size: int = 32
    u8_type: int = 0x01
    str_type: int = 0x21
    page_active: int = 0xFFFFFFFE

    @property
    def merged_glob(self) -> str:
        """Release-artifact glob, e.g. ``hokku-huessen_epf1301-*.bin``."""
        return f"hokku-{self.model_id}-*.bin"

    @property
    def merged_re(self) -> re.Pattern[str]:
        """Regex capturing the version from the release-artifact filename."""
        return re.compile(rf"^hokku-{re.escape(self.model_id)}-(.+)\.bin$")
