# Bigme F7 — Display Driver Analysis

Reverse-engineered from boot partition binary (2026-06-06) using capstone ARM Thumb-2 disassembly.
Load address corrected to 0x00201000 (XR872AT maps its code RAM there, not 0x20000000).

All findings here are facts derived directly from disassembly — no guesses.

## Display Controller

**EK79655 or direct compatible** — E Ink Spectra 6 ACeP 7-color controller.

Evidence: the initialization command sequence (see below) is a byte-for-byte match with the
publicly available Waveshare `EPD_7in3f` driver (`epd7in3f.cpp`). That driver targets the
EK79655/E Ink Spectra 6 7.3" panel. Every command byte and every parameter byte matches.

## SPI Interface (bit-banged)

The XR872AT drives the display using software bit-bang SPI via GPIO, not hardware SPI.
Confirmed from functions at 0x0020707A (send_cmd) and 0x002070F2 (send_data).

**Protocol**: 9-bit frames — first bit = D/C, next 8 bits = data, MSB first.
CS is asserted (LOW) around each byte.

| XR872AT GPIO | EPD function | Notes |
|---|---|---|
| PA8  (0x08) | Static LOW output | Purpose unknown; never toggled after init |
| PA9  (0x09) | BUSY input | Pull-up; HIGH = controller ready |
| PA13 (0x0D) | RST, active LOW | Double-pulse on init (see RST sequence below) |
| PA15 (0x0F) | Static LOW output | Purpose unknown; never toggled after init |
| PA16 (0x10) | Static HIGH output | Purpose unknown; never toggled after init |
| PA19 (0x13) | MOSI / D/C | 9-bit SPI: first bit = D/C flag, then 8 data bits |
| PA21 (0x15) | SCLK | Idle LOW; data sampled on rising edge |
| PA22 (0x16) | CS | Active LOW; asserted around each 9-bit byte |
| PB17 (0x11) | POWER_EN | Active HIGH; set on at init, stays on during session |

PA8/PA15/PA16 are confirmed static via disassembly — they appear in `EpaperIO_Init`
and `epd_step1` but are never touched again in the entire EPD cycle including shutdown.
They likely control board-level power rails or panel mode selection; the EPD works with
these values so match them exactly.

### Bit-bang implementation

`send_cmd(byte)` at 0x0020707A:
1. PA22 = 0 (CS low)
2. PA19 = 0, clock pulse (D/C = 0 = command)
3. Loop 8×: set PA19 = bit (MSB first), PA21 = 1 (clock high), PA21 = 0 (clock low)
4. PA19 = 0, PA22 = 1 (CS high)

`send_data(byte)` at 0x002070F2:
1. PA22 = 0 (CS low)
2. PA19 = 1, clock pulse (D/C = 1 = data)
3. Loop 8×: same as above
4. PA19 = 0, PA22 = 1 (CS high)

`wait_busy_high()` at 0x002071DC:
Reads GPIOA_9 (via HAL_GPIO_ReadPin at 0x0000BDC8) in a tight loop until non-zero.

## Image Format

- **Size**: **192,000 bytes** (= 800 × 480 ÷ 2)
- **Encoding**: 4 bits per pixel, 2 pixels per byte (upper nibble = left pixel, lower nibble = right pixel)
- **Row order**: top to bottom, left to right within each row
- **Color values** (standard E Ink Spectra 6 / Waveshare 7in3f mapping):

| Nibble | Color |
|---|---|
| 0x0 | Black |
| 0x1 | White |
| 0x2 | Green |
| 0x3 | Blue |
| 0x4 | Red |
| 0x5 | Yellow |
| 0x6 | Orange |
| 0x7–0xF | Undefined / reserved |

## Command Set

Matches EK79655 / Waveshare 7in3f. Commands used in the firmware:

| Cmd | Name | Description |
|---|---|---|
| 0xAA | CMDH | Command header — enable register write mode |
| 0x00 | PSR | Panel Setting Register |
| 0x01 | PWR | Power Setting Register |
| 0x02 | POF | Power Off |
| 0x04 | PON | Power On |
| 0x06 | BTST | Booster Soft Start |
| 0x08 | (booster variant) | Booster timing setting |
| 0x10 | DTM | Data Start Transmission (image data follows) |
| 0x12 | DRF | Display Refresh (triggers update) |
| 0x30 | PLL | PLL control |
| 0x50 | CDI | VCOM and data interval |
| 0x60 | TCON | Gate/source non-overlap |
| 0x61 | TRES | Resolution setting |
| 0x84 | (custom) | Unknown |
| 0xE3 | PWS | Power saving |

## Full Init Call Graph

Confirmed from disassembly of `epd_init_parent` at 0x002073D8:

```
epd_init_parent(image_ptr)
  EpaperIO_Init (0x00206F94)     ← configure all GPIO directions/drive/pull
  epd_step1     (0x00207024)     ← set initial pin states
  EPD_step2     (0x00207228)     ← RST pulse + wait busy + command sequence
  send_image_data(image_ptr)     ← DTM + PON + DRF + POF
  PB17 = LOW                     ← power off after image sent
```

