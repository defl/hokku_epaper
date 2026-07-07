# Bigme F7 — Reverse Engineering Overview

See [`hardware_facts.md`](hardware_facts.md) for the hardware reference.

> **This is the early RE overview.** The F7 is now fully brought up on custom Hokku
> firmware. For the finished work see:
> [`custom_firmware.md`](custom_firmware.md) (feature parity + boot/rollback),
> [`ota.md`](ota.md) (A/B OTA design, safety, adversarial findings),
> [`firmware_build.md`](firmware_build.md) (build),
> [`display_driver.md`](display_driver.md) (EK79655 / Spectra-6),
> [`restore_to_stock.md`](restore_to_stock.md) (surgical OEM restore).

## Current Status

- [x] SoC identified: XRADIOTECH XR872AT (from FCC internal photo)
- [x] Display identified: E Ink Spectra 6 ACeP, 7.3", 800×480 (EK79655)
- [x] Battery identified: U255671P 1300mAh; voltage sense = ADC ch4/PA14
- [x] PCB photographed: FCC filing internal photos (L-shaped board)
- [x] Toolchain: PhoenixMC (UART), pure-Python `xr872_flasher` (BROM), CH341A (SPI)
- [x] Firmware dumped (PhoenixMC + pure-Python BROM dump, 4 MB)
- [x] Boot log captured; display controller identified + driven
- [x] Image format / pixel encoding confirmed (Spectra-6 nibble map)
- [x] **Custom firmware: reporting, config, deep-sleep, battery, A/B OTA** — all
      verified on hardware (see `custom_firmware.md`, `ota.md`)

## Firmware Extraction Plan

### Option A: Direct SPI Flash Read (Recommended First Attempt)

The XR872AT uses an external QSPI NOR flash chip — it is a separate, physically accessible component on the PCB. This chip is almost certainly unencrypted (standard consumer IoT practice).

1. Open the device (unscrew back, pry plastic back panel)
2. Locate the small SOP-8 or WSON-8 flash chip near the XR872AT (GD25Qxx or W25Qxx family)
3. Clip with a SOIC-8 test clip **without desoldering**
4. Connect to CH341A programmer (cheap ~$5 USB SPI programmer)
5. Read with: `flashrom -p ch341a_spi -r bigme_f7_flash.bin`
6. Verify size matches (likely 2 MB = 0x200000 bytes)

**Advantage**: No boot mode needed, no UART interaction, non-destructive.

### Option B: UART PhoenixMC Dump

1. Open device, locate UART0 test pads (PB0=TX, PB1=RX on XR872AT)
2. Connect USB-TTL adapter (3.3V): adapter-RX → PB0, adapter-TX → PB1, GND → GND
3. Power on device, watch for boot log at **115200 baud** — confirms chip identity
4. Use PhoenixMC tool at **921600 baud** to read full flash:
   - `iDebugOp = r`, address `0x0`, length = full flash size
5. Alternatively send `"upgrade"` + 50×`'U'` over UART to trigger ROM download mode manually

**Tool**: https://github.com/openshwprojects/FlashTools/tree/main/XRadioTech-AllWinner

### Option C: SWD Memory Read

1. Attach J-Link or DAPLink to PB2 (SWDIO) and PB3 (SWDCLK) on XR872AT
2. Use OpenOCD with generic Cortex-M4 target config
3. Dump flash-mapped memory region
4. Also useful for live RAM inspection and debugging

## Firmware Analysis Plan

Once flash is dumped:

1. **Identify flash chip size** from the binary (check for 0xFF padding at the end)
2. **Parse image layout**: bootloader at offset 0, app at offset 32 KB, look for `AWIH` magic
3. **Run binwalk**: `binwalk bigme_f7_flash.bin` — will find compressed sections and WLAN firmware
4. **Extract app section**: `dd if=bigme_f7_flash.bin bs=1 skip=32768 count=<size> of=app.bin`
5. **Disassemble**: Load `app.bin` into Ghidra with ARM Cortex-M4 profile, load address 0x00200000 (RAM XIP region per XR872AT memory map)
6. **Find display driver**: Search for SPI transaction patterns, look for large byte arrays (LUT tables), find display init/refresh sequences
7. **Map GPIO usage**: Cross-reference against XR872AT HAL GPIO headers from the SDK

## Display Driver Investigation

The display controller is unknown — likely one of:
- **IT8951** (E Ink's common host controller for ACeP panels)
- **UC8179** (used in the EPF1301, but that was Spectra 6 not ACeP — possible overlap)
- A custom/integrated controller in the panel module itself

Key things to find in the firmware:
- SPI init sequence (clock speed, mode, CS pin)
- Display resolution registers (should match 800×480)
- Pixel data format (bits per pixel, byte packing)
- Color nibble → ink mapping (compare to EPF1301: Black=0x0, White=0x1, Yellow=0x2, Red=0x3, Blue=0x5, Green=0x6)
- Refresh command sequence (PON → DRF → POF or equivalent)

## Reference: XR872AT Memory Map

From datasheet (use for Ghidra load addresses):

| Region | Address | Size | Notes |
|--------|---------|------|-------|
| Boot ROM | 0x00000000 | 160 KB | Factory ROM |
| SRAM | 0x00200000 | 416 KB | Main RAM |
| Flash (XIP) | 0x10000000 | up to 16 MB | Execute-in-place from QSPI |
| Peripherals | 0x40000000 | — | MMIO registers |

App binary likely loads to SRAM at 0x00200000 and executes from flash XIP at 0x10000000+.

## Files

Private artifacts go in `.private/`:
- Raw flash dump: `flash_dump_<date>.bin`
- Extracted sections: `extracted/bootloader.bin`, `extracted/app.bin`, `extracted/wlan_fw.bin`
- Boot log capture: `uart_boot_log_<date>.txt`
- Analysis scripts: `analyze_*.py`
- Ghidra project: `ghidra_proj/`
