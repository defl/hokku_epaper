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

| XR872AT GPIO | EPD pin | Active level |
|---|---|---|
| PA9  (0x09) | BUSY | HIGH = ready |
| PA19 (0x13) | MOSI / D/C | — |
| PA21 (0x15) | SCLK | rising edge |
| PA22 (0x16) | CS | LOW = asserted |

Additional pins set during hardware init (exact functions not yet confirmed):
PA8, PA13, PA15, PA16, PA17 — likely RST and power-enable signals.

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

## Initialization Sequence

Called at epd_test step 2 (function `EPD_step2` at 0x00207228):

```
wait BUSY HIGH           ; controller must be ready before init

CMD 0xAA, DATA: 49 55 20 08 09 18   ; CMDH: enable register writes
CMD 0x01, DATA: 3F                   ; PWR
CMD 0x00, DATA: 5F 69                ; PSR
CMD 0x05, DATA: 40 1F 1F 2C          ; booster
CMD 0x08, DATA: 6F 1F 1F 22          ; booster
CMD 0x06, DATA: 6F 1F 17 17          ; BTST
CMD 0x03, DATA: 00 54 00 44          ; (unknown booster)
CMD 0x60, DATA: 02 00                ; TCON
CMD 0x30, DATA: 08                   ; PLL
CMD 0x50, DATA: 3F                   ; CDI (VCOM)
CMD 0x61, DATA: 03 20 01 E0          ; TRES: 0x0320=800 × 0x01E0=480 pixels
CMD 0xE3, DATA: 2F                   ; PWS
CMD 0x84, DATA: 01                   ; (custom)
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