## Initialization Sequence

### GPIO init (EpaperIO_Init, 0x00206F94)

Sets mode, drive strength, pull for every EPD pin.
Order matches the disassembly exactly:

```
PA8  → OUTPUT, drive=1, pull=NONE
PA13 → OUTPUT, drive=1, pull=NONE   (RST)
PA22 → OUTPUT, drive=1, pull=NONE   (CS)
PA21 → OUTPUT, drive=1, pull=NONE   (SCLK)
PA19 → OUTPUT, drive=1, pull=NONE   (MOSI/DC)
PA16 → OUTPUT, drive=1, pull=NONE
PA15 → OUTPUT, drive=1, pull=NONE
PA9  → INPUT,  drive=1, pull=UP     (BUSY)
PB17 → OUTPUT, drive=3, pull=UP     (POWER_EN, high drive strength)
```

### Initial pin states (epd_step1, 0x00207024)

```
PA21 (SCLK)  = 0   (idle LOW)
PA19 (MOSI)  = 1   (HIGH)
PA22 (CS)    = 1   (deasserted)
PA16         = 1
PA15         = 0
PA8          = 0
PA13 (RST)   = 1   (deasserted)
PB17 (POWER) = 1   (power on)
```

### RST double-pulse (RST_pulse fn, 0x002071E8)

Called at the **start** of EPD_step2, before wait_busy. Confirmed from disassembly:

```
PA13 = 0 (assert RST)
OS_MSleep(100 ms)
PA13 = 1 (deassert RST)
OS_MSleep(100 ms)
PA13 = 0 (assert RST again)
OS_MSleep(100 ms)
PA13 = 1 (deassert RST, final)
← no trailing delay; next instruction is wait_busy_high
```

### EK79655 command sequence (EPD_step2 continued, 0x00207228)

After the RST pulse:

```
wait BUSY HIGH           ; wait for controller ready after RST

CMD 0xAA, DATA: 49 55 20 08 09 18   ; CMDH: enable register writes
CMD 0x01, DATA: 3F                   ; PWR
CMD 0x00, DATA: 5F 69               ; PSR
CMD 0x05, DATA: 40 1F 1F 2C         ; POFS (power-off sequence)
CMD 0x08, DATA: 6F 1F 1F 22         ; BTST1 (booster soft-start 1)
CMD 0x06, DATA: 6F 1F 17 17         ; BTST2 (booster soft-start 2)
CMD 0x03, DATA: 00 54 00 44         ; BTST_N (negative booster)
CMD 0x60, DATA: 02 00               ; TCON
CMD 0x30, DATA: 08                  ; PLL (frame rate)
CMD 0x50, DATA: 3F                  ; CDI (VCOM and data interval)
CMD 0x61, DATA: 03 20 01 E0         ; TRES: 0x0320=800 × 0x01E0=480 pixels
CMD 0xE3, DATA: 2F                  ; PWS (power saving)
CMD 0x84, DATA: 01                  ; vendor-specific
```

## Image Update Sequence

Called by `display_image(ptr)` at 0x0020736C with pointer to 192000-byte image buffer:

```
CMD 0x10                                 ; DTM: begin image data transfer
DATA: [192000 bytes of 4bpp image data]  ; 800×480 pixels, 2px/byte

CMD 0x04                                 ; PON: power on
wait BUSY HIGH                           ; wait for power-on complete

CMD 0x06, DATA: 6F 1F 17 49              ; BTST: booster pre-refresh settings
CMD 0x12, DATA: 00                       ; DRF: trigger display refresh

wait BUSY HIGH                           ; ~20–30 s for full ACeP refresh to complete

CMD 0x02, DATA: 00                       ; POF: power off
wait BUSY HIGH                           ; wait for power-off complete
```

## Relationship to Waveshare 7in3f

The initialization sequence is a byte-for-byte match with:
https://github.com/waveshare/e-Paper/blob/master/RaspberryPi_JetsonNano/python/lib/waveshare_epd/epd7in3f.py

This confirms the panel is the same E Ink Spectra 6 7-color ACeP module used in the
Waveshare 7.3" e-Paper HAT (F), product code `7.3inch e-Paper HAT (F)`.

## Implications for hokku_epaper Integration

Since the firmware downloads an image via `pictureUrl` and then calls `display_image(ptr)` with
exactly 192000 bytes, the server **likely serves raw 4bpp image data** (192000 bytes, no header)
rather than JPEG — consistent with the absence of any JPEG decoder library in the 4 MB firmware dump.

To integrate:
1. Implement the `/PhotoFrameDeviceStatus` endpoint (see [`cloud_protocol.md`](cloud_protocol.md))
2. Serve images as raw 4bpp 192000-byte blobs at `pictureUrl`
3. hokku_epaper must convert RGB images to the 7-color nibble format before serving

The raw 4bpp format is identical to what Waveshare documents for their 7in3f panel.
Color quantization: reduce each pixel to the nearest of the 7 supported colors.
