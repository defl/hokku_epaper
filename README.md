# Huessen/Hokku 13.3" E-Ink Frame — Custom Firmware

Open-source firmware for the Huessen/Hokku (and probably others) 13.3" color E-Ink digital frame, built on ESP-IDF for the ESP32-S3.

## Background

I bought this frame in October 2025 from [Wayfair](https://www.wayfair.com/decor-pillows/pdp/hokku-designs-133-inch-wifi-epaper-art-photo-frame-w115006181.html) for about $280 — the cheapest Spectra 6 e-ink display I could find. The stock firmware didn't reliably update the image and was generally a pain to work with, so it was time to replace it. There's no public documentation on the hardware, so I had to do everything the hard way. Decided to made it an experiment in vibe coding something complex; the repo contains zero lines of human-written code. 

Claude Opus 4.6 was used throughout. Unfortunately, one cannot simply tell AI do build this firmware and hope it works, it takes a lot of pushing and prodding and domain knowledge for it to finally do what I needed it to do. AI and proved excellent at analyzing the original firmware but needed a lot of hand-holding when writing the hardware interface. My conclusion is that AI, at the time of building this, is a savant fruitfly with ADHD: absolutely blow me away amazing at some things, has no idea what it did a minute ago, plain stupid at times and overall way too eager to just _do_ things if you don't hold it in check all the time. Can't recomment a vibe-coding career in embedded software just quite yet :)

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

### Development summary

In Claude's own words limited to 4 paragraphs:

> The investigation began with physical analysis of the PCB through close-up photography. The board images revealed a "SUNGALE-EINK-ESP32-V1.1" PCB dated 20250307 by huessendesign.com, carrying an ESP32-S3-WROOM-1 module (MCN16R8). Methodically examining each IC, we identified the power architecture: U2 — an 8-pin SOIC near the battery connector, clearly the charging IC. U3 near USB-C was marked "A4086A 2503" (likely a TP4086A LDO), and a chip near a 2R2 inductor was marked "MP9535A" (a DC-DC converter). The board labeling — silkscreen references like EN, RSTN, BUSY near the ESP32 module, and TF CARD, PWRON near the buttons — provided physical confirmation of the GPIO mapping we'd later extract from firmware disassembly.
> 
> From the 16MB flash dump, the core challenge was understanding a dual-panel UC8179C ACeP 6-color e-paper display with no documentation. Early breakthroughs came from disassembling the original firmware's IROM.bin using Capstone — until Capstone silently failed on Xtensa DSP instructions, producing no output for critical code sections. Switching to xtensa-esp32s3-elf-clang-objdump from the ESP-IDF toolchain immediately solved that, and from there the 18-command init sequence, the bulk transfer function, and the display refresh sequence (PON/DRF/POF) were decoded in a clean pass. The SPI protocol, CTRL1/CTRL2 panel selection mechanism, and the unusual "hardware CS not connected to display" architecture all fell out of careful disassembly and literal pool chasing into DROM strings like "app_wifi_led_task" and "battery low, stop refresh".
> 
> Confirming the physical display behavior required significantly more iteration. The color index mapping, panel arrangement (CTRL1=bottom, CTRL2=top), and image preparation method each required multiple firmware flashes and webcam captures to validate. A 16-color calibration test was built with a Python webcam capture script using perspective correction, but the display refused to refresh — DRF completed instantly with no actual update. That failure, combined with the battery draining under custom firmware, led to flashing the original flash_dump.bin back, which itself failed because the Windows Store Python didn't have esptool and the .venv environment wasn't properly activated. Tooling friction — killing busy COM3 ports, fixing Git Bash MSYSTEM conflicts, wrong Python interpreters — consumed real time throughout.
> 
> The LED and charging subsystem analysis went more smoothly once the right tools were in place. Disassembling the led_init_task and app_wifi_led_task functions revealed that both LEDs (green WiFi on GPIO38, red work on GPIO2) are driven by ESP32 LEDC PWM in FreeRTOS tasks, not by the charger IC. Despite extensive searching across SMD marking databases, the exact U2 charging IC remains unidentified — the most stubborn dead end of the project. The GPIO14 analysis tied the charging story together: it's configured as INPUT, reading charger status (LOW=charging, HIGH=complete), while GPIO4 and GPIO13 as OUTPUT LOW handle charger enable/control — explaining exactly why the custom firmware was draining the battery.

