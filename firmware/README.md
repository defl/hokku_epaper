# Firmware

Custom firmware for the Hokku/Huessen 13.3" Spectra 6 e-paper frame. Downloads images from the server, displays them, and deep sleeps until the server tells it to wake.

## For users

You don't need to build the firmware — pre-built binaries are included in `firmware/release/`. Just run the setup tool:

```bash
cd tools
pip install pyserial esptool
python hokku_setup.py
```

Or on Windows: double-click `hokku_setup.bat` in the root directory.

## For developers

### Requirements

- [ESP-IDF v5.5.x](https://docs.espressif.com/projects/esp-idf/en/v5.5.3/esp32s3/get-started/)
- ESP32-S3 with 16MB flash and 8MB octal PSRAM

### Build

```bash
. /path/to/esp-idf/export.sh
cd firmware
idf.py build
```

The build timestamp is embedded as the firmware version (YYYYMMDDHHMMSSZ format) and can be read by the setup tool.

### Flash

The setup tool handles flashing automatically. For manual flashing:

```bash
esptool.py --chip esp32s3 --port /dev/ttyACM0 --baud 921600 write-flash \
  --flash-mode dio --flash-freq 80m --flash-size 16MB \
  0x0 build/bootloader/bootloader.bin \
  0x8000 build/partition_table/partition-table.bin \
  0x10000 build/hokku_epaper.bin
```

On Windows, replace `/dev/ttyACM0` with `COM3` (or whichever port your device is on).

### Configuration

All configuration (WiFi SSID/password, server URL, screen name) is stored in the NVS partition, not in source code. Use `hokku_setup.py` or `hokku_config.py` to write it.

### Important notes

- **Do not modify the display driver code** (SPI init, CS, BUSY polling, GPIO init, epaper_reset, epaper_init_panel, epaper_send_panel, epaper_display_dual). See `CLAUDE.md` for details.
- The firmware has a 120-second reflash window before every deep sleep.
- When USB is connected (charging), the firmware stays awake instead of deep sleeping to avoid boot loops.
- After flashing, restore the factory firmware dump first for a clean display state (the setup tool handles this).
