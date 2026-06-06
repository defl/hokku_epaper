# Bigme F7 — Hardware Guesses

Inferences, estimates, SDK-derived defaults, and unknowns. Nothing here is confirmed on the actual device.
Confirmed findings get moved to [`hardware_facts.md`](hardware_facts.md).

## Platform

- **OS**: Bare-metal RTOS, likely XR Skylark SDK (FreeRTOS-based) — inferred from SoC identity; no Android/Linux on Cortex-M4
- **External flash**: QSPI NOR flash — **confirmed 4 MB Zbit JEDEC 0x5E4016** (moved to facts). Boot partition code also contains drivers for Puya P25QXXH and XTX XT25WXXB flash chips — inferred that the same firmware binary supports multiple hardware variants.
- **USB port**: confirmed present with CH340 USB-to-serial (VID 1A86:7523, COM6); exposes UART0 at 115200 baud — confirmed working for boot log capture.

## GPIO Map

All pin assignments below are XR872AT SDK defaults. The device vendor may use any GPIO for any function. Treat as starting points for probing only.

| GPIO | Expected Function | Basis |
|------|------------------|-------|
| PB0 | UART0_TX | SDK default (primary) |
| PB1 | UART0_RX | SDK default (primary) |
| PB2 | SWD_SWDIO | SDK default (secondary SWD — preferred to avoid UART conflict) |
| PB3 | SWD_SWDCLK | SDK default (secondary SWD) |
| PB4 | FLASH_MOSI | XR872AT hardware-fixed |
| PB5 | FLASH_MISO | XR872AT hardware-fixed |
| PB6 | FLASH_CS | XR872AT hardware-fixed |
| PB7 | FLASH_CLK | XR872AT hardware-fixed |

Display SPI CS, RST, BUSY, PWR pins: entirely unknown — to be determined from firmware analysis.

## UART / Boot Protocol

From XR872AT SDK documentation — not verified on this device:

- UART0 boot log baud: 115200
- PhoenixMC flash tool baud: 921600
- ROM download mode trigger: send `"upgrade"` + 50×`'U'` over UART0 → ROM responds `"OKBOOM"` → enters download mode
- No hardware boot pin required (unlike ESP32's GPIO0 pull-down)

## SWD / JTAG

From XR872AT datasheet — not verified on this device:

- Secondary SWD: PB2 (SWDIO), PB3 (SWDCLK) — preferred over PB0/PB1 which are shared with UART0
- JTAG: PB0(TMS), PB1(TCK), PB2(TDO), PB3(TDI)
- 3.3V logic, standard ARM SWJDP — J-Link / DAPLink / OpenOCD should work

## Secure Boot

Almost certainly disabled. Consumer picture frame, no public evidence of signed firmware requirement. eFuse state unknown until device is accessed.

## Display

Flash partition layout is now confirmed — see [`hardware_facts.md`](hardware_facts.md).

- **Controller**: Unknown. The binary has no controller name string. Driver is SRAM-loaded (boot partition). Candidates: UC8159, UC8179, or proprietary Bigme/E Ink controller. The `check_busy_high` pattern (polling a BUSY pin high before sending commands) is consistent with E Ink ACeP controllers.
- **Pixel format**: Unknown. No image format strings (`jpeg`, `bmp`, `png`, `raw`) found anywhere in the 4 MB dump. The XR872AT has a hardware JPEG decoder (used for its camera interface) — likely used here so no software JPEG library is needed.
- **Inferred image format**: JPEG at 800×480. Hardware decode → 7-color quantisation → EPD SPI stream. This would explain why there are no JPEG library strings. **Not confirmed** — needs network traffic capture.
- **Refresh time**: ~20–30 seconds estimated for ACeP full refresh
- **Panel part number**: Likely GDEP073E01 or equivalent 7.3" Spectra 6 module — not confirmed
