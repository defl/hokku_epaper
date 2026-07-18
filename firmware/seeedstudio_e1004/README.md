# Firmware — Developer Notes

ESP-IDF port of the Seeed reTerminal E1004 hokku client. Same framework, build
system, and driver conventions as `firmware/huessen_epf1301` (ESP-IDF, not
Arduino). Downloads an image from the hokku server, displays it on the
T133A01 dual-chip panel, and deep sleeps for `X-Sleep-Seconds`.

Ported from the Arduino/GxEPD2 sketch in [`clients/reterminal_e1004/`](../../clients/reterminal_e1004/)
(added in PR #16, following up on issue #14) — see that directory for the
original, GxEPD2-based implementation and its own README (pinout notes,
build instructions for the Arduino toolchain).

## Status — read before flashing

**Written but UNBUILT and UNFLASHED.** No ESP-IDF build or real E1004
hardware was available while porting; this has not been compiled, let alone
run on a device. The Arduino sketch it was ported from IS verified working
end-to-end on stock E1004 hardware (colors, orientation, ~30s cycle time —
see `clients/reterminal_e1004/README.md`), and this port carries over its
pin map, register values, and wire-format handling directly from that
verified source plus the vendored driver. What it does NOT carry any
verification for is the ESP-IDF-specific plumbing that's new here: the
DC-line two-phase SPI send, chunked DMA transfers, WiFi/HTTP glue, and ADC
calibration. Treat all of that as needing a real build + flash + scope/serial
bring-up pass before trusting it.

**No A/B OTA.** This firmware uses a single `factory` partition, matching
the Arduino sketch's own scope (no OTA there either). Per root `AGENTS.md`'s
firmware-flashing STOP rules, it must NOT be flashed to a device without
either adding dual-slot/A-B support and a recovery hatch first, or explicit
human sign-off to flash without them.

**Naming**: this directory uses `seeedstudio_e1004` for the `brand_model`
screen ID (per `AGENTS.md` "Screen naming"), whereas `clients/reterminal_e1004/`
uses the bare product name. Not yet reconciled — pick one before this goes
further; flagged in the PR #16 review as an open question.

**Config is compile-time only.** No NVS config store (unlike
`huessen_epf1301`'s runtime-provisioned config) — WiFi SSID/password, server
host/port, and screen name are `static const` in `main.c`, matching the
Arduino sketch's `USER CONFIG` block. Edit those before building.

## Requirements

- ESP-IDF v5.x (same version family as `firmware/huessen_epf1301`)
- Seeed reTerminal E1004 (ESP32-S3, 8MB octal PSRAM; flash size per
  `sdkconfig.defaults` comment — not independently re-verified)

## Build

```bash
. /path/to/esp-idf/export.sh
cd firmware/seeedstudio_e1004
idf.py build
```

## What was and wasn't changed from the Arduino version

- **Panel driver**: translated 1:1 from `GxEPD2_T133A01_1200x1600.cpp`'s
  `_InitDisplay()` / `_sendFrameToDisplay()` / `refresh()` — same register
  values, same CS0-only-vs-both grouping, same command order. SPI transfers
  use ESP-IDF's DMA-backed `spi_device_polling_transmit` in 4800-byte chunks
  (matching `huessen_epf1301`'s `epaper_send_panel` chunking) instead of the
  Arduino driver's per-byte `SPI.transfer()` loop.
- **Color LUT — dropped, not ported.** The Arduino sketch remaps every byte
  through an inverse LUT because it goes through GxEPD2's generic
  color-index framebuffer, which re-encodes to the panel's native nibbles at
  SPI-out time. This port talks to the panel directly with no GxEPD2 layer,
  and hokku's wire-format nibbles are already identical to the T133A01's
  native encoding (see `docs/screens/huessen_epf1301/hardware_guesses.md` →
  "Display — Cross-reference: Seeed T133A01 driver"), so the downloaded
  bytes are DMA'd straight to the panel unchanged. One fewer 960,000-byte
  pass, and removes a correctness dependency on the LUT math being right.
- **DC line handling — this is the one place the port isn't a mechanical
  translation.** `huessen_epf1301`'s board has no DC pin; it distinguishes
  command vs. data purely via the SPI peripheral's hardware command-phase
  (`command_bits`). The T133A01 board has a real DC GPIO (11) that must be
  driven LOW for the command byte and HIGH for the following data bytes,
  same as the Arduino driver's `digitalWrite(_dc, ...)` calls. `main.c`
  implements this as two back-to-back SPI transactions (command byte with
  DC low, then data with DC high) with chip-select held across both —
  electrically equivalent to the Arduino driver's mid-transaction DC
  toggle, since neither approach touches CS in between.
- **WiFi**: single-network connect only, no `huessen_epf1301`-style
  dual-network fast-reconnect cache — matches the Arduino sketch's scope.
- **Deep sleep**: timer-wake only, no button/EXT1 wake source (the E1004
  client has no button in scope) — matches the Arduino sketch.
- **Added beyond the Arduino sketch**: sends `X-Screen-Model:
  seeedstudio_e1004` (the Arduino sketch only sends `X-Screen-Name` /
  `X-Battery-mV`), for dashboard parity with how `huessen_epf1301` reports
  itself.

## Not carried over from `huessen_epf1301`

Deliberately out of scope for this port — it matches the Arduino sketch's
feature set, not `huessen_epf1301`'s full appliance feature set:

- No NVS-backed runtime config (`hokku_config`-style provisioning)
- No `X-Frame-State` telemetry / dashboard "Details" reporting beyond name + battery
- No OTA
- No button / USB-awake regime state machine — this firmware always fetches
  once and deep-sleeps; every wake is a fresh `app_main()` from a chip reset
- No ring-buffer log upload

Any of these would be reasonable follow-ups if this screen moves from
prototype to a supported target, mirroring how `bigme_f7` was brought to
parity with `huessen_epf1301` in stages.
