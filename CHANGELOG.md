# Changelog

## 2.1.3

- Web UI: pending-dither badge renamed from "Generating preview…" to "Dithering…" with a tooltip — the thumbnail is already visible, so the previous wording suggested the wrong thing was being generated.
- Web UI: removed the redundant "Dithered preview not ready yet" caption on pending cards. The badge above the thumbnail is clear enough on its own.

## 2.1.2

- Fix: thumbnail generation crashed with `cannot write mode RGBA as JPEG` on PNGs that carry an alpha channel (RGBA, LA, or paletted-with-transparency). `_ensure_thumbnail` now composites onto a white background before encoding. New unit tests in `TestEnsureThumbnail` cover all five PIL mode paths plus cache reuse / regeneration on stale source.
- Web UI: delete (trash) button pinned to `z-index: 3` and enlarged to 36×36 so it stays clickable even if the "Generating preview…" badge sits next to it. Badge gets a `max-width` and ellipsis overflow so it can never extend under the button.

## 2.1.1

- Upload (`POST /hokku/api/upload`) and delete (`DELETE /hokku/api/image/<name>`) now catch `OSError` and return a JSON error body with a meaningful message. Previously a filesystem failure (most commonly: `upload_dir` outside the systemd `StateDirectory` allowlist when `DynamicUser=yes` enables `ProtectSystem=strict`) leaked a Flask HTML 500 page, which the web UI couldn't parse and reported as `Unexpected token '<'`.
- Web UI checks `resp.ok` before calling `resp.json()` on upload responses so HTTP errors surface as their actual message.
- Web UI image grid now renders every uploaded file. Files still waiting on the slow dither show a "Generating preview…" badge with a faded thumbnail and the action row collapsed to "Dithered preview not ready yet".
- Status bar shows `N / M ready` while a batch is converting, and the plain count when fully caught up.
- Delete confirmation upgraded from browser `confirm()` to a styled in-page modal (Esc cancels, Enter confirms, click on backdrop dismisses).
- `_sync_pool` now does a fast thumbnail pre-pass before the slow dither loop, so the grid populates with visible previews almost immediately on startup or large uploads.
- `/hokku/api/status` adds `upload_files: [{name, dithered}]` and `upload_size` fields. The legacy `pool_files` field still lists only the dithered set.

## 2.1.0

Image management directly from the web GUI — no need to shell into the server or run Samba.

### Web GUI

- **Drag-and-drop upload**: drop image files anywhere on the page. The whole window becomes a drop zone while dragging; an upload zone above the image grid also takes click-to-browse.
- **Per-image delete**: trashcan button on every thumbnail. Confirms, then removes the original and its cached conversion (binary, dithered preview, thumbnail).
- **Upload progress list**: per-file status with rename notice when filename collisions are auto-suffixed.

### Webserver

- New endpoint `POST /hokku/api/upload` accepts multipart `files`, validates extensions against the supported set, sanitizes names via `secure_filename`, and avoids clobbering existing files by suffixing `_1`, `_2`…
- New endpoint `DELETE /hokku/api/image/<name>` removes the upload and its cached thumbnail; the background sync then prunes the matching dithered cache and pool entry.
- `_sync_pool` now coalesces concurrent triggers via a rerun-pending flag — triggers arriving during a running sync cause one extra pass at the end instead of being dropped, so newly uploaded or deleted files are picked up immediately rather than waiting for the next watcher tick.

---

## 2.0.1

Complete rewrite of the release and deployment model. The firmware is now shipped as a pre-built binary — no toolchain needed. Configuration is stored in NVS and flashed via a setup tool. The webserver has a web GUI and supports multiple screens.

### Privacy

**Your photos stay on your network.** The stock firmware sends your pictures to servers on the other side of the world. This project replaces it entirely. Your photos go straight from your computer to the frame, never leaving your home network. No cloud, no accounts, no data collection.

### New: Setup tool (`hokku-setup`)

- Interactive console installer — detects devices, flashes firmware, writes config
- No ESP-IDF toolchain needed — ships pre-built firmware binaries
- `hokku_setup.bat` for one-shot Windows setup
- Auto-detects ESP32-S3 via USB (VID:PID 303a:1001)
- Reads device state in a single flash read (NVS + app header)
- Identifies Hokku firmware by project name in app binary
- Shows firmware version comparison (device vs release build timestamps)
- Configure-before-flash: NVS config written first so device boots ready
- Auto-backup of existing config before every write
- NVS partition generated via ESP-IDF's `nvs_partition_gen.py` for guaranteed format compatibility

### New: Web GUI

- Accessible at `http://server:port/` (redirects to `/hokku/ui`)
- **Configuration panel**: timezone picker with live server time, refresh schedule (HHMM format), orientation (landscape/portrait), poll interval
- **Connected screens table**: tracks every screen that calls in (name, IP, request count, last seen)
- **Image grid**: thumbnails of all images with original/dithered view links, show count, total display time (human-formatted), "Show Next" button
- **Processing indicator**: shows which image is being dithered, batch progress (e.g. "2 of 5"), and a banner showing remaining count
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

- Build timestamp version (YYYYMMDDHHMMSSZ) embedded at fixed offset in app binary
- Removed NTP sync, timezone handling, and schedule calculation
- Removed embedded calibration image (moved to `resources/`)
- RTC magic value validates stale RTC memory after flash
- USB charging detection: stays awake instead of boot-looping when USB connected
- 120-second reflash window before every deep sleep
- EXIF orientation applied before image processing (fixes rotated phone photos)
- Padding areas forced to pure white after dithering (fixes dotted line artifacts)

### Webserver changes

- Configurable orientation: landscape or portrait
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
