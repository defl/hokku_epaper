# Firmware — Developer Notes

Custom firmware for the Hokku/Huessen 13.3" Spectra 6 e-paper frame. Downloads images from the server, displays them, and deep sleeps until the server tells it to wake.

For flashing and configuration see [docs/install.md](../../docs/install.md).

### Requirements

- [ESP-IDF v5.5.x](https://docs.espressif.com/projects/esp-idf/en/v5.5.3/esp32s3/get-started/)
- ESP32-S3 with 16MB flash and 8MB octal PSRAM

### Build

```bash
. /path/to/esp-idf/export.sh
cd firmware/huessen_epf1301
idf.py build          # or: bash ci-build.sh  (produces the merged release .bin)
```

The firmware version is `VERSION` (`PROTOCOL.CONFIG.N`, see [AGENTS.md](AGENTS.md)); the build timestamp is embedded separately as debug metadata.

### Flash

The setup tool handles flashing automatically. For manual flashing:

```bash
esptool.py --chip esp32s3 --port /dev/ttyACM0 write_flash 0x0 hokku-huessen_epf1301-<tag>.bin
```

On Windows, replace `/dev/ttyACM0` with `COM3` (or whichever port your device is on).

### Serial console

On the native USB Serial/JTAG port, only while a USB host is connected (USB_AWAKE). Commands: `ping`, `help`, `frame` (upload a full 960 KB panel image and refresh — protocol in [`frame_proto.h`](../common/all/frame_proto.h)) and `interactive on|off` (stop scheduled refreshes, button refreshes and sleep while a host drives the console — [`interactive.h`](../common/all/interactive.h); any reset clears it). Host side: `tools/send_frame.py --model huessen_epf1301 --port <port> --solid red` (asserts interactive mode unless `--no-interactive`).

### Important notes

- **Do not modify the display driver code** (SPI init, CS, BUSY polling, GPIO init, `epaper_reset`, `epaper_init_panel`, `epaper_send_panel`, `epaper_display_dual`). See [`AGENTS.md`](AGENTS.md) for details.
- **State-machine architecture** — the firmware's top-level behaviour is a 4-state machine (USB_AWAKE / BATTERY_IDLE / DEEP_SLEEP / REFRESH). Design spec at `docs/screens/huessen_epf1301/firmware_design.md`; don't change the semantics without updating the spec first.
- **Boot is never a refresh trigger.** The image changes only on: a scheduled refresh time fires, a button press, or the very first install after a clean flash. Plugging USB in / out does not change the image.
- **RTC state survival** — all persistent counters use `RTC_NOINIT_ATTR`, not `RTC_DATA_ATTR`. RTC_DATA_ATTR re-runs its initialiser on every `esp_restart`, which silently wipes counters. If you add new persistent state, use RTC_NOINIT_ATTR and zero-init it in the `rtc_magic` validation block at the top of `app_main`.
- **Reflash reachability** — USB_AWAKE never deep-sleeps so the chip is always reachable while the cable is plugged in. BATTERY_IDLE has only a 5 s awake window; to reflash a battery-only frame, plug in USB which transitions to USB_AWAKE.
