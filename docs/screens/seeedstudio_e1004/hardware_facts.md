# Seeed reTerminal E1004 — Hardware Facts

13.3" E Ink Spectra 6 (T133A01 dual-chip panel) on a Seeed XIAO ESP32-S3 (ESP32-S3R8)
mounted on the reTerminal E-Series baseboard. Product page:
https://www.seeedstudio.com/reTerminal-E1004-p-6692.html

Only confirmed / definitively-documented information lives here. Inferences and
sibling-board-derived values that are not E1004-confirmed belong in
[`hardware_guesses.md`](hardware_guesses.md). (Per root `AGENTS.md` "Hardware" rule.)

## Platform (CONFIRMED — Seeed E1004 wiki)
- **SoC**: ESP32-S3R8 (XIAO ESP32-S3 module)
- **Flash**: 32 MB
- **PSRAM**: 8 MB, OPI (octal). Mandatory — the 960,000-byte panel framebuffer does not fit in internal SRAM.
- Source: https://wiki.seeedstudio.com/getting_started_with_reterminal_e1004/

## Display (CONFIRMED)
- **Panel**: T133A01, 13.3", E Ink Spectra 6 (6 colours), 1200×1600 total.
- **Architecture**: dual-chip — two controllers on one SPI bus, each driving a 600×1600 half (left/right split). Confirmed against `huessen_epf1301` cross-validation (see that screen's `hardware_guesses.md`).
- **Panel control GPIOs** (CONFIRMED from Seeed's own `Seeed_GxEPD2/examples/GxEPD2_reTerminal_E1004/GxEPD2_reTerminal_E1004.ino`):

| Signal | GPIO |
|--------|------|
| SCK | 7 |
| MISO | 8 |
| MOSI | 9 |
| CS0 (chip 0, left half) | 10 |
| DC | 11 |
| CS1 (chip 1, right half) | 2 |
| RST | 38 |
| BUSY | 13 |
| ENABLE | 12 |

- **Palette (native nibble encoding)**: Black=0x0, White=0x1, Yellow=0x2, Red=0x3, Blue=0x5, Green=0x6 — identical to the hokku wire format, so no colour remap is needed when driving the panel directly (see `firmware/seeedstudio_e1004/README.md`). Cross-validated against the reverse-engineered `huessen_epf1301` palette.

## Expansion header J2 (CONFIRMED — E1004 wiki)
- I2C1: SDA=GPIO39, SCL=GPIO40
- UART1: RX=GPIO42, TX=GPIO41

## Battery voltage sense (CONFIRMED — corroborated by Seeed ESPHome cookbook + Zephyr E-Series devicetree + the community Arduino port that was hardware-tested end-to-end)
- **ADC pin**: GPIO1 (ESP32-S3 ADC1_CH0). RTC-capable.
- **Divider-enable pin**: GPIO21, active-HIGH — must be driven HIGH to read the battery; otherwise the divider path is disconnected. RTC-capable. Drive LOW before sleep to avoid leaking through the divider.
- **Divider ratio**: 2.0× (10 kΩ / 10 kΩ). Vbatt = Vadc × 2. Corroborated on a
  real E1004 running this firmware — the frame reported a plausible battery %
  in the dashboard ([issue #14](https://github.com/defl/hokku_epaper/issues/14)).
  That is a sanity check, not a metered calibration across the discharge curve.
- Sources: https://wiki.seeedstudio.com/reterminal_e10xx_with_esphome_advanced/ ; Zephyr `boards/seeed/reterminal_e1002` devicetree (shared baseboard).

## RTC-wake-capable GPIOs
ESP32-S3 RTC GPIOs are GPIO0–GPIO21 only. Of the pins used here, GPIO1, 2, 12, 13, 21 are RTC-capable; GPIO7/8/9/10 (SPI), 38 (RST), 39/40 (I2C1), 41/42 (UART1) are NOT and cannot serve as EXT1 deep-sleep wake sources.
