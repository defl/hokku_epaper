# Seeed reTerminal E1004 (`seeedstudio_e1004`)

A 13.3" E Ink Spectra 6 panel (T133A01, 1200×1600) on a Seeed XIAO ESP32-S3
mounted on the reTerminal E-Series baseboard — same panel family and resolution
as the Hokku/Huessen frame, on open, documented hardware.

> ## ⚠️ Experimental — never run on real hardware
>
> The firmware compiles, links against the real ESP-IDF toolchain in CI, and
> passes the host test suite. **It has never been flashed to a physical E1004.**
>
> - The panel registers and pinout come from Seeed's own `Seeed_GxEPD2` driver
>   and a community Arduino port that *was* hardware-tested.
> - **Unverified on silicon:** the ESP-IDF SPI/DC/DMA plumbing, and the battery
>   divider ratio (GPIO1 ADC × 2.0 — see [hardware guesses](hardware_guesses.md)).
>
> If you own one, please try it and
> [tell us how it went](https://github.com/defl/hokku_epaper/issues) — that's the
> one thing standing between this and full support.

## Documentation

- [Hardware facts](hardware_facts.md) — confirmed from Seeed's wiki and driver:
  SoC, panel, GPIO map, expansion header
- [Hardware guesses](hardware_guesses.md) — inferences not yet E1004-confirmed,
  including the battery divider and the USB-detect behaviour that does **not**
  carry over from the huessen frame
- [Firmware source and build](../../../firmware/seeedstudio_e1004/README.md)

## How it's built

The firmware is a thin **board layer** over the shared code in
[`firmware/common/`](../../../firmware/common/) — the same modules the
huessen frame uses — so it inherits feature parity for free: NVS config,
`X-Frame-State` telemetry, the diagnostic log ring, A/B OTA, server clock sync and
scheduled deep sleep. Only the board-specific parts live in its `main.c`: the
T133A01 panel driver, the battery ADC, GPIO/SPI init, and the sleep state machine.

The panel bring-up work this is based on was contributed by
[@TaichungLester](https://github.com/TaichungLester) in
[PR #16](https://github.com/defl/hokku_epaper/pull/16) (see also
[issue #14](https://github.com/defl/hokku_epaper/issues/14)). That PR was closed
in favour of rebuilding on the shared ESP-IDF foundation rather than as a separate
Arduino client.

## Panel at a glance

| | |
|---|---|
| Panel | E Ink Spectra 6 (T133A01, dual-chip), 13.3" |
| Resolution | 1200 × 1600 (two 600×1600 halves, left/right split) |
| Colours | Black, white, yellow, red, blue, green |
| SoC | ESP32-S3R8 (XIAO ESP32-S3), 32 MB flash, 8 MB OPI PSRAM |
| WiFi | 2.4 GHz only |
| Product page | [seeedstudio.com](https://www.seeedstudio.com/reTerminal-E1004-p-6692.html) |

The panel's native nibble encoding matches Hokku's wire format exactly, so no
colour remapping is needed when driving it.
