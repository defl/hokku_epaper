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
- The display controller being wedged in an unrecoverable state by a
  flash that goes wrong
- Battery damage from running unsafe charge / sleep schedules
- WiFi credentials being stored on the frame's flash memory in plain
  text — anyone with physical access to the device can read them out
  over USB. This applies to every supported frame; only the storage
  area differs by model (NVS on the ESP32 frames, sysinfo on the
  Bigme F7)
- Data loss from any photos or configuration files on the host running
  the image server
- Any other direct, indirect, incidental, or consequential damages

**This software was written largely by AI** and has had limited
real-world testing beyond the author's own frames. It may contain
bugs, incorrect assumptions about the hardware, or behaviours that
differ from the original factory firmware.

**Support maturity varies sharply by model.** The Hokku / Huessen
13.3" frame and the Bigme F7 have both been run end-to-end on real
hardware over an extended period. The **Seeed reTerminal E1004 has been
confirmed working on a physical device exactly once** — flash, WiFi,
server fetch, panel render and battery reporting all verified in a
single session. Longer-term behaviour (deep sleep over days, OTA,
a full battery discharge) is still unproven. Treat it as experimental.

**Before flashing, back up the factory firmware from your frame.** A
complete flash dump can be restored if anything goes wrong, but only
if you saved it first. For the Bigme F7 see
`docs/screens/bigme_f7/firmware_dump_procedure.md` and
`docs/screens/bigme_f7/restore_to_stock.md`.

## Intended Use

This project is intended for **people who own one of the supported
six-colour e-ink photo frames** — the Hokku / Huessen 13.3", the
Bigme F7, or the Seeed reTerminal E1004 — or very similar hardware,
and who want to replace the stock firmware and any cloud-hosted image
service with an open-source local-network alternative. It runs photos you upload to
your own server, on your own WiFi, without talking to any third party.

## Reverse Engineering

The firmware in this project was developed through clean-room
reverse-engineering of the original factory firmware, using Ghidra
decompilation + disassembly of a flash dump of a frame the author
physically owns. The display-controller init sequence, post-refresh
shutdown sequence, and SPI timing were derived from that analysis.
The Bigme F7 required a separate effort against a different SoC
(XRADIOTECH XR872AT), including its BROM protocol and panel init
sequence. See `docs/screens/huessen_epf1301/hardware_facts.md` and
`docs/screens/bigme_f7/hardware_facts.md` for the confirmed facts and
the open questions. The authors believe this constitutes lawful
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
- "Bigme" is a trademark of Bigme Cloud Literacy Technology Co., Ltd.
- "Seeed", "Seeed Studio", "reTerminal", and "XIAO" are trademarks of
  Seeed Technology Co., Ltd.
- "XRADIOTECH" and "XR872" are trademarks of Xradiotech / Allwinner
  Technology.
- "E Ink" and "Spectra" are trademarks of E Ink Holdings.
- "ESP32", "ESP32-S3", and "ESP-IDF" are trademarks of Espressif
  Systems.
- "Raspberry Pi" is a trademark of Raspberry Pi Ltd.
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
- **XR872 SDK** — required to build the Bigme F7 firmware from
  source. Not redistributed by this project; obtain it separately.
- **pi-gen** (BSD-3-Clause) — used to build the Raspberry Pi
  appliance image. Not redistributed; fetched at build time.
- **Factory firmware dumps** (under `.private/` in the author's own
  working tree) are not distributed with this repository. Each user
  must extract one from their own frame before flashing; see the
  per-screen documentation for the procedure.

## Privacy

- The firmware stores WiFi credentials in plain text on the frame
  (NVS on the ESP32 frames, sysinfo on the Bigme F7). Anyone with USB
  access to a frame can read them.
- **The appliance image ships with a default login (`hokku`/`hokku`)**
  so the serial console and first SSH session work at all. Change it,
  especially if you enable SSH.
- **The web app has no authentication.** Anyone who can reach port
  8080 on your network can view, upload and delete photos and
  reconfigure your frames. This is deliberate for a home appliance on
  a trusted LAN — do not expose it to the internet.
- The image server stores a `database.json` on disk tracking per-image
  and per-screen usage (show counts, last-seen timestamps, IP
  addresses, full `X-Frame-State` dicts). Treat this as sensitive
  local telemetry if you care.
- No data is transmitted off your local network by this project — it
  makes no outbound requests at all. The stock firmware on these
  frames did talk to external servers; this project exists
  specifically to replace that behaviour.
