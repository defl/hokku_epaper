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

Display SPI pins — confirmed from boot partition disassembly 2026-06-06 (moved to facts):
PA9=BUSY (input), PA19=MOSI/DC, PA21=SCLK, PA22=CS (active low).
Remaining unknown: which of PA8, PA13, PA15, PA16, PA17 is RST vs power-enable.

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

Controller, image format, and SPI pins are now fully confirmed — see [`hardware_facts.md`](hardware_facts.md) and [`display_driver.md`](display_driver.md).

Remaining unknowns:
- **Panel part number**: Likely GDEP073E01 or equivalent 7.3" Spectra 6 module — not confirmed
- **RST pin**: PA15 is set LOW during GPIO init (EPD_init_2) suggesting active-low reset, but not conclusively confirmed vs PA8/PA13/PA16/PA17
- **Image transfer**: Confirmed 192000 raw bytes sent over SPI. Whether the `pictureUrl` HTTP response is also raw 4bpp or JPEG (hardware-decoded first) is still unknown — needs traffic capture to confirm
