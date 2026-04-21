# Hokku/Huessen 13.3" E-Ink Frame open source firmware and image server

Open source firmware and image server for the Hokku / Huessen 13.3" six-color e-ink photo frame.

## Features

### Image management (web GUI at `http://server:port/`)

- **Drag-and-drop upload** anywhere on the page, or click the upload zone to browse. Multiple files at a time, with a per-file progress list and automatic rename on filename collision.
- **Per-image trash button** with a styled confirmation dialog (Esc cancels, Enter confirms). Removes the original *and* its cached dithered binary, preview PNG, and thumbnail.
- **Image grid shows every uploaded file immediately**, even ones still being converted. Pending entries display a yellow "Dithering…" badge and a faded thumbnail. Status bar shows `N / M ready` while a batch is in progress.
- **Per-image stats**: shown count, total display time (human-formatted: `2h 14m`, `3d 5h`), last-displayed timestamp.
- **"Show Next" button** on each image to force it next in the rotation.
- **Originals stay accessible**: each card links to the original (auto-converted to JPEG for HEIC/TIFF/etc.) and the dithered preview as the screen will see it.
- **Connected screens table**: every frame's name, IP, request count, last-seen timestamp, and next-scheduled update.
- **Configuration panel**: timezone with live server time, refresh schedule (multiple HHMM entries), orientation (landscape / portrait), poll interval. Changes saved back to disk.
- **"Clear Cache & Re-convert"** button when you want to re-dither everything (e.g. after changing orientation).
- **Supported formats**: JPEG, PNG, BMP, TIFF, WebP, GIF, HEIC/HEIF, AVIF.

### Server behaviour

- **Multi-screen support** — name each frame via the setup tool; the server tracks per-screen request counts and last-seen times.
- **Fair image rotation** — least-shown image is served next, with random tie-breaking. Newly-uploaded images jump to the front of the queue automatically.
- **Server-driven sleep schedule** — refresh times configured on the server (e.g. 06:00, 12:00, 18:00). The frame has no concept of time; the server tells it how long to sleep on each request.
- **Sleep-accuracy logging** — server stamps each response with `X-Server-Time-Epoch`; firmware compares actual vs expected sleep duration on the next wake and logs the error in seconds.
- **EXIF-aware** — phone photos appear right-side up, including in thumbnails.
- **Landscape or portrait** — pick your mounting orientation, the server rotates and re-dithers.
- **Spectra 6 dithering** — Floyd-Steinberg with *measured* palette values (not theoretical sRGB) plus dynamic range compression so the palette's actual L\* range is used efficiently. Significantly cleaner than the factory firmware.
- **Disk cache** — converted images are SHA-1 keyed; survives restarts, auto-pruned when source files change or are removed.
- **REST API** — every Web GUI action is also a JSON API call: `/hokku/api/{status, upload, image/<name>, show_next/<name>, original/<name>, thumbnail/<name>, dithered/<name>, config, clear_cache, time}`.
- **Debian packaging** with `systemd` service, `DynamicUser=yes` isolation. Or run from source on any Python 3.9+ host.

### Frame firmware (ESP32-S3 + UC8179C dual panel)

