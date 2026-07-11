"""ESP32-S3 / NVS constants for the EPF1301 (EL133UF1) Hokku frame.

Single source of truth for the wire-level constants shared by the webserver
flasher and the ``tools/`` CLI. ``tools/hokku_config.py`` and
``tools/esp32_setup.py`` import these from here — do not redefine them there.

``CONFIG_VERSION`` must match the firmware's NVS schema version (firmware
``CONFIG_VERSION``, derived from ``firmware/huessen_epf1301/VERSION``). Bump both together.
"""

from __future__ import annotations

# ESP32-S3 USB Serial/JTAG VID:PID
ESP32S3_VID = 0x303A
ESP32S3_PID = 0x1001

# NVS partition location (from firmware/huessen_epf1301/partitions.csv)
NVS_OFFSET = 0x9000
NVS_SIZE = 0x6000  # 24 KB

# NVS namespace used by the firmware
NVS_NAMESPACE = "hokku"

# NVS partition binary format constants (ESP-IDF NVS)
NVS_PAGE_SIZE = 4096
NVS_ENTRY_SIZE = 32
# Entry types. uint8 (0x01) and namespace share the same type code; namespace
# entries have ns_idx=0, data entries have ns_idx>0.
U8_TYPE = 0x01
STR_TYPE = 0x21
# Page states
PAGE_ACTIVE = 0xFFFFFFFE

# NVS config schema version. Must match firmware's CONFIG_VERSION.
CONFIG_VERSION = 2

# Flash offsets inside the merged firmware image. The whole merged image is
# written at 0x0; APP_OFFSET is where the app descriptor / version string lives.
BOOTLOADER_OFFSET = 0x0
APP_OFFSET = 0x10000

# esptool baud rate used for all flash operations.
ESPTOOL_BAUD = "921600"
