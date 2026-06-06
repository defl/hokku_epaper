# Bigme F7 — Hardware Guesses

Inferences, estimates, SDK-derived defaults, and unknowns. Nothing here is confirmed on the actual device.
Confirmed findings get moved to [`hardware_facts.md`](hardware_facts.md).

## Platform

- **OS**: Bare-metal RTOS, likely XR Skylark SDK (FreeRTOS-based) — inferred from SoC identity; no Android/Linux on Cortex-M4
- **External flash**: QSPI NOR flash, separate SOP-8 or WSON-8 chip near the XR872AT — required by XR872AT architecture (no internal flash)
  - Size: likely 2–4 MB; exact part unknown (GD25Qxx or W25Qxx family typical for this SoC)
  - Encryption: likely absent — no known consumer XR872AT product ships with flash encryption
- **USB port**: confirmed present with CH340 USB-to-serial (VID 1A86:7523, COM6); exposes UART0 at 115200 baud — confirmed working for boot log capture. Whether it's usable for PhoenixMC firmware dump at 921600 baud is untested.

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

## Flash Layout

From XR872AT SDK `image.cfg` — not verified against this device's flash dump:

| Offset | Expected Content |
|--------|-----------------|
| 0 KB | Stage-1 bootloader, magic `0xa5ff5a00` |
| 32 KB | Application image, `AWIH` header / magic `0xa5fe5a01` |
| ~980 KB | WLAN bootloader |
| ~985 KB | WLAN firmware |
| ~1017 KB | WLAN SDD config |
| ~1024 KB | OTA partition |

## Display

- **Controller**: Unknown — to be determined from firmware analysis. Candidates: IT8951, UC8179, or custom
- **Pixel format**: Unknown — likely 4bpp but 7-color nibble mapping differs from EPF1301's 6-color Spectra 6
- **Refresh time**: ~20–30 seconds estimated for ACeP
- **Panel part number**: Likely GDEP073E01 or equivalent 7.3" Spectra 6 module — not confirmed
