# Hokku 13.3" E-Ink Frame — Custom Firmware

Open-source firmware for the Huessen/Hokku (and probably others) 13.3" color E-Ink digital frame, built on ESP-IDF for the ESP32-S3.

## Background

I bought this frame in October 2025 from [Wayfair](https://www.wayfair.com/decor-pillows/pdp/hokku-designs-133-inch-wifi-epaper-art-photo-frame-w115006181.html) for about $280 — the cheapest Spectra 6 e-ink display I could find. The stock firmware didn't reliably update the image and was generally a pain to work with, so it was time to replace it. There's no public documentation on the hardware, so everything was reverse-engineered.

This is also an experiment in AI-assisted development: the repo contains zero lines of human-written code. Claude Opus 4.6 was used throughout, and proved excellent at analyzing the original firmware but needed a lot of hand-holding when writing the hardware interface.

## Getting Started

You'll need two things running:

1. **A web server** that serves images in the right format — see [webserver/README.md](webserver/README.md)
2. **Custom firmware** compiled with your WiFi credentials and server URL, then flashed to the frame — see [firmware/README.md](firmware/README.md)

(Since this is an AI-only project, you can also just feed the whole directory to Claude Code and let it figure out how to build and flash.)

## How The Firmware Works

1. Boot, connect to WiFi, sync time via NTP (every 96 hours)
2. Download an image from the server and display it
3. Stay awake for 30 seconds (window for USB reflash), then enter deep sleep
4. Wake at the next scheduled hour or on button press
5. On first boot: stay awake for 60 seconds with button-triggered image cycling

### LEDs

There are two LEDs on the board: **red** and **green**.

**Red LED**
- Solid while the device is awake and working
- Blinks at 1 Hz while the battery is charging (2 Hz in charge-only mode)
- Off during deep sleep

**Green LED**
- Off by default
- On while WiFi is connected
- Off again after WiFi shuts down
