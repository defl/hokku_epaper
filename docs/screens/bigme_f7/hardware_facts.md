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

## Antenna

PCB trace antenna, labeled "Antenna" with arrow in FCC internal photo 12.

## PCB

- **Shape**: L-shaped, wraps along bottom and one side of the display (FCC photos 10/11)
- **Display connector**: Wide FPC/ZIF connector at the bottom edge (FCC photos 10/11)
- **Button**: Single power button on the bottom of the frame (product description + FCC external photos)

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
