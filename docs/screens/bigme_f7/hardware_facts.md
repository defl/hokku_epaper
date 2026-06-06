# Bigme F7 — Hardware Facts

Only confirmed information lives here. Inferences, estimates, and TBD items belong in [`hardware_guesses.md`](hardware_guesses.md).

## Product Variants

All of the following share **identical PCB and hardware** (source: FCC equivalence letter 2025-08-21, signed by Bigme Cloud Literacy Technology Co., Ltd.):
F7, F7 Lite, F7 Plus, F7 Pro, F7 Max, F7 Ultra, F7 SE, F7+
Only the sales region differs.

FCC ID: **2A8EM-F7**
Manufacturer: Bigme Cloud Literacy Technology Co., Ltd. / xrztech.com

## SoC

- **Part**: XRADIOTECH XR872AT
- **Source**: Chip marking visible in FCC internal photo 12: `XRADIOTECH / XR872AT / N6135EA / 68F1`
- **Package**: QFN52, 6×6 mm (from datasheet)
- **CPU**: ARM Cortex-M4F (from datasheet)
- **Max clock**: 384 MHz (from datasheet)
- **SRAM**: 416 KB (from datasheet)
- **Boot ROM**: 160 KB (from datasheet)
- **WiFi**: 2.4 GHz 802.11b/g/n, integrated (from datasheet)
- **NOT an ESP32** — esptool does not work; see [`hardware_guesses.md`](hardware_guesses.md) for tooling
- **Crystal**: 40 MHz (confirmed from boot log: `HF clock 40000000 Hz`)
- **CPU clock in use**: 240 MHz (confirmed from boot log: `cpu clock 240000000 Hz`; max is 384 MHz per datasheet)
- **XIP**: enabled (confirmed from boot log: `XIP: enable`)
- **SRAM heap available**: 332,156 bytes at [0x21ca84, 0x26dc00) (confirmed from boot log)

## Firmware

Source: UART boot log captured 2026-06-06, full log at `.private/screens/bigme_f7/uart_log_20260606_104615.txt`

- **SDK**: XRADIO Skylark SDK 1.2.3
- **Build date**: Aug 7 2025 15:44:24
- **WLAN firmware version**: R-XR_C10.08.52.64_01.80 (built Jul 6 2019)
- **WLAN driver version**: XR_V02.06.28
- **Flash chip**: Zbit Semiconductor SPI NOR, **4 MB**, JEDEC ID `0x5E4016`
- **Flash dump**: `.private/screens/bigme_f7/flash_dump.bin` (4,194,304 bytes, `AWIH` magic, captured 2026-06-06)
- **BROM version**: 2 (confirmed via PhoenixMC flash ID dialog)
- **Dump procedure**: [`firmware_dump_procedure.md`](firmware_dump_procedure.md)
- **MAC address (efuse)**: 18:9e:2d:f9:87:54

## Antenna

PCB trace antenna, labeled "Antenna" with arrow in FCC internal photo 12.

## PCB

- **Shape**: L-shaped, wraps along bottom and one side of the display (FCC photos 10/11)
- **Display connector**: Wide FPC/ZIF connector at the bottom edge (FCC photos 10/11)
- **Button**: Single power button on the bottom of the frame (product description + FCC external photos)
- **USB-to-serial**: CH340 bridge (USB VID 1A86:7523), exposes XR872AT UART0 at 115200 baud (confirmed 2026-06-06)

## Battery

Source: label visible in FCC internal photo 9.

- **Model**: U255671P (Li-ion)
- **Rated capacity**: 1300 mAh
- **Rated energy**: 4.81 Wh
- **Nominal voltage**: 3.7 V
- **Charge voltage limit**: 4.2 V
- **Standard**: GB 31241-2022
- **Manufacturer**: Shenzhen Utility Energy Co., Ltd.
- **Date on unit**: 2025-05-26

## Display

Source: Bigme official product description and E Ink Spectra 6 panel specification.

- **Technology**: E Ink Spectra 6 (ACeP)
- **Size**: 7.3 inch
- **Resolution**: 800×480 pixels
- **PPI**: 127.8
- **Colors**: 7 (black, white, red, green, blue, yellow, orange)
- **No backlight**

## Key References

- XR872AT Datasheet v1.05: https://github.com/XradioTech/xradiotech.github.io/blob/master/docs/doc/XR872/XR872_Datasheet_V1.05.pdf
- Official XR872 SDK: https://github.com/XradioTech/xradio-skylark-sdk
- FCC filing: https://fccid.io/2A8EM-F7
