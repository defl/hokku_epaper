# Firmware — Developer Notes

ESP-IDF firmware for the Seeed reTerminal E1004 (13.3" Spectra 6, T133A01
dual-chip panel) as a hokku screen. It is a thin **board layer** over the shared
appliance code in [`firmware/common/`](../common/) — the same modules
`huessen_epf1301` uses — so it has feature parity with the other ESP32 screen:
NVS config, `X-Frame-State` telemetry, the diagnostic log ring, A/B OTA, server
clock sync, and scheduled deep-sleep.

Only the board-specific parts live in `main.c`: the T133A01 panel driver, the
battery ADC, GPIO/SPI init, and the deep-sleep state machine. Everything else is
`#include`d from `common/esp32` (ESP-IDF, shared with huessen) and `common/all`
(pure C, shared with the XR872 F7 too).

The panel driver/register values originate from Seeed's own `Seeed_GxEPD2`
T133A01 driver, by way of the Arduino reTerminal E1004 client contributed by
[@TaichungLester](https://github.com/TaichungLester) in
[PR #16](https://github.com/defl/hokku_epaper/pull/16) (see also
[issue #14](https://github.com/defl/hokku_epaper/issues/14)). That PR was closed
in favour of rebuilding the client on the shared ESP-IDF `firmware/common/`
foundation, but its panel bring-up work is what this driver is based on.

## Status — read before flashing

**Confirmed on real E1004 hardware once** — see
[issue #14](https://github.com/defl/hokku_epaper/issues/14).
- Built with ESP-IDF v5.5.5 (`idf.py set-target esp32s3 && idf.py build`),
  flashed over USB, provisioned with
  `tools/hokku_config.py set --ssid ... --url ... --name ...`. WiFi connected,
  the server fetch succeeded, and the panel rendered a photo with correct
  colours and orientation (no rotation or mirroring needed). The frame
  registered in the dashboard with a sane battery %, so the **×2.0 battery
  divider** on GPIO1 and the **ESP-IDF SPI/DC/DMA plumbing** are both confirmed.
- The shared logic is unit-tested on the host (`test/host/`) and exercised by
  huessen's suite; the whole firmware compiles + links in CI via the real
  ESP-IDF toolchain.
- **Still unproven:** deep sleep across days, an OTA on this board, and battery
  behaviour over a full discharge. One unit, one session — not a track record.

**Serial console is on UART0, not native USB Serial/JTAG.** This baseboard's
USB-C port is an external **CH340K** bridge wired to UART0 (GPIO43/44), so the
console is `CONFIG_ESP_CONSOLE_UART_DEFAULT=y` — unlike huessen, which uses the
SoC's native USB Serial/JTAG. Plugged in, the port enumerates as a CH340K
(`VID_1A86:PID_7522`), and `idf.py monitor` at 115200 shows the full boot log.
Early builds copied huessen's `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`, which
pointed the console at a peripheral with no path to the host (ROM banner, then
silence — looked like a hang, wasn't); diagnosed and fixed on real hardware in
[issue #14](https://github.com/defl/hokku_epaper/issues/14) /
[PR #23](https://github.com/defl/hokku_epaper/pull/23).

**Flashing safety (root `AGENTS.md` STOP rules):** this firmware now has **A/B
OTA with bootloader rollback** (`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`, ota_0/
ota_1 slots — see `partitions.csv`) and an early recovery hatch (button-wake:
holding a button wakes the device from deep sleep so it's reachable to reflash).
A bad OTA image self-checks and rolls back automatically.

## Board vs. shared

| Board-specific (`main.c`) | Shared (`common/`) |
|---|---|
| T133A01 dual-CS panel driver, DC line | `net` — HTTP image fetch + header capture |
| battery ADC (GPIO1 + enable GPIO21, ×2) | `ota` — A/B OTA (model-aware) |
| GPIO/SPI init | `wifi` — dual-network connect + BSSID cache |
| deep-sleep (timer + button GPIO3/4/5 wake) | `config` — NVS config store |
| frame-state gatherer (fills the struct) | `log` — crash-safe RTC log ring |
| `display_message` (text via `text_render`) | `scheduler`, `state`, `frame_state`, `text_render` |

## Differences from `huessen_epf1301`

- **No USB-awake regime.** The E1004 has **no USB-host-detect GPIO** (external
  power is only readable over I²C from the SY6974B charger — see
  `hardware_guesses.md`), so there's no USB_AWAKE/BATTERY_IDLE split. It's a
  deep-sleep appliance: wake (timer or button) → refresh → sleep. `X-Frame-State`
  therefore always reports `usb:"none"`.
- **DC line.** The T133A01 has a real DC GPIO (11): LOW=command, HIGH=data,
  toggled around each command/data pair. huessen's UC8179C uses the SPI
  peripheral's hardware command-phase instead.
- **No colour LUT.** hokku's wire nibbles are already the T133A01's native
  encoding, so the downloaded bytes DMA straight to the panel (the Arduino
  client needs a LUT only because it routes through GxEPD2's framebuffer).

## Requirements

- ESP-IDF v5.5.x (same as `firmware/huessen_epf1301`)
- Seeed reTerminal E1004 (ESP32-S3R8, 8 MB OPI PSRAM, 32 MB flash)

## Build

```bash
. /path/to/esp-idf/export.sh
cd firmware/seeedstudio_e1004
idf.py build          # or: bash ci-build.sh  (produces the merged release .bin)
```

## Host tests

```bash
cmake -B test/host/build test/host && cmake --build test/host/build
ctest --test-dir test/host/build --output-on-failure
```
