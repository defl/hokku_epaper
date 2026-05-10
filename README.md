![Logo](images/logo/logo_alt_white.png)

This project offers open source firmware for the Hokku / Huessen 13.3" six-colour e-ink photo frame plus  a self-hosted image server. It's better in every imaginable way - from color correctness to full privacy. It installs with just a cable and you can run the server on basically any hardware.

## Core features

**Photos, your way**
- **Local-only.** Your photos never leave your network. No cloud, no third-party servers, no telemetry. Your hardware and open source software means you're in full control. 
- **Drag-and-drop upload** straight into the web app — single files or dozens at a time, with a live progress list. Works on phones too.
- **Browse in a grid**, preview originals *and* the dithered version side-by-side (so you see exactly what the frame will show before it shows it), delete anything you don't want with a one-click trash button.
- **All the formats you actually have:** JPEG, PNG, HEIC/HEIF, AVIF, WebP, GIF, TIFF, BMP. It takes anything from 90's formats to modern day iPhone and Android images. Phone photos auto-rotate thanks to EXIF.
- **Landscape or portrait** — flip a switch, the server re-dithers everything to match the mounting.
- **"Show next" on any image** when you want to force a specific photo onto the frame at the next refresh.

**Looks good on e-paper**
- **Correct out of the box**, comes pre-set for optimal results.
- **Context aware**, the software knows if images are black and white or have faces in them (model runs locally) and it can adapt the dithering algorithm to deliver optimal results.
- **Extremely configurable**, 6 preconfigured settings and dozens of knobs to turn. If you want to, you can tune this to the n-th degree. ([Details on dithering](docs/dithering.md))
- **Calibrated to real Spectra 6 panel colours**, not theoretical sRGB, for accurate rendering.

**Smart about frames**
- **Multiple frames, one server.** Each frame gets a name and shows up in a dashboard table with battery level, last-seen time, WiFi signal, and when it'll next update.
- **Fair image rotation** — least-shown image goes next, randomised tie-breaking, newly-uploaded photos jump to the front.
- **Ultra-low power on battery** — ~8 µA in deep sleep, so a full charge lasts months. Live battery indicator per frame on the web app (red below 20 %) so you know when to plug in.
- **Errors show up on the screen.** If something goes wrong — wrong WiFi password, server unreachable, configuration missing — the frame renders a readable explanation right on the e-paper instead of silently giving up. No serial-cable debugging required.
- **Overdue-frame warning** banner if a frame is more than an hour late on its scheduled refresh — so you know to check WiFi or the battery before you notice a stale photo on the wall.
- **Schedule-driven refreshes** (e.g. 06:00, 12:00, 18:00) with timezone support; the frame sleeps between refreshes and wakes on its own. This means the screen updates when you want,
- **Clock-synced** — every refresh carries the server's wall-clock such that the screen updates exactly when you want it, while using no power at all in the mean time.
- **Per-frame diagnostics modal** — one click opens the frame's self-reported state (firmware version, boot count, wake cause, WiFi cache hit, free heap, etc.) without a serial cable.
- **Button on the side** forces an immediate refresh regardless of schedule.

**Easy to run**
- **Upload, download and manage everything from the web app.** No extra servers, no Linux config, no Samba share to mount. Just works out of the box.
- **Runs happily on a Raspberry Pi.** Hundreds of 20+ MP photos, dithered once and served from cache thereafter — the per-request load is a file-copy, not image processing. A Pi Zero 2 W handles a multi-frame setup without breaking a sweat.
- **Debian package** with a systemd service for a one-line install, or run from source on any Python 3.9+ host (Linux, macOS, Windows, Raspberry Pi).
- **Pre-built firmware + a wizard flasher** — no ESP-IDF toolchain required. Walks you through WiFi, server address, and naming in a few clicks.
- **Configuration lives on the server** (timezone, schedule, orientation, dither choice) — change it once in the web app, every frame picks it up on its next refresh.
- **Clear Cache & Re-convert** one-click button for when you change orientation, dither algorithm, or want to re-render everything from scratch.

## System Requirements

**Server side** — where you host the image server:
- Any Linux, macOS, Windows, or Raspberry Pi host with Python 3.9+.
- ~256 MB of RAM is plenty. Dithering a fresh upload briefly peaks a little higher while Pillow decodes the source, but it's single-threaded, transient, and you can feed it a slow machine. Dithering takes about 50Mb/image/core so a simple Pi Zero 2 with 512Mb can use all cores to process.
- Disk: ~2 MB per image for the dithered cache + preview + thumbnail, plus however big your originals are. A thousand photos fits comfortably on any SD card.
- Networking: anywhere on the same LAN as the frame. No internet access needed.

