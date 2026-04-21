# Legal Disclaimer

## No Warranty — Absolutely None

**THIS SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED.** There is no guarantee that this firmware / image server
works correctly, does what it claims to do, or is safe to use. The
authors make no representations about the accuracy, reliability,
completeness, or timeliness of the software.

**Use entirely at your own risk.** The authors are not responsible for
any damage to your hardware or data. This includes but is not limited to:

- Incorrect NVS configuration or firmware that bricks the frame
- Display controller (UC8179C) being wedged in an unrecoverable state
  by a flash that goes wrong
- Battery damage from running unsafe charge / sleep schedules
- WiFi credentials being stored on the frame's flash memory (they are
  stored in plain text in the NVS partition — anyone with physical
  access to the device can read them out via USB)
- Data loss from any photos or configuration files on the host running
  the image server
- Any other direct, indirect, incidental, or consequential damages

**This software was written largely by AI** and has had limited
real-world testing beyond the author's own frames. It may contain
bugs, incorrect assumptions about the hardware, or behaviours that
differ from the original Hokku / Huessen factory firmware.

**Before flashing, back up the factory firmware dump from your frame**
(see `CLAUDE.md` flashing procedure). A complete flash-dump can be
restored if anything goes wrong, but only if you saved it first.

## Intended Use

This project is intended for **people who own a Hokku / Huessen 13.3"
Spectra 6 e-ink photo frame** (or very similar hardware) and want to
replace the stock firmware and cloud-hosted image service with an
open-source local-network alternative. It runs photos you upload to
your own server, on your own WiFi, without talking to any third party.

## Reverse Engineering

The firmware in this project was developed through clean-room
reverse-engineering of the original factory firmware, using Ghidra
decompilation + disassembly of a flash dump of a frame the author
physically owns. The display-controller init sequence, post-refresh
shutdown sequence, and SPI timing were derived from that analysis.
See `docs/HARDWARE_FACTS.md` for the confirmed facts and the open
questions. The authors believe this constitutes lawful
interoperability research under DMCA Section 1201(f) (US) and the EU
Software Directive Article 6 — we're making compatible software for
hardware we own, not circumventing any protection mechanism.

**This project does not include, distribute, or circumvent any of the
factory firmware's code.** The factory firmware is not redistributed;
only our own implementation of a compatible command sequence is
included.

## Trademarks

- "Hokku", "Hokku Designs", and "Huessen" are trademarks of their
  respective owners.
- "E Ink" and "Spectra" are trademarks of E Ink Holdings.
- "ESP32", "ESP32-S3", and "ESP-IDF" are trademarks of Espressif
  Systems.
- This project is not affiliated with, endorsed by, or sponsored by
  any of these companies.

## Third-Party Software

- **ESP-IDF** (Apache 2.0) — required to build the firmware from
  source. Not redistributed by this project; obtain it directly from
  Espressif.
- **Pillow, NumPy, Flask, pillow-heif** — the image server's Python
  dependencies. Each has its own license; see their individual
  projects.
- **Measured Spectra 6 palette values** are sourced from the
  [esp32-photoframe](https://github.com/vroland/esp32-photoframe)
  project (GPL). See the dithering pipeline docs for attribution.
- **Factory firmware dump** (under `.private/` in the author's own
  working tree) is not distributed with this repository. Each user
  must extract it from their own frame before flashing. See
  `CLAUDE.md` for the procedure.

## Privacy

- The firmware stores WiFi credentials in the ESP32-S3's NVS
  partition in plain text. Anyone with USB access to the frame can
  read them.
- The image server stores a `database.json` on disk tracking per-image
  and per-screen usage (show counts, last-seen timestamps, IP
  addresses, full `X-Frame-State` dicts). Treat this as sensitive
  local telemetry if you care.
- No data is transmitted off your local network by this project.
  The stock Hokku firmware did talk to external servers; this project
  exists specifically to replace that behaviour.
