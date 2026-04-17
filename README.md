# Hokku/Huessen 13.3" E-Ink Frame open source firmware and image server

Open source firmware and image server for the Hokku / Huessen 13.3" six-color e-ink photo frame.

**Your photos stay on your network.** The stock firmware sends your pictures to servers on the other side of the world — and there's no way to know what happens to them there. These are photos of your family, your home, your life. They deserve better than that. This project replaces the stock firmware completely. Your photos go straight from your computer to the frame, never leaving your home network. No cloud, no accounts, no data collection. Just your photos on your wall.

## Getting Started

You need two things:

1. **The image server** running on a computer on your network — serves and dithers your photos
2. **The firmware** flashed to the frame via USB — connects to WiFi and downloads images from the server

### Quick setup

```bash
# 1. Start the server
cd webserver
pip install flask pillow numpy pillow-heif
python webserver.py
# Drop your photos into /images/upload/
# Web GUI at http://localhost:8080/

# 2. Flash and configure the frame
cd tools
pip install pyserial esptool
python hokku_setup.py
# Follow the prompts: WiFi, server URL, screen name
```

The setup tool detects your frame over USB, flashes the firmware, and writes your WiFi credentials — no toolchain or compilation needed. On Windows, you can also run `hokku_setup.bat` from the root directory for a one-shot setup.

### How to Flash

To flash the firmware, take off the front cover of the frame (it's magnetically attached, be careful as it's easily damaged) and connect a USB-A to USB-C cable to the ESP32-S3 board's USB-C port as shown below:

![Connecting the USB cable for flashing](images/flashing_cable.png)

Once connected, run `python hokku_setup.py` from the `tools/` directory (or `hokku_setup.bat` on Windows).

See [webserver/README.md](webserver/README.md) for server details and [firmware/README.md](firmware/README.md) for firmware development.

## Features

- **Web GUI** — configure the server, browse images, manage screens at `http://server:port/`
- **Multi-screen support** — name each frame, track which images they've shown
- **Fair image rotation** — least-shown image served next, new images get priority
- **Server-driven schedule** — configure refresh times on the server, firmware just sleeps
- **EXIF-aware** — phone photos displayed in correct orientation
- **Landscape or portrait** — pick your mounting orientation, server rotates for you
- **Spectra 6 dithering** — Floyd-Steinberg with measured palette values and dynamic range compression
- **No cloud, no accounts** — everything runs on your local network

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

### LEDs

**Red LED** — solid while awake, blinks at 1Hz while charging, off during sleep

**Green LED** — on while WiFi is connected

## Background

I bought this frame in October 2025 from [Wayfair](https://www.wayfair.com/decor-pillows/pdp/hokku-designs-133-inch-wifi-epaper-art-photo-frame-w115006181.html) for about $280 — the cheapest Spectra 6 e-ink display I could find. The stock firmware didn't reliably update the image and was generally a pain to work with, so it was time to replace it. There's no public documentation on the hardware, so I had to do everything the hard way. Decided to made it an experiment in vibe coding something complex; the repo contains zero lines of human-written code. 

Claude Opus 4.6 was used throughout. Unfortunately, one cannot simply tell AI do build this firmware and hope it works, it takes a lot of pushing and prodding and domain knowledge for it to finally do what I needed it to do. AI proved excellent at analyzing the original firmware, but needed a lot of hand-holding when writing the hardware interface. My conclusion is that AI, at the time of building this, is a savant fruitfly with ADHD: absolutely blow me away amazing at some things, has no idea what it did a minute ago, plain stupid at times and overall way too eager to just _do_ things if you don't hold it in check all the time. Can't recomment a vibe-coding career in embedded software just quite yet :)
