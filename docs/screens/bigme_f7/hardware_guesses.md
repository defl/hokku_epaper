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
| PA12 | RED LED output | Boot partition disassembly 2026-06-07: 8-case switch at 0x002FF0 drives PA12 HIGH/LOW; init at 0x002FA0 sets output+LOW |
| PB3 | GREEN LED output (SWD_SWDCLK repurposed) | Same switch; driven alongside PA12; both active HIGH |
| PA20 | USB/charge detect input | Polled with compare-to-zero at 0x002CD0; HIGH = USB present (unconfirmed polarity) |
| PB2 | SWD_SWDIO (possibly repurposed) | SDK default (secondary SWD — preferred to avoid UART conflict) |
| PB4 | FLASH_MOSI | XR872AT hardware-fixed |
| PB5 | FLASH_MISO | XR872AT hardware-fixed |
| PB6 | FLASH_CS | XR872AT hardware-fixed |
| PB7 | FLASH_CLK | XR872AT hardware-fixed |

Display SPI pins — confirmed from boot partition disassembly 2026-06-06 (moved to facts):
PA9=BUSY (input), PA19=MOSI/DC, PA21=SCLK, PA22=CS (active low).
Remaining unknown: which of PA8, PA13, PA15, PA16, PA17 is RST vs power-enable.

## UART / Boot Protocol

From XR872AT SDK documentation unless noted:

- UART0 boot log baud: 115200
- **921600 baud NOT supported** on this device hardware — confirmed 2026-06-12; BROM operations work at 115200
- PhoenixMC flash tool claims 921600 — does not apply to this unit
- ~~ROM download mode trigger: send `"upgrade"` + 50×`'U'` over UART0 → `"OKBOOM"`~~ — **DISPROVEN
  2026-06-13.** Flooding `"upgrade"+0x55` at the bootloader during boot gets no response; the only
  working `upgrade` trigger is the firmware *console* command (see BROM/Recovery in
  [`hardware_facts.md`](hardware_facts.md)). Whether a boot-strap pin can force BROM is untested.
- ~~No hardware boot pin required~~ — unverified; with no software BROM entry left on a bricked
  unit, a boot-strap pad may be the only non-SPI-clip recovery. Needs PCB probing.

## Custom-firmware XIP crash (root cause TBD)

Our custom firmware crashes in `platform_init_level1` (MemManage IACCVIOL, PC=0x40000000) because
the XIP cache bias (`OPI_MEM_CTRL->BIAS_ADDR0`, see facts) is left at 0 instead of our app_xip
offset `0x13040`, so VMA 0x470534 maps to flash[0x70534] = wrong code. **Why the stock SDK path
leaves it 0 is not yet confirmed** — by static analysis `platform_xip_init` → `HAL_Xip_Init` should
program it correctly for our valid AWIH header chain. Leading hypothesis: we build with the generic
`xr872_evb_ai` board config (flash 96 MHz + EVB read mode) rather than Bigme's, giving a wrong XIP
read mode/clock for the Zbit part; alternatively `image_get_section_addr` fails before the console
UART is up (its error print would be lost — consistent with the silent-then-crash boot capture).
Current fix (`firmware_bigme_f7/main.c`, a `platform_init_level0` override that hardcodes the offset
and force-writes `BIAS_ADDR0`) is **unverified** — needs a SPI-clip flash+observe loop to confirm.

## SWD / JTAG

From XR872AT datasheet — not verified on this device:

- Secondary SWD: PB2 (SWDIO), PB3 (SWDCLK) — preferred over PB0/PB1 which are shared with UART0
- JTAG: PB0(TMS), PB1(TCK), PB2(TDO), PB3(TDI)
- 3.3V logic, standard ARM SWJDP — J-Link / DAPLink / OpenOCD should work

## LED and Charge Detection

Source: disassembly of `01_boot_payload.bin` (SRAM-loaded, OEM app code), 2026-06-12.

### LED pins

An 8-case switch-statement at boot payload offset 0x002FF0 drives exactly two GPIO pins: PA12 and PB3. The init function at 0x002FA0 configures both as outputs (driving level 2) and immediately drives them LOW.

- **PA12** (GPIOA pin 12) — RED LED, active HIGH
- **PB3** (GPIOB pin 3) — GREEN LED, active HIGH

PB3 is also the SDK-default SWD_SWDCLK pin (PB3 = SWDCLK). The OEM repurposes it as LED; SWD is presumably non-functional in production firmware.

**Color assignment** is inferred from the Huessen screen pattern (work LED = red). The hardware may have them swapped — confirm on first flash. Swapping is a one-line change in `led.c`.

### Charge detection

- **PA20** (GPIOA pin 20) — USB/charge detect input, polled at 0x002CD0 in a loop
- Logic: `HAL_GPIO_ReadPin(PORT_A, PIN_20)` result compared to zero; non-zero (HIGH) branches to a different code path — interpreted as USB present
- **Polarity unconfirmed**; if green/off is inverted after first flash, flip `GPIO_PIN_HIGH` → `GPIO_PIN_LOW` in `led_usb_present()` in `firmware_bigme_f7/led.c`
- The boot log shows `HAL_ADC_Init success!!` — ADC is likely used for battery voltage measurement, not for USB detect (USB detect appears to be a simple GPIO)
- The OEM firmware uses wakeup IO 2 (`CHARGE_WAKEUP_IO_PIN_DEF=2`) for charge-triggered wakeup from deep sleep; this likely maps to PA20 via `WakeIo_To_Gpio()`

## Secure Boot

Almost certainly disabled. Consumer picture frame, no public evidence of signed firmware requirement. eFuse state unknown until device is accessed.

## Display

Controller, image format, and SPI pins are now fully confirmed — see [`hardware_facts.md`](hardware_facts.md) and [`display_driver.md`](display_driver.md).

Remaining unknowns:
- **Panel part number**: Likely GDEP073E01 or equivalent 7.3" Spectra 6 module — not confirmed
- **RST pin**: PA15 is set LOW during GPIO init (EPD_init_2) suggesting active-low reset, but not conclusively confirmed vs PA8/PA13/PA16/PA17
- **Image transfer**: Confirmed from disassembly (2026-06-06). `pictureUrl` HTTP response is raw 4bpp (192000 bytes, no header). The device streams HTTP body bytes directly to EPD via SPI with no intermediate decode step. No JPEG decoder exists.
