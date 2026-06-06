# Bigme F7 — Hardware Facts

**Status: Pre-firmware-dump. All facts here are from FCC filing + chip research, not empirical measurement.**

## Product Variants

All of the following share **identical PCB and hardware** (confirmed by FCC equivalence letter 2025-08-21):
F7, F7 Lite, F7 Plus, F7 Pro, F7 Max, F7 Ultra, F7 SE, F7+
Only the sales region differs.

FCC ID: **2A8EM-F7**  
Manufacturer: Bigme Cloud Literacy Technology Co., Ltd. / xrztech.com

## Platform

- **SoC**: XRADIOTECH XR872AT (QFN52, 6×6 mm)
  - Chip markings on PCB: `XRADIOTECH / XR872AT / N6135EA / 68F1`
  - ARM Cortex-M4F @ up to 384 MHz
  - 416 KB SRAM, 160 KB Boot ROM
  - Integrated 2.4 GHz 802.11b/g/n WiFi
  - **NOT an ESP32** — requires different tooling (PhoenixMC, not esptool)
- **OS**: Bare-metal RTOS (XR Skylark SDK / FreeRTOS-based). No Android, no Linux.
- **External Flash**: QSPI NOR flash (separate SOP-8/WSON-8 chip near XR872AT)
  - Likely 2–4 MB (GD25Qxx or W25Qxx family); exact part TBD from physical inspection
  - Flash is NOT encrypted in known consumer products using this chip
- **Crystal**: On-board crystal (visible in PCB photo, exact frequency TBD — likely 40 MHz or 24 MHz)
- **Antenna**: PCB trace antenna (labeled in FCC photo 12)

## Display

- **Panel**: E Ink Spectra 6 (ACeP), 7.3 inch
- **Resolution**: 800×480 pixels, 127.8 PPI
- **Colors**: 7-color: black, white, red, green, blue, yellow, orange
- **Pixel format**: TBD from firmware analysis (likely 4bpp like EPF1301 but 7-color mapping differs)
- **Controller**: TBD — connected via FPC ribbon cable to bottom of PCB
- **Refresh time**: ~20–30 seconds (typical for ACeP/Spectra 6)
- **Panel part**: Likely GDEP073E01 or equivalent 7.3" Spectra 6 module

## Battery

- **Model**: U255671P (Li-ion)
- **Capacity**: 1300 mAh rated, 4.81 Wh
- **Nominal voltage**: 3.7 V
- **Charge voltage**: 4.2 V
- **Standard**: GB 31241-2022
- **Manufacturer**: Shenzhen Utility Energy Co., Ltd. (date on unit: 2025-05-26)

## PCB

- **Shape**: L-shaped, wraps around bottom and one side of the display
- **Connector**: Wide FPC/ZIF connector for e-ink display (visible in PCB photos 10/11)
- **Button**: Single power button on the bottom of the frame
- **USB**: USB port type TBD (charging + likely UART access)
- **LED**: TBD

## GPIO Map

**Unknown — requires physical probing.** Expected from XR872AT SDK defaults:

| GPIO | Expected Function | Confidence | Notes |
|------|------------------|------------|-------|
| PB0 | UART0_TX | HIGH | Primary UART boot/console |
| PB1 | UART0_RX | HIGH | Primary UART boot/console |
| PB2 | SWD_SWDIO | HIGH | Secondary SWD (preferred — avoids UART conflict) |
| PB3 | SWD_SWDCLK | HIGH | Secondary SWD |
| PB4 | FLASH_MOSI | HIGH | External QSPI NOR flash |
| PB5 | FLASH_MISO | HIGH | External QSPI NOR flash |
| PB6 | FLASH_CS | HIGH | External QSPI NOR flash |
| PB7 | FLASH_CLK | HIGH | External QSPI NOR flash |

Display SPI, power enable, BUSY, RST pins: all TBD from firmware analysis.

## UART / Debug Interface

- **UART0**: PB0 (TX), PB1 (RX) — 3.3V logic
- **Boot log baud**: 115200 baud (initial ROM messages)
- **PhoenixMC baud**: 921600 baud (firmware download protocol)
- **Boot trigger**: ROM monitors UART0 for `"upgrade"` + 50×`'U'` → responds `"OKBOOM"` → download mode. No hardware boot pin needed (unlike ESP32's GPIO0).
- **Test pads**: Location on PCB TBD — need to probe with multimeter when device is open

## SWD / JTAG

- **Interface**: ARM SWJDP (SWD + JTAG)
- **Preferred SWD pins**: PB2 (SWDIO), PB3 (SWDCLK) — avoids conflict with UART0 on PB0/PB1
- **JTAG**: PB0(TMS), PB1(TCK), PB2(TDO), PB3(TDI)
- **Compatible**: J-Link, CMSIS-DAP/DAPLink, OpenOCD (generic Cortex-M4 target)
- **Voltage**: 3.3V

## Secure Boot

Almost certainly **disabled** — consumer IoT picture frame. No evidence of signed firmware requirement. Exact eFuse state TBD from hardware access.

## Flash Layout (Expected)

From XR872AT SDK `image.cfg` — not yet verified against this device:

| Offset | Content |
|--------|---------|
| 0 KB | Stage-1 bootloader (`boot_24M.bin` or `boot_40M.bin`), magic `0xa5ff5a00` |
| 32 KB | Application image (`app.bin`), magic `0xa5fe5a01` / `AWIH` header |
| ~980 KB | WLAN bootloader |
| ~985 KB | WLAN firmware |
| ~1017 KB | WLAN SDD config |
| ~1024 KB | OTA partition |

## Key References

- XR872AT Datasheet v1.05: https://github.com/XradioTech/xradiotech.github.io/blob/master/docs/doc/XR872/XR872_Datasheet_V1.05.pdf
- Official SDK (xradio-skylark-sdk): https://github.com/XradioTech/xradio-skylark-sdk
- PhoenixMC flash tool (open-source fork): https://github.com/openshwprojects/FlashTools/tree/main/XRadioTech-AllWinner
- Elektroda A9 camera RE thread (same chip family): https://www.elektroda.com/rtvforum/topic4074636.html
- FCC filing: https://fccid.io/2A8EM-F7
