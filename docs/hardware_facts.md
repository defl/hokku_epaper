# Hokku 13.3" WiFi E-Paper Frame — Hardware Facts

## Platform
- **SoC**: ESP32-S3 (QFN56), revision v0.2
- **Crystal**: 40 MHz
- **Flash**: 16 MB, DIO mode, 80 MHz
- **PSRAM**: 8 MB Octal (AP vendor, generation 3, 3V, 80 MHz)
- **USB**: USB-Serial/JTAG on GPIO19/GPIO20 (must NEVER be reconfigured)
- **Original firmware**: ESP-IDF v5.2.2 (Apr 21 2025)

## Display
- **Panel**: EL133UF1 (E Ink Spectra 6), confirmed from ribbon cable label
- **Product page**: https://www.eink.com/product/detail/EL133UF1
- **Technology**: E Ink Spectra 6 (NOT ACeP) — 6 primary colors via microcup with 4 particle types
- **Controller**: UC8179C
- **Resolution**: 1600(H)×1200(V) total, dual-panel: each panel is 600 columns × 1600 rows
- **TRES register**: {0x04,0xB0,0x03,0x20} → 0x04B0=1200, 0x0320=800. This is the logical resolution the controller uses internally; the dual-gate panel hardware maps 1200×800 logical pixels to 600×1600 physical pixels (each logical row of 1200 feeds two physical rows of 600 via top/bottom gate drivers)
- **Pixel density**: 150 ppi
- **Active area**: 202.8mm × 270.4mm
- **Pixel format**: 4bpp (2 pixels per byte)
- **Panel size**: 480,000 bytes per panel (600 × 1600 / 2)
- **Physical display**: 13.3" dual-panel, landscape orientation (LED at bottom-right)
- **Panel arrangement (CONFIRMED)**: CTRL1 (GPIO18) = left panel (600×1600), CTRL2 (GPIO8) = right panel (600×1600)
- **Column offset**: the original firmware's embedded image had an 80-pixel circular column offset per panel. Our custom firmware sends data without any offset and the image displays correctly, confirming this was a server-side artifact in the original firmware, NOT a hardware/controller property.
- **Standard Spectra 6 nibble mapping (CONFIRMED)**: Black=0x0, White=0x1, Yellow=0x2, Red=0x3, Blue=0x5, Green=0x6
- **Nibble 0x4**: White/light on this panel (NOT orange despite some drivers suggesting it)
- **Upper nibbles (0x7-0xF)**: Produce various muted/intermediate colors (0x8=dark grey, 0x9=light blue, 0xA=yellow, 0xB=muted pink, 0xD=blue-purple, 0xE=olive green), but NOT standard/reliable
- **IMPORTANT**: nibble 0x0 is BLACK (standard Spectra 6). Previous empirical test incorrectly identified 0x0 as White due to rotation confusion.
- **DTM addressing**: both panels use DTM command 0x10; panel selection via CTRL pin (CTRL1 LOW or CTRL2 LOW)
- **Flash data bug**: embedded binary data at large offsets shows as noise — must use heap_caps_malloc(SPIRAM) + memcpy
- **Refresh time**: ~19 seconds during DRF command
- **BUSY pin pull-up**: GPIO7 MUST NOT have internal pull-up enabled — it masks the display controller's BUSY LOW signal, causing epaper_wait_busy() to return immediately and display never refreshes
- **BUSY pin has external pull-up on PCB**: The board has a physical pull-up resistor on GPIO7 (BUSY). When the display controller is powered off or disconnected, GPIO7 reads HIGH. Confirmed by driving GPIO7 LOW as output (reads 0), then releasing as input with internal pull-up disabled (reads 1 even with display power off). The display controller must actively pull BUSY LOW to overcome this external pull-up. If BUSY is stuck HIGH during display operations, the display flex cable is likely disconnected or the controller is not powered. Do NOT use gpio_reset_pin() on GPIO7 — it enables the internal pull-up which stacks with the external one, making it even harder for the controller to pull LOW.

