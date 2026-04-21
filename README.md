# Hokku / Huessen 13.3" E-Ink Frame — Open Source Firmware & Image Server

Replacement firmware and self-hosted image server for the Hokku / Huessen 13.3" six-colour e-ink photo frame. Runs entirely on your local network — no cloud, no accounts, no third-party servers.

![Web GUI](images/server.png)

## Features

- **Local-only.** Your photos never leave your network. The frame talks to a server you run on your own computer.
- **Upload, download, and manage files directly in the web application.** No extra servers to set up, no Linux configuration, no Samba share to mount. Just works out of the box.
- **Drop your photos in, done.** Any folder the server watches; the web GUI shows everything immediately. JPEG, PNG, BMP, TIFF, WebP, GIF, HEIC/HEIF, AVIF all work — the server auto-converts, rotates via EXIF, and dithers for the six-colour display.
- **Multiple frames, one server.** Each frame gets a name. The web GUI shows a table of all connected frames with battery level, last-seen time, and when they'll next update.
- **Fair rotation.** The least-shown image goes next, with a random tie-break. Newly-uploaded images jump to the front automatically.
- **"Show next" button** on any image to force it onto the frame at the next refresh.
- **Landscape or portrait.** Pick the mounting orientation in the settings panel; the server re-dithers everything accordingly.
- **Three dither algorithms to choose from**, including a hue-aware Atkinson recipe that avoids the common failure modes (blue speckle on skin tones, pink noise on whites). See [`docs/dithering.md`](docs/dithering.md) for the details.
- **Per-image stats** — how often each image has been shown, total display time, last-seen timestamp.
- **Schedule-driven refreshes** — configure one or more refresh times (e.g. 06:00, 12:00, 18:00); the frame sleeps between them to save battery.
- **Ultra-low power on battery** — ~8 µA in deep sleep between refreshes, so a full charge lasts months. The web app shows a live battery indicator for every connected frame (red below 20 %) so you know when to plug in.
- **Errors show up on the screen.** If something goes wrong — wrong WiFi password, server unreachable, configuration missing — the frame renders a readable explanation right on the e-paper instead of silently giving up. No serial-cable debugging required.
- **Clock-synced** — the server tells the frame its wall-clock time on every refresh, so the dashboard can show real drift in seconds.
- **Button on the bottom of the frame** forces an immediate refresh regardless of schedule.
- **Debian package** with a systemd service, or run from source on any Python 3.9+ machine.

## Installation

You need two things running:

1. **The image server**, on a computer on your network.
2. **The firmware**, flashed onto the frame over USB.

### 1. Install the image server

**Debian / Ubuntu** (recommended):
```bash
# Download the .deb from the latest release, then:
apt install ./hokku-server_2.1.19-1_all.deb
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

- **[Technical details](docs/tech_details.md)** — state machine, REST API, hardware map, firmware internals.
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
