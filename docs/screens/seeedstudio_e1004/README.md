# Seeed reTerminal E1004 (`seeedstudio_e1004`)

A 13.3" E Ink Spectra 6 panel (T133A01, 1200×1600) on a Seeed XIAO ESP32-S3
mounted on the reTerminal E-Series baseboard — same panel family and resolution
as the Hokku/Huessen frame, on open, documented hardware.

> ## ⚠️ Confirmed on hardware once, lightly tested
>
> A first end-to-end run on a physical E1004 is confirmed
> ([issue #14](https://github.com/defl/hokku_epaper/issues/14)): built with
> ESP-IDF v5.5.5, flashed over USB, then WiFi + server fetch + a photo rendered
> with correct colours and orientation, and the frame registered in the dashboard
> with a sane battery reading — so the ESP-IDF SPI/DC/DMA plumbing and the ×2.0
> battery divider both check out.
>
> That is **one unit, one session** — not the long-running fleet history behind
> the huessen frame and the Bigme F7. Deep sleep over days, OTA and battery
> behaviour over a full discharge are still unproven.
>
> **Known issue:** app logs do not reach the native USB serial console. See
> [serial console](#serial-console-known-issue) below.

## Documentation

- [Hardware facts](hardware_facts.md) — confirmed from Seeed's wiki and driver:
  SoC, panel, GPIO map, expansion header
- [Hardware guesses](hardware_guesses.md) — inferences not yet E1004-confirmed,
  including the USB-detect behaviour that does **not** carry over from the
  huessen frame
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

## Serial console (known issue)

The firmware is built with `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y` (the same
setting the huessen frame uses, where it works). On the E1004, the native USB
port shows the ROM bootloader banner and then goes **completely silent** for the
rest of boot — no second-stage bootloader line, no app logs. Reproduced with
both `idf.py monitor` and a raw reset-pulse capture.

The device is not hung: the panel renders normally throughout, which is how the
first hardware run was verified at all. Only the log stream is missing. The
Arduino reference example for this board notes that diagnostic output goes out
`Serial0` / UART0, so the likely explanation is that the console ends up on the
physical UART0 pins rather than the USB Serial/JTAG peripheral — but that has
not been confirmed with a probe on the header.

**If you are bringing this board up, do not read silence on the USB port as a
crash.** Watch the panel, or use the frame's own diagnostics instead: the
firmware keeps a log ring and reports state to the server via `X-Frame-State`,
both visible in the web app. Reported in
[issue #14](https://github.com/defl/hokku_epaper/issues/14).