## Image Preparation
- Full image is 1200×1600 (960K at 4bpp), split into two 480K panels
- First 480K → CTRL1 (left panel, 600×1600), second 480K → CTRL2 (right panel, 600×1600)
- Each panel's data is 600 pixels wide × 1600 rows, stored row-major, 2 pixels per byte (high nibble first)
- **Real-world dithering RGB values**: Black(25,30,33), White(232,232,232), Yellow(239,222,68), Red(178,19,24), Blue(33,87,186), Green(18,95,32)

## Original Firmware Default Image
- Location in flash: offset 0x310000 + 0x5AB4 (within data partition)
- Size: 960,000 bytes (480K per panel)
- Content: HUESSEN setup instructions (QR codes, app download steps)
- The two panel halves are nearly identical but differ by ~3400 pixels (dithering noise at color boundaries)
- Extracted images are kept under `.private/` (not in the repo; they're derivatives of the factory firmware binary)

## Confirmed GPIO Map

| GPIO | Function | Confidence | Notes |
|------|----------|------------|-------|
| 0 | SPI HW CS | CONFIRMED | Directly connected to SPI peripheral, NOT connected to display |
| 1 | BUTTON_1 | CONFIRMED | Active LOW, wakeup capable (RTC GPIO), "power off" in original FW |
| 2 | WORK_LED | CONFIRMED | Active HIGH |
| 3 | EPAPER_PWR_EN | LIKELY | Active HIGH — needs verification |
| 5 | BATT_ADC | CONFIRMED | ADC1_CH4, divider ratio 3.34:1 |
| 6 | EPAPER_RST | CONFIRMED | Active LOW (pull LOW to reset display) |
| 7 | EPAPER_BUSY | CONFIRMED | Active LOW (LOW = display busy) |
| 8 | CTRL2 | CONFIRMED | Display CS for right panel — held LOW during SPI |
| 9 | EPAPER_SCLK | CONFIRMED | SPI clock |
| 12 | PWR_BUTTON | CONFIRMED | Active LOW, wakeup capable (RTC GPIO). **Also tracks USB-host plug events** — see "USB Detection" below. Don't trust GPIO 12 LOW as a button press without checking GPIO 14 isn't transitioning at the same instant. |
| 17 | SYS_POWER | CONFIRMED | Active HIGH — controls main power rail |
| 18 | CTRL1 | CONFIRMED | Display CS for left panel — held LOW during SPI |
| 19 | USB D- | DO NOT TOUCH | USB-Serial/JTAG |
| 20 | USB D+ | DO NOT TOUCH | USB-Serial/JTAG |
| 4 | CHG_EN1 | LIKELY | Charger enable, active LOW |
| 13 | CHG_EN2 | LIKELY | Charger enable, active LOW |
| 14 | USB_HOST_DETECT | CONFIRMED | LOW = USB host (computer) connected. HIGH = no USB host. **NOT a pure VBUS-detect** — wall chargers / USB battery banks (no USB-data signaling) leave it HIGH even with VBUS present. Likely tied to charger IC's USB-BC (Battery Charging spec) "host detected" output. RTC-capable, usable as EXT1 wake source for "computer plug" events. (Was named CHG_STATUS pre-2026-04-19 — name was misleading; behavior is host-detect, not charge-active.) |
| 38 | WIFI_LED | CONFIRMED | Uses LEDC PWM for fade effects |
| 39 | BUTTON_3 | CONFIRMED | "restart wifi" in original FW, NOT RTC-capable |
| 40 | BUTTON_2 | CONFIRMED | "switch photo" in original FW, NOT RTC-capable, external pull-up on PCB |
| 41 | EPAPER_MOSI | CONFIRMED | SPI data out |

## SPI Configuration
- **Host**: SPI2_HOST
- **Mode**: 0 (CPOL=0, CPHA=0)
- **Clock**: 8 MHz
- **Flags**: SPI_DEVICE_3WIRE | SPI_DEVICE_HALFDUPLEX
- **command_bits**: 8
- **max_transfer_sz**: 4800 bytes
- **HW CS (GPIO0)**: toggles freely, not connected to display
- **Display CS**: CTRL1 (GPIO18) and CTRL2 (GPIO8) — held LOW during SPI transactions
- **No SPI_TRANS_CS_KEEP_ACTIVE** — CTRL pins provide CS, HW CS (GPIO0) toggles freely
- **Bulk transfer**: first chunk has flags=0 with cmd=0x10; continuation chunks use SPI_TRANS_VARIABLE_CMD (0x20) with command_bits=0

## Init Sequence (18 commands, verified from IROM disassembly at 0x4200B9D5)

**Group 1** (cmds 1–12): ctrl_low() (both CTRL1+CTRL2 LOW) before each
- 0x74: {C0,1C,1C,CC,CC,CC,15,15,55}
- 0xF0: {49,55,13,5D,05,10}
- 0x00 (PSR): {DF,69}
- 0x50 (CDI): {F7}
- 0x60 (TCON): {03,03}
- 0x86: {10}
- 0xE3: {22}
- 0xE0: {01}
- 0x61 (TRES): {04,B0,03,20} → 1200×800
- 0x01 (PWR): {0F,00,28,2C,28,38}
- 0xB6: {07}
- 0x06 (BTST): {E8,28}

**Group 2** (cmds 13–18): sent to CTRL1 then CTRL2 separately
- 0xB7: {01}
- 0x05: {E8,28}
- 0xB0: {01}
- 0xB1: {02}
- 0xA4: {83,00,02,00,00,00,00,00,00}
- 0x76: {00,00,00,00,04,00,00,00,83}

## Refresh Sequence
- PON (0x04) → DRF (0x12, {0x00}) with 30ms pre-delay → POF (0x02, {0x00})
- CTRL pins must be released (HIGH) AFTER sending refresh commands but BEFORE waiting for BUSY
- BUSY asserts after PON and DRF (normal behavior)
- BUSY does NOT assert during hardware reset (RST=0)

## Power Architecture
- **SYS_POWER (GPIO17)**: controls main power rail, must stay HIGH for system to run
- **EPAPER_PWR_EN (GPIO3)**: likely controls display power supply
- **Battery**: Li-ion, monitored via ADC on GPIO5. **Battery is REQUIRED for display** — display controller is powered from battery rail, not USB
- **Battery ADC**: ADC1_CH4, ADC_ATTEN_DB_6, voltage divider ratio = **3.34** (calibrated: ADC reads ~1230mV at pin when battery is 4.1V)
- **USB**: provides 5V power, may not be sufficient for display refresh without battery
- **Brownout risk**: gpio_reset_pin() on SYS_POWER briefly cuts power — must handle SYS_POWER separately in init, set HIGH immediately
- **RTC GPIO isolation**: persists across chip resets (not power-on resets). Must call rtc_gpio_hold_dis() + rtc_gpio_deinit() before gpio_reset_pin()

## USB Detection (verified empirically 2026-04-19)

**Test method:** custom probe firmware in `firmware_probe/` watched every safe
GPIO (0-18 except 19/20, 21, 38-48), all 10 ADC1 channels (DB_12 attenuation,
calibrated mV), and the USB Serial/JTAG `FRAM_NUM`/`INT_RAW` registers, all
sampled every 100 ms with a ring buffer that survives USB unplug because the
chip stays powered by battery. Sequence tested: computer USB → unplug → wall
charger (high-power laptop charger) → unplug → battery → replug computer.

### What's reliably detectable

- **Computer USB plugged in / unplugged**:
  - `gpio_get_level(14) == 0` ↔ host present, `== 1` ↔ host absent
  - Equivalent secondary signals: `USB_SERIAL_JTAG_FRAM_NUM_REG` (0x60038024) increments
    by 1 per ms when host is enumerated and frozen otherwise; `INT_RAW.SOF` (bit 1 of
    0x60038008) is set when SOFs are arriving.
  - Edge transitions are clean and fast (single 100 ms sample shows the change).

### What's NOT detectable

- **Wall-charger / USB-power-only plug-in**: zero transitions on any of the
  29 safe GPIOs, zero changes on any of the 10 ADC1 channels, zero change in
  `FRAM_NUM` (no SOF traffic). The board has no firmware-readable signal that
  reflects "VBUS present without USB data signaling".
- Tested with high-power laptop USB-C charger that we know provides VBUS to
  similar devices. No measurable response from the firmware's perspective.
- This means: **firmware cannot distinguish wall-charger-plugged-in from
  battery-only operation.** Both look the same.

### Why GPIO 14 behaves this way (best inference, not schematic-confirmed)

GPIO 14 is wired to a charger-IC pin that asserts only after USB-BC (Battery
Charging Specification 1.2) detection determines the source is a Standard
Downstream Port (SDP) or similar data-capable host. Pure-power sources
(Dedicated Charging Port / wall warts / battery banks) don't trigger BC
detection because there's no data signaling on D+/D-, so the pin stays
de-asserted (HIGH).

This is *more* useful than pure VBUS-detect for our use case, because:
- A user only ever needs the device "awake on USB" when there's a host they
  could be reflashing/monitoring from. A wall charger has no such use case.
- On wall charger, normal battery-mode behaviour (deep sleep, refresh on
  schedule, no LED, no logging) is exactly what we want — the charger keeps
  the battery topped up in the background regardless.

### GPIO 12 + GPIO 14 are paired, not independent

Tests show GPIO 12 (PWR_BUTTON) transitions *simultaneously* with GPIO 14 on
USB-host plug events, even when no button is physically pressed. The two
signals appear to share an electrical path, or both respond to the same
charger-IC output. This matches the factory firmware's pattern (`battery_task`
polls both pins together via `gpio_get_level(14)` + `gpio_get_level(12)` and
treats them as a paired event signal, applying a 6-sample debounce).