**Frame side** — the ESP32-S3 board already inside your Hokku / Huessen frame:
- ESP32-S3 with 16 MB flash and 8 MB octal PSRAM. This is what ships in the frame — the pre-built firmware matches.
- One USB-C cable (any USB-A-to-C or C-to-C works) for first-time flashing and re-configuration.
- 2.4 GHz WiFi (open / WPA2 / WPA3). 5 GHz not supported by the frame's chip.

## Installation

![Logo](images/frame_x_pi.png)

Hokku loves Pi! There is an installer that is designed to get your frame and a Raspberry Pi Zero 2 W to work together in no-time. Just connect both to this computer and run hokku_setup.bat and you'll be off in no time. 

For everybody else who wants to do it the harder way, ou need two things running:

1. **The image server**, on a computer on your network.
2. **The firmware**, flashed onto the frame over USB.

### 1. Install the image server

**Debian / Ubuntu** (recommended):
```bash
# Download the .deb from the latest release, then:
apt install ./hokku-server_2.1.20-1_all.deb
```
Starts automatically via systemd. Web GUI at `http://<your-server>:8080/`. Use web upload, drop photos into `/var/lib/hokku/upload/` — or install Samba so you can manage that folder from any machine on your network.

**Any platform** (from source):
```bash
cd webserver
pip install flask pillow numpy pillow-heif
python webserver.py
```
Photos go in `/images/upload/`. Web GUI at `http://<your-server>:8080/`.

### 2. Flash and configure the frame

1. Take off the front cover — it's magnetically attached, be careful, it damages easily.
2. Connect a USB-C cable from your computer to the board on the back.
3. Run the setup tool:

   **Windows** (easiest — just double-click):
   ```
   hokku_setup.bat
   ```

   **Any platform**:
   ```bash
   cd tools
   pip install pyserial esptool
   python hokku_setup.py
   ```

The setup tool walks you through WiFi, the server address, and a name for this frame. It flashes the firmware and writes the configuration over USB — no toolchain or compilation needed.

![Setup tool configuring a frame](images/configurator.png)

Once configured, the frame will fetch its first image and then settle into its refresh schedule.

## Buttons and LEDs

**The button** on the back of the frame (right-hand side in landscape, lower side in portrait) forces an immediate refresh — pulls the next image from the server right now, ignoring the schedule. Works whether the frame is deep-asleep on battery, plugged into USB, or anywhere in between.

**Two tiny LEDs** on the bottom of the frame:

- **Red** — blinks at 1 Hz whenever a USB host (computer) is plugged in. Off on battery. This is a "host is present" indicator rather than a strict "charging" one — a dumb wall charger doesn't trigger it, although the battery still charges in the background.
- **Green** — solid while the frame is talking to WiFi during a refresh. Otherwise off.

## Supported Image Formats

JPEG, PNG, BMP, TIFF, WebP, GIF, HEIC/HEIF, AVIF. Drop any of these into the upload directory or into the web gui and the server takes care of the rest.

## More Documentation

- **[Image server documentation](webserver/README.md)** — install, web GUI, API endpoints, systemd service.
- **[Dithering pipeline](docs/dithering.md)** — why it looks the way it does; failure modes and countermeasures.
- **[Firmware documentation](firmware/README.md)** — building from source, manual flashing, developer notes.
- **[Firmware design spec](docs/firmware_design.md)** — the state-machine spec the current firmware implements.
- **[Hardware facts](docs/hardware_facts.md)** — confirmed GPIO map, SPI config, init sequence, USB-detection findings.
- **[Changelog](CHANGELOG.md)** — release history.
- **[Disclaimer](DISCLAIMER.md)** — warranty (none), intended use, reverse-engineering notes, privacy.

## Background

I bought this frame in October 2025 from [Wayfair](https://www.wayfair.com/decor-pillows/pdp/hokku-designs-133-inch-wifi-epaper-art-photo-frame-w115006181.html) for about $280 — the cheapest Spectra 6 e-ink display I could find. The stock firmware didn't reliably update the image and was generally a pain to work with, so it was time to replace it. There's no public documentation on the hardware, so I had to do everything the hard way. Decided to make it an experiment in vibe coding something complex; the repo contains zero lines of human-written code.

Claude Opus 4.6-4.7 was used throughout. Unfortunately, one cannot simply tell AI to build this firmware and hope it works — it takes a lot of pushing, prodding and domain knowledge for it to finally do what I needed it to do. AI proved excellent at analysing the original firmware, but needed a lot of hand-holding when writing the hardware interface. My conclusion is that AI, at the time of building this, is a savant fruitfly with ADHD: absolutely blow-me-away amazing at some things, has no idea what it did a minute ago, plain stupid at times, and way too eager to just _do_ things if you don't hold it in check. Can't recommend a vibe-coding career in embedded software just quite yet :)
