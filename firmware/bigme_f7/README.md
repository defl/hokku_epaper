# Firmware — Developer Notes (Bigme F7 / XR872AT)

Custom firmware for the Bigme F7 7.3" Spectra 6 frame. Same job as the ESP32
firmwares — fetch an image from the server, display it, sleep until the next
scheduled refresh — on a completely different SoC: an **XRADIOTECH XR872AT**
(Cortex-M4F, 4 MB SPI NOR) with no ESP-IDF, no public vendor SDK support, and no
documentation.

Board-independent logic is shared with the other screens via
[`firmware/common/all/`](../common/) (pure C) and `firmware/common/xr872/`
(SoC-specific). Only the panel driver, power management, and board bring-up are
local to this directory.

**For converting a stock unit, start at
[`docs/screens/bigme_f7/bootstrap.md`](../../docs/screens/bigme_f7/bootstrap.md)** —
not with this file.

## Requirements

- **Docker** — the toolchain is containerised; there's no host toolchain to install.
- **An `xr872_sdk` checkout** — not vendored here. Defaults to a sibling of this
  repo, or set `XR872_SDK`.

## Build

```bash
bash firmware/bigme_f7/build.sh
```

Runs `ci-build.sh` inside the builder image and collects the result into
`firmware/release/`. Full toolchain details, SDK patches, and the manual Docker
invocation are in
[`docs/screens/bigme_f7/firmware_build.md`](../../docs/screens/bigme_f7/firmware_build.md).

Environment overrides: `XR872_SDK` (SDK path), `BUILDER_IMAGE` (builder tag).

## Flash

**Use the tooling, not raw writes.** A fresh unit is adopted via the mask BROM —
see [`bootstrap.md`](../../docs/screens/bigme_f7/bootstrap.md), or use
*Flash a screen* in the web app, which drives the same code path. A unit already
running Hokku firmware enters the BROM on its own via the `upgrade` console
command, so no replug/press is needed.

After the first flash, all further updates go
[over the air](../../docs/screens/bigme_f7/ota.md).

## Important notes

- **A/B slots are the safety net — never bypass them.** The flashers write only
  the **inactive** slot plus its config sector, and never touch the bootloader or
  the OEM slot. A new image marks itself verified only after it boots and reaches
  the server, so a bad build rolls back automatically. This is not optional
  belt-and-braces: this SoC's only recovery path runs through the mask BROM, and
  a crash-on-boot image that removed the software BROM trigger is exactly how a
  unit was bricked on 2026-06-13. See the STOP rules in the root
  [`AGENTS.md`](../../AGENTS.md).
- **Recovery hatch.** The `upgrade` console command sets the boot flag and resets
  into the BROM. Keep it working and keep it early in boot — it is the difference
  between a recoverable unit and a paperweight.
- **`sys_reboot` re-enters the BROM**, it does not boot the app. After flashing,
  the unit must be **power-cycled** to actually run the new image. Tooling passes
  `reboot=False` for this reason.
- **The version string is hardcoded** as `FIRMWARE_VERSION` at the top of
  `main.c` — unlike the ESP32 screens, which read a `VERSION` file at build time.
  There is no `firmware/bigme_f7/VERSION`; update `main.c` when bumping. Worth
  unifying, but it needs Makefile changes and a full SDK build to verify.
- **Don't change the panel init sequence** without reading
  [`epd_init_analysis.md`](../../docs/screens/bigme_f7/epd_init_analysis.md) and
  [`display_driver.md`](../../docs/screens/bigme_f7/display_driver.md). The
  sequence was recovered byte-for-byte from the stock firmware; the panel is
  unforgiving about ordering.
- **The panel is Spectra 6, not ACeP**, despite vendor wording — same six inks and
  nibble encoding as the 13.3" frames. A 7-colour ACeP palette produces garbage.
- **WiFi credentials live in sysinfo**, not in the app config sector, so
  reflashing the app slot preserves them. The config blob at `0x340000` holds the
  server URL, screen name, IP settings and power mode
  ([`config.py`](../../python/hokku/screens/bigme_f7/config.py)).
- **Battery is `ADC_CHANNEL_4` (PA14)**, not `ADC_CHANNEL_VBAT` — VBAT reads the
  SoC rail and reports a nonsense 0%.