Consequence for the current firmware's design choices is documented in
[`firmware_design.md`](firmware_design.md) (USB detection + GPIO 12 section).

## Deep Sleep & Wakeup
- **RTC-capable GPIOs**: only GPIO0–21 on ESP32-S3. GPIO39 / GPIO40 cannot wake from deep sleep.
- **Original FW ext1 bitmask**: 0x1002 = GPIO1 | GPIO12 (factory firmware predates our USB-detect-on-14 finding).
- **USB-Serial/JTAG disconnects during deep sleep**: this causes the USB host to reset the chip, which appears as a fresh boot (wakeup cause UNDEFINED). Firmware detects this via an RTC memory flag to avoid re-displaying / looping — see [`firmware_design.md`](firmware_design.md).
- **Target sleep current**: ~8µA with RTC GPIO isolation.

## Boot Hazards
- **CTRL pins LOW at boot**: gpio_config() defaults output to 0 (LOW = CS active). Display sees garbage during SPI bus init. Must set CTRL HIGH before spi_bus_initialize()
- **SPI DMA vs flash memory**: ESP32-S3 SPI DMA cannot read flash-mapped memory — must memcpy to RAM buffer first

## SPI Flags (verified identical in ESP-IDF v5.2.2 and v5.5.3)
- SPI_DEVICE_3WIRE = (1<<2) = 0x04
- SPI_DEVICE_HALFDUPLEX = (1<<4) = 0x10
- Combined device flags = 0x14
- SPI_TRANS_VARIABLE_CMD = (1<<5) = 0x20
- SPI_TRANS_VARIABLE_ADDR = (1<<6) = 0x40
- SPI_TRANS_CS_KEEP_ACTIVE = (1<<8) = 0x100

## Key Addresses (Original FW IROM.bin, base 0x42000020)
- init_sequence: 0x4200B9D5
- bulk_transfer: 0x4200BE08 (chunked 4800B, struct1@0x3fca2230/struct2@0x3fca2200)
- dtm_function: 0x4200BD90 (CTRL1-only LOW → bulk_transfer(0x10))
- display_refresh: 0x4200BB9F (PON/DRF/POF)
- helper_spi_cmd: 0x4200BEC8
- helper_spi_bulk: 0x4200BDE0
- spi_device_polling_transmit: 0x420554E0

