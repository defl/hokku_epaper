# Hokku Firmware

Custom firmware for the Hokku 13.3" Spectra 6 e-paper frame. Downloads images from an HTTP server, displays them, and deep sleeps until the next scheduled refresh.

## Requirements

- [ESP-IDF v5.5.x](https://docs.espressif.com/projects/esp-idf/en/v5.5.3/esp32s3/get-started/)
- ESP32-S3 with 16MB flash and 8MB octal PSRAM

## Configure

```bash
cd firmware/main
cp secrets.h.example secrets.h
```

Edit `secrets.h`:

```c
#define WIFI_SSID       "your-wifi-ssid"
#define WIFI_PASS       "your-wifi-password"
#define IMAGE_URL       "http://your-server-ip:8080/spectra6"
#define WAKE_HOURS_INIT {6, 12, 18}                      /* hours to wake and refresh */
#define TIMEZONE        "CST6CDT,M3.2.0,M11.1.0"        /* POSIX TZ string */
```

## Build

```bash
. /path/to/esp-idf/export.sh
cd firmware
idf.py build
```

On Windows with ESP-IDF Tools Installer, use the ESP-IDF command prompt or:

```bash
cd firmware
PATH="/c/Espressif/tools/xtensa-esp-elf/esp-14.2.0_20251107/xtensa-esp-elf/bin:/c/Espressif/tools/ninja/1.12.1:$PATH" \
  IDF_PATH=/c/esp/v5.5.3/esp-idf \
  ninja -C build
```

## Flash

Connect your display to the computer.

Full flash (bootloader + partition table + app):

```bash
esptool.py --chip esp32s3 --port /dev/ttyACM0 --baud 921600 write_flash \
  --flash_mode dio --flash_freq 80m --flash_size 16MB \
  0x0 build/bootloader/bootloader.bin \
  0x8000 build/partition_table/partition-table.bin \
  0x10000 build/hokku_epaper.bin
```

App only (after initial flash):

```bash
esptool.py --chip esp32s3 --port /dev/ttyACM0 --baud 921600 write_flash \
  --flash_mode dio --flash_freq 80m --flash_size 16MB \
  0x10000 build/hokku_epaper.bin
```


On Windows, replace `/dev/ttyACM0` with `COM3` (or whichever port your device is on).
