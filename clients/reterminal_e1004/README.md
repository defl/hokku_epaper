# Seeed reTerminal E1004 as a hokku screen

Arduino client that lets a [Seeed Studio reTerminal E1004](https://www.seeedstudio.com/reTerminal-E1004-p-6692.html)
(13.3" E Ink Spectra 6, 1200×1600, ESP32-S3) pull and display images from a hokku
server — same wire protocol, same server-side dithering pipeline as the stock
Hokku/Huessen frame. **No server changes required**: the device shows up in the
Connected Screens dashboard on its first request, battery percentage included.

Both frames use the same 13.3" Spectra 6 glass, but the E1004's panel (T133A01)
has a different controller wiring (single SPI bus, two chip-selects, one per
600-px half), so this is a standalone client sketch rather than a port of the
main firmware.

## Files

| file | what |
|---|---|
| `reterminal_e1004.ino` | the client: WiFi → GET `/hokku/screen/` → decode → display → deep sleep |
| `GxEPD2_T133A01_1200x1600.cpp/.h` | panel driver, vendored from [Seeed_GxEPD2](https://github.com/Seeed-Projects/Seeed_GxEPD2) `examples/GxEPD2_reTerminal_E1004/` (GPL-3.0, same as this repo) |

## Build

Arduino IDE or arduino-cli with the espressif `esp32` core (tested: 3.3.10) and
the **Adafruit GFX** + **[Seeed_GxEPD2](https://github.com/Seeed-Projects/Seeed_GxEPD2)** libraries.

Board settings (critical):

```
Board:  XIAO_ESP32S3
PSRAM:  OPI PSRAM        <- mandatory; the 4bpp framebuffer is ~937 KB
```

arduino-cli one-liners:

```sh
arduino-cli compile --fqbn "esp32:esp32:XIAO_ESP32S3:PSRAM=opi" --libraries <path-to-libraries> .
arduino-cli upload  -p <port> --fqbn "esp32:esp32:XIAO_ESP32S3:PSRAM=opi" .
```

Edit the config block at the top of the sketch first: WiFi credentials, server
IP/port, screen name. `DEEP_SLEEP 0` keeps the board awake between refreshes
(USB serial stays up — use while bring-up/debugging), `1` deep-sleeps for
`X-Sleep-Seconds` between fetches for battery deployment.

## Pinout (E1004, dual-chip T133A01)

Taken from Seeed's shipped example — note the wiki's pin table is wrong about
RST at the time of writing:

| signal | GPIO |
|---|---|
| SCK / MISO / MOSI | 7 / 8 / 9 |
| CS (chip 0, left half) | 10 |
| DC | 11 |
| CS1 (chip 1, right half) | 2 |
| RST | **38** (wiki says 12 — that's actually ENABLE) |
| BUSY | 13 |
| ENABLE | 12 |
| battery ADC / enable | 1 / 21 (2× divider; drive 21 HIGH to read) |

## How the panel data path works

hokku's `/hokku/screen/` returns exactly 960,000 bytes: two 600×1600 halves
back-to-back, row-major, two pixels per byte (high nibble = even column), with
the Spectra-6 nibble palette `0x0` black / `0x1` white / `0x2` yellow /
`0x3` red / `0x5` blue / `0x6` green — see `docs/` and
`webserver/hokku_server/display.py`.

That left/right split maps 1:1 onto the E1004's two controller chips, so the
sketch never touches individual pixels: it remaps each byte through a 256-entry
LUT and hands each half straight to the driver's `writeNative()`.

The LUT exists because of an encoding subtlety: the GxEPD2 driver's framebuffer
stores GxEPD2 color *indices* (0=black 1=white 2=green 3=blue 4=red 5=yellow)
and converts them to the panel's native nibbles only when shifting data out
over SPI. hokku's wire format is already panel-native — which happens to be the
identical palette — so the sketch applies the *inverse* of the driver's mapping
first and the two conversions cancel out. Colors and orientation then come out
correct with no rotation or mirroring.

## Protocol niceties implemented

- `X-Screen-Name` request header → auto-registers in the dashboard
- `X-Battery-mV` request header → battery % in the dashboard (ADC GPIO1,
  enable GPIO21, 2× divider — same circuit as the E1001/E1002)
- `X-Sleep-Seconds` response header honored for the deep-sleep interval,
  including on 404/503 "no image yet" responses
- Full refresh takes ~30 s (panel physics); complete wake→fetch→display cycle
  measured at ~30 s on a 2.4 GHz network

---

*Developed against hokku 3.1.0-alpha1 on a stock E1004 (ESP32-S3 rev v0.2,
8 MB PSRAM, 32 MB flash). Written up with the help of Claude (Anthropic) as a
pair-programming assistant.*
