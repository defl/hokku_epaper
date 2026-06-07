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
Binary analysis of flash dump performed 2026-06-06; partition files in `.private/screens/bigme_f7/partitions/`.

- **SDK**: XRADIO Skylark SDK 1.2.3
- **App firmware version**: **1.2.7** (string literal `1.2.7` at boot payload offset 0x84B1)
- **Build date**: Aug 7 2025 15:44:24
- **WLAN firmware version**: R-XR_C10.08.52.64_01.80 (built Jul 6 2019)
- **WLAN driver version**: XR_V02.06.28
- **Flash chip**: Zbit Semiconductor SPI NOR, **4 MB**, JEDEC ID `0x5E4016`
- **Flash dump**: `.private/screens/bigme_f7/flash_dump.bin` (4,194,304 bytes, `AWIH` magic, captured 2026-06-06)
- **BROM version**: 2 (confirmed via PhoenixMC flash ID dialog)
- **Dump procedure**: [`firmware_dump_procedure.md`](firmware_dump_procedure.md)
- **MAC address (efuse)**: 18:9e:2d:f9:87:54

## Flash Partition Layout

Confirmed from AWIH header analysis of flash dump. Each partition image has its own 64-byte AWIH header.

| Flash offset | Size | Name | Load | Payload size |
|---|---|---|---|---|
| 0x000000 | 32 KB | Main image table | — | 10 KB (partition table) |
| 0x008000 | 128 KB | Boot / app | SRAM 0x00201000, EP 0x00201100 | 48 KB |
| 0x028000 | 819 KB | App XIP | XIP (runs from flash) | 757 KB |
| 0x0F4C00 | 4 KB | WLAN bootloader | XIP | 2 KB |
| 0x0F5C00 | 35 KB | WLAN firmware | XIP | 34 KB |
| 0x0FE800 | ~3 MB | WLAN SDD data | — | 1 KB |

The EPD display driver lives in the **Boot partition** (SRAM-loaded). Confirmed EPD functions found:
`check_busy_high`, `epd_test` (with sub-steps 1, 11, 2, 3).

## Cloud Connectivity

Source: string literals extracted from boot partition binary, 2026-06-06. Full protocol in [`cloud_protocol.md`](cloud_protocol.md).

- **Cloud server**: `http://ereader.bigme.vip:8086`
- **MQTT broker**: `120.76.40.178:1883`
- **MQTT credentials**: user `mqt_user`, password `xrz86112763`
- **MQTT topics**: `iot/device/<deviceId>`, `iot/device/willTopic`, `iot/device/sever_topic`
- **Device ID format**: `BIGME_<MAC>` (e.g. `BIGME_189E2DF98754`)
- **Setup AP SSID**: `BigmeFrameRouter`, password `88888888`
- **Operational AP SSID**: `XRZ_<MAC>` (used during WiFi provisioning)
- **Default timezone**: UTC+8 (`TZ=GMT-8` is the POSIX convention for Asia/Shanghai)

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

Source: product description + E Ink Spectra 6 spec + disassembly of boot partition (2026-06-06).
Full driver analysis: [`display_driver.md`](display_driver.md)

- **Technology**: E Ink Spectra 6 (ACeP)
- **Size**: 7.3 inch
- **Resolution**: 800×480 pixels (confirmed by TRES command 0x61 params 0x0320, 0x01E0 in firmware)
- **PPI**: 127.8
- **Colors**: 7 (black, white, red, green, blue, yellow, orange)
- **No backlight**
- **Controller**: EK79655 or direct compatible — initialization sequence is byte-for-byte identical to
  Waveshare EPD_7in3f open source driver; confirmed from boot partition disassembly
- **Image format**: 192,000 bytes raw, 4 bits per pixel, 2 pixels per byte (upper nibble = left pixel)
  Color encoding: 0=Black, 1=White, 2=Green, 3=Blue, 4=Red, 5=Yellow, 6=Orange
- **SPI interface**: bit-banged, 9-bit frames (1 D/C bit + 8 data bits), MSB first
  PA19=MOSI/DC, PA21=SCLK, PA22=CS (active low), PA9=BUSY input (HIGH = ready)
- **Refresh time**: ~20–30 seconds for full ACeP refresh

## SDK Ecosystem

The device firmware was built with **XRADIO Skylark SDK 1.2.3** (Aug 7 2025). Research conducted 2026-06-06 found no public availability of this version anywhere — making it the newest confirmed SDK version and suggesting the manufacturer has a private/NDA channel with Allwinner.

Publicly available SDK versions (for reference):

| Version | Source | Notes |
|---|---|---|
| 1.1.1 | github.com/XradioTech/xradio-skylark-sdk | Last official public release (Jun 2020, abandoned) |
| 1.2.0 | github.com/divadiow/xr872_sdk | Community fork; also seen in device boot logs (Apr 2023 / Jun 2024 builds) |
| 1.2.2 | github.com/divadiow/xr872_sdk | Community fork ceiling (Feb 2023); referenced as "test build" in Elektroda threads |
| **1.2.3** | **This device only** | **No public source found; built Aug 7 2025** |

Allwinner (who acquired XRadioTech) does not publicly host an XR872 SDK. Their official developer platform (aw-ol.com / docs.aw-ol.com) lists XR806 but not XR872. The community fork at github.com/divadiow/xr872_sdk (active through May 2025) is the best public alternative.

## Key References

- XR872AT Datasheet v1.05: https://github.com/XradioTech/xradiotech.github.io/blob/master/docs/doc/XR872/XR872_Datasheet_V1.05.pdf
- Official XR872 SDK (abandoned at 1.1.1): https://github.com/XradioTech/xradio-skylark-sdk
- Community SDK fork (1.2.2, active): https://github.com/divadiow/xr872_sdk
- Allwinner developer platform: https://www.aw-ol.com/
- FCC filing: https://fccid.io/2A8EM-F7
