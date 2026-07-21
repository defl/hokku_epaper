# Hokku / Huessen 13.3" (`huessen_epf1301`)

The frame this project started with. A 13.3" six-colour E Ink Spectra 6 panel
(EL133UF1, 1200×1600) driven by an ESP32-S3, sold under both the **Hokku Designs**
and **Huessen** brands — identical hardware, different retailer.

**Status:** ✅ fully supported and the most thoroughly tested screen. Everything
Hokku does works here: OTA firmware updates, USB flashing from the web app,
battery reporting, deep sleep, diagnostics, and the full conversion pipeline.

## Getting one running

| | |
|---|---|
| **Buy** | [Retailers and prices](hardware.md) · [hardware overview](../../hardware.md) |
| **Install** | [Appliance image](../../appliance.md) (easiest) · [manual install](../../install.md) |
| **Use** | [User manual](../../manual.md) |

The stock firmware is replaced over USB during setup. After that first flash, all
future firmware updates go [over the air](../../manual.md) from the web app.

## Documentation

**Using it**
- [Hardware and where to buy](hardware.md) — retailers, prices, what to check
- [Image quality](image_quality.md) — how this panel renders, measured

**Developing**
- [Firmware source and build](../../../firmware/huessen_epf1301/README.md)
- [Firmware design spec](firmware_design.md) — the state machine the firmware implements
- [Hardware facts](hardware_facts.md) — confirmed GPIO map, SPI config, init sequence
- [Hardware guesses](hardware_guesses.md) — inferences and unknowns, kept separate on purpose

**Reverse engineering**

The vendor publishes nothing, so all of this was worked out from the stock
firmware:
- [Overview](reverse_engineering_overview.md)
- [v2.0.19 (April)](reverse_engineering_v2.0.19_apr21.md) ·
  [v2.0.26 (June)](reverse_engineering_v2.0.26_jun20.md)

## Panel at a glance

| | |
|---|---|
| Panel | E Ink Spectra 6 (EL133UF1), 13.3" |
| Resolution | 1200 × 1600 |
| Colours | Black, white, yellow, red, blue, green |
| SoC | ESP32-S3 |
| WiFi | 2.4 GHz only |
| Firmware version | see [`VERSION`](../../../firmware/huessen_epf1301/VERSION) |