- **No cloud, no accounts** — everything runs on your local network.
- **Pre-built binaries** — ships as a `.bin`. No ESP-IDF toolchain needed for end users; the setup tool flashes everything over USB.
- **NVS-stored configuration** — WiFi SSID/password, server URL, screen name. Re-configurable via `hokku-setup` without rebuilding.
- **Reliable scheduled refreshes** — RTC slow-clock fallback for the rare cases where the ESP32-S3 misreports `esp_sleep_get_wakeup_cause()`, with a 26 h sanity guard against corrupt RTC state. Spurious-reset safety valve guarantees the chip is reflashable within ~3 minutes even in pathological reset loops.
- **60 s reflash + button window** after every displayed image. During this window the user can press the button to fetch the next image (resets the window) or trigger a USB reflash. Buttons GPIO 1 and GPIO 12 both wake from deep sleep AND fetch-while-awake.
- **Display error messages on screen** — config-version mismatch, missing config, download failure all render a readable explanation directly on the e-paper.
- **EXIF orientation applied** before display.
- **Battery-aware** — supports charging via USB. Charge LED blinks at 1 Hz throughout the entire awake window so "device is on and charging" is obvious.
- **Failure feedback over LED** — green LED triple-blinks rapidly if a manual fetch fails (so the button isn't mistaken for broken).

## Getting Started

You need two things:

1. **The image server** running on a computer on your network — serves and dithers your photos
2. **The firmware** flashed to the frame via USB — connects to WiFi and downloads images from the server

### 1. Install the image server

**Debian/Ubuntu** (recommended):
```bash
# Download the .deb from the latest release
apt install ./hokku-server_2.1.10-1_all.deb
# Starts automatically via systemd, web GUI at http://server:8080/
# Drop your photos into /var/lib/hokku/upload/ or install samba
# and make it very easy to manage them from any machine in your network
```

**Any platform** (from source):
```bash
cd webserver
pip install flask pillow numpy pillow-heif
python webserver.py
# Drop your photos into /images/upload/
# Web GUI at http://localhost:8080/
```

### 2. Flash and configure the frame

**Windows** (easiest — requires [Python 3](https://www.python.org/downloads/)):
```
hokku_setup.bat
```
Double-click or run from the command line. It installs dependencies automatically and walks you through WiFi, server address, and screen name.

**Any platform**:
```bash
cd tools
pip install pyserial esptool
python hokku_setup.py
```

The setup tool detects your frame over USB, flashes the firmware, and writes your WiFi credentials — no toolchain or compilation needed.

### How to Flash

To flash the firmware, take off the front cover of the frame (it's magnetically attached, be careful as it's easily damaged) and connect a USB-A to USB-C cable to the ESP32-S3 board's USB-C port.

Once connected, run `python hokku_setup.py` from the `tools/` directory (or `hokku_setup.bat` on Windows). The setup tool walks you through WiFi credentials, server address, and screen name — then flashes the firmware:

![Setup tool configuring a frame](images/configurator.png)

> **Note:** The frame has to be **awake** for the host to see it over USB-Serial/JTAG. ESP32-S3 disconnects USB during deep sleep, so a sleeping frame won't show up as a serial port. To flash an already-configured frame:
>
> 1. **Press the button on the back** to wake the frame.
> 2. You now have **~60 seconds** while the frame is in its post-display awake window to launch the flasher.
> 3. The setup tool / `esptool` will reset the chip into the ROM bootloader and take over.
>
> If you miss the 60 s window, the frame goes back to sleep and the USB device disappears — just press the button again. A freshly-powered frame (or one woken from deep sleep) starts in this window automatically.

### Image Server

The web GUI lets you manage your image library, configure refresh times, and monitor connected frames. Drop photos into the upload directory and the server automatically converts them to the 6-color e-ink palette:

![Web GUI showing image library and configuration](images/server.png)

For more details see:
- **[Image Server documentation](webserver/README.md)** — configuration, web GUI, API endpoints, color correction, systemd service
- **[Firmware documentation](firmware/README.md)** — building from source, manual flashing, developer notes

## Supported Image Formats

JPEG, PNG, BMP, TIFF, WebP, GIF, HEIC/HEIF, and AVIF. Drop any of these into the upload directory and the server auto-converts them.

## How It Works

**Server side:**
1. Images in the upload directory are converted to the 6-color Spectra palette using perceptual Lab color matching and Floyd-Steinberg dithering
2. When a frame requests an image, the server picks the least-shown one and serves it as a 960KB binary with an `X-Sleep-Seconds` header

**Frame side:**
1. Boot, connect to WiFi
2. Download image from server (one HTTP call gets image + sleep duration)
3. Display on the dual-panel e-ink screen (~19 second refresh)
4. Deep sleep until the server-specified time
5. On button press: wake and fetch next image

### Buttons

Press the right-hand button (in landscape) or lower button (in portrait) to wake the frame and fetch the next image. The button works both ways: it wakes the frame from deep sleep AND, while the frame is awake (the 60 s window after every image), pressing it fetches the next image and resets the window for another 60 s.

### LEDs

**Red LED** — solid while awake (including the 60 s post-display window), blinks at 1 Hz while charging, off once the chip enters real deep sleep.

**Green LED** — solid while WiFi is connected. Triple-blinks rapidly if you press the button to fetch a new image but the download fails (the current image is kept, the button is not broken).

## Background

I bought this frame in October 2025 from [Wayfair](https://www.wayfair.com/decor-pillows/pdp/hokku-designs-133-inch-wifi-epaper-art-photo-frame-w115006181.html) for about $280 — the cheapest Spectra 6 e-ink display I could find. The stock firmware didn't reliably update the image and was generally a pain to work with, so it was time to replace it. There's no public documentation on the hardware, so I had to do everything the hard way. Decided to made it an experiment in vibe coding something complex; the repo contains zero lines of human-written code. 

Claude Opus 4.6 was used throughout. Unfortunately, one cannot simply tell AI do build this firmware and hope it works, it takes a lot of pushing and prodding and domain knowledge for it to finally do what I needed it to do. AI proved excellent at analyzing the original firmware, but needed a lot of hand-holding when writing the hardware interface. My conclusion is that AI, at the time of building this, is a savant fruitfly with ADHD: absolutely blow me away amazing at some things, has no idea what it did a minute ago, plain stupid at times and overall way too eager to just _do_ things if you don't hold it in check all the time. Can't recomment a vibe-coding career in embedded software just quite yet :)
