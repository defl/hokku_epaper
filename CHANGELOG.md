# Changelog

## 2.0.0 (unreleased)

Complete rewrite of the release and deployment model. The firmware is now shipped as a pre-built binary — no toolchain needed. Configuration is stored in NVS and flashed via a setup tool. The webserver has a web GUI and supports multiple screens.

### Privacy

**Your photos stay on your network.** The stock firmware sends your pictures to servers on the other side of the world. This project replaces it entirely. Your photos go straight from your computer to the frame, never leaving your home network. No cloud, no accounts, no data collection.

### New: Setup tool (`hokku-setup`)

- Interactive console installer — detects devices, flashes firmware, writes config
- No ESP-IDF toolchain needed — ships pre-built firmware binaries
- Auto-detects ESP32-S3 via USB (VID:PID 303a:1001)
- Reads device state in a single flash read (NVS + app header)
- Identifies Hokku firmware by project name in app binary
- Compares on-device firmware against release binaries
- Auto-backup of existing config before every write
- NVS partition generated via ESP-IDF's `nvs_partition_gen.py` for guaranteed format compatibility

### New: Web GUI

- Accessible at `http://server:port/` (redirects to `/hokku/ui`)
- **Configuration panel**: timezone picker with live server time, refresh schedule (HHMM format), poll interval
- **Connected screens table**: tracks every screen that calls in (name, IP, request count, last seen)
- **Image grid**: thumbnails of all images with original/dithered view links, show count, total display time (human-formatted), "Show Next" button
- **Processing indicator**: shows which image is being dithered and progress
- **Clear cache** button for full re-conversion
- Config changes saved to disk via POST API

### New: Multi-screen support

- Screens identify themselves via `X-Screen-Name` HTTP header
- Screen name stored in firmware NVS (max 64 bytes)
- Server tracks all screens in `database.json` (name, IP, request count, last seen)
- Device endpoint renamed from `/spectra6` to `/hokku/screen/`

### New: Server-driven sleep schedule

- Firmware has no concept of time, timezone, or NTP — all removed
- Server calculates seconds until next refresh from `refresh_image_at_time` config
- Sleep duration sent as `X-Sleep-Seconds` HTTP response header on image download
- One HTTP call does everything: image + sleep duration

### New: Fair image distribution

- Replaced shuffled playlist with `show_index` ranking system (supports negative values for priority)
- New images automatically get priority: existing show_index values reset to 1
- "Show Next" button in web GUI sets show_index to min-1
- Tracks `total_show_count` and `total_show_minutes` per image with human-readable formatting
- Display time tracked: when next image is served, elapsed time added to previous image
- Random tie-breaking when multiple images have the same show_index
- Persistent tracking in `database.json`

### New: NVS config system

- All configuration (WiFi SSID/password, server URL, screen name) stored in NVS
- `secrets.h` removed entirely — no compile-time configuration
- Config version byte (`cfg_ver`) for forward compatibility
- Firmware validates config version on boot, shows on-screen error if mismatched
- On-screen error messages for: missing config, version mismatch, download failure

### New: Debian packaging

- `pyproject.toml` for pip-installable webserver (`hokku-server` command)
- Full `debian/` packaging: control, rules, systemd service, postinst, conffiles
- `DynamicUser=yes` with `StateDirectory` for secure service isolation

### Firmware changes

- Removed NTP sync, timezone handling, and schedule calculation
- Removed embedded calibration image (moved to `resources/`)
- RTC magic value validates stale RTC memory after flash
- USB charging detection: stays awake instead of boot-looping when USB connected
- 120-second reflash window before every deep sleep
- EXIF orientation applied before image processing (fixes rotated phone photos)
- Padding areas forced to pure white after dithering (fixes dotted line artifacts)

### Webserver changes

- Configurable poll interval (`poll_interval_seconds`)
- Config file loaded from `HOKKU_CONFIG` env, `./config.json`, or `/etc/hokku/config.json`
- Config saveable from web GUI
- `strict_slashes=False` on device endpoint (no more 308 redirects)
- EXIF orientation applied in image conversion and thumbnail generation
- All endpoints renamed from `/spectra6/` to `/hokku/`
- Removed `/hokku/preview`, `/hokku/status`, `/hokku/clear_cache` (replaced by `/hokku/api/*`)

### Breaking changes

- Firmware no longer reads `secrets.h` — use `hokku-setup` to flash NVS config
- Server endpoint changed from `GET /spectra6` to `GET /hokku/screen/`
- `database.json` format changed: `show_count` renamed to `show_index`, added `total_show_count` and `total_show_minutes`
- Old `database.json` files auto-migrated on load

---

## 1.0.0

Initial release. Firmware decoded from original Huessen firmware disassembly. Webserver with Floyd-Steinberg dithering to Spectra 6 palette.
