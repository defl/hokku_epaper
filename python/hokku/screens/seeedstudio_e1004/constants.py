"""ESP32-S3 / NVS constants for the Seeed reTerminal E1004.

Partition layout matches ``firmware/seeedstudio_e1004/partitions.csv`` (A/B OTA,
32 MB flash). The NVS binary-format constants are the ESP32-S3 standard, identical
to huessen_epf1301; the USB VID:PID is not (see below). ``CONFIG_VERSION`` must
match the seeed firmware's CONFIG_VERSION (the CONFIG digit of ``firmware/seeedstudio_e1004/VERSION``
= PROTOCOL.CONFIG.N); seeed shares huessen's exact config schema, hence 2.
"""

from __future__ import annotations

# USB VID:PID the board enumerates as. Unlike huessen, the USB-C port is an
# external CH340K bridge on UART0, not the SoC's native USB Serial/JTAG, so the
# host sees 1A86:7522 and never 303A:1001 (docs/screens/seeedstudio_e1004/
# hardware_facts.md). Not to be confused with the Bigme F7's CH340 (1A86:7523).
USB_VID = 0x1A86
USB_PID = 0x7522

# NVS partition location (from firmware/seeedstudio_e1004/partitions.csv).
NVS_OFFSET = 0x9000
NVS_SIZE = 0x6000  # 24 KB

# NVS namespace used by the firmware.
NVS_NAMESPACE = "hokku"

# NVS partition binary format constants (ESP-IDF NVS) — identical everywhere.
NVS_PAGE_SIZE = 4096
NVS_ENTRY_SIZE = 32
U8_TYPE = 0x01
STR_TYPE = 0x21
PAGE_ACTIVE = 0xFFFFFFFE

# NVS config schema version. Must match the seeed firmware's CONFIG_VERSION
# (shared config schema with huessen -> 2).
CONFIG_VERSION = 2

# Merged image is written at 0x0; APP_OFFSET is the (ota_0) app descriptor.
BOOTLOADER_OFFSET = 0x0
APP_OFFSET = 0x10000

# A/B OTA partitions (see firmware/seeedstudio_e1004/partitions.csv). The merged
# image only covers ota_0; otadata selects which slot the bootloader runs, so a
# USB flash must clear it (blank otadata -> ota_0) or the device keeps booting
# whatever an earlier OTA left in ota_1.
OTA1_OFFSET = 0x310000
OTADATA_OFFSET = 0x610000
OTADATA_SIZE = 0x2000

# esptool --flash-size for this board (32 MB flash — the E1004's ESP32-S3R8).
FLASH_SIZE = "32MB"

# esptool baud rate used for all flash operations.
ESPTOOL_BAUD = "921600"
