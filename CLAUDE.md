This is a project where you're writing firmware for an ESP32 that drives an e-ink display.

Hardware
========
- The known facts are in HARDWARE_FACTS.md, though this might be wrong so treat with caution

NVS Config Version
==================
- Current config version: 1
- Stored as uint8 "cfg_ver" in NVS namespace "hokku"
- Defined in firmware/main/main.c as CONFIG_VERSION and in tools/hokku_config.py as CONFIG_VERSION
- INCREMENT THIS VALUE every time NVS config fields are added, removed, or changed
- Firmware refuses to boot if cfg_ver doesn't match its CONFIG_VERSION
- hokku-setup treats mismatched cfg_ver as unconfigured

Display driver
==============
- DO NOT MODIFY the display driver code (SPI init, CS management, BUSY polling, GPIO init, epaper_reset, epaper_init_panel, epaper_send_panel, epaper_display_dual, epaper_wait_busy). It must remain identical to the main branch. Changes that look harmless (manual CS, skipping gpio_reset_pin on BUSY, fixed delays instead of BUSY polling) all break the display in subtle ways.
- GPIO0 (SPI CS) is a boot strapping pin — the SPI driver must manage it (spics_io_num = PIN_EPAPER_CS), not manual gpio_set_level
- GPIO7 (BUSY) has an external pull-up on the PCB. gpio_reset_pin enables an internal pull-up too. Both are needed for correct BUSY signaling. Do not skip gpio_reset_pin for BUSY.
- display_message() must use split_and_display() — the exact same function used for downloaded images. The buffer layout must be identical: first 480K = panel 1, second 480K = panel 2.
- After flashing the factory firmware dump (.private/flash_dump.bin) before our firmware, wait 30s for the display controller to fully reset. The factory restore puts the display in a known good state.

Flashing procedure
==================
- For reliable results, flash the factory dump first, wait 30s, then flash our firmware: factory dump → 30s wait → bootloader + partition table + app → NVS config
- esptool works any time USB is connected regardless of firmware state (resets into ROM bootloader)
- Reflash strategy depends on regime: USB_AWAKE never deep-sleeps so the chip is always reachable while plugged into a computer. BATTERY_IDLE has only a 5 s awake window per refresh — to reflash, plug into USB to push the chip into USB_AWAKE first.

Dithering
=========
- The image dithering pipeline (webserver/webserver.py) is explained in detail for humans in docs/dithering.md. If you change anything that affects dither output — algorithms, palette, saturation/vividness knobs, B&W detection, cache versioning — update docs/dithering.md to match. It is the one document that's meant to stay in sync with the code for non-AI readers.

Coding and compiling
====================
- always git commit firmware code before building and flashing, the comment is a 1 line summary of the change
- never use the ESP32 USB pins for anything, leave them in their original state such that USB always works
- always double check that you didn't create a fast boot loop by accident
- the firmware never auto-refreshes on boot (boot is not a refresh trigger). Refreshes happen only on schedule, button press, or first install. The reflash window on battery is 5 s by spec — if you need longer access, plug USB to enter USB_AWAKE which never sleeps.
- hard_reset after flashing ESP32 automatically
- the python environment to use is in .venv in the same directory as this file
