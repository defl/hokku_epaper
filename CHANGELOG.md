# Changelog

## 2.1

Two big themes: **in-browser image management** (no more Samba or SSH) and **bulletproof deep-sleep / refresh handling** (the v2.0 firmware had several edge cases where a frame could get stuck never updating, or wake at the wrong time, or be hard to reflash). Plus the unified "60 s post-display awake window" model that replaced three separate ad-hoc waits.

### Web GUI

- **Drag-and-drop upload** anywhere on the page. Whole window becomes a drop zone while dragging; click the upload zone to file-browse. Multiple files at once with a per-file progress list. Filename collisions auto-suffixed (`_1`, `_2`, …).
- **Per-image trash button** with a styled in-page confirmation (Esc cancels, Enter confirms, click backdrop dismisses). Removes the original *and* its cached dithered binary, preview PNG, and thumbnail. Cache stays in sync via `_sync_pool` coalescing concurrent triggers.
- **Image grid shows every uploaded file immediately**, even ones still being converted. Pending entries get a yellow "Dithering…" badge and a faded thumbnail. Status bar reads `N / M ready` while a batch is in progress, plain count when fully caught up. Thumbnail pre-pass at the start of every sync so the grid populates with visible previews even during long dither batches.
- **Per-image stats**: shown count, total display time (human-formatted: `2h 14m`, `3d 5h`), last-displayed timestamp.
- **Connected-screens table**: name, IP, request count, last-seen timestamp, next-scheduled update time (computed from the screen's last `X-Sleep-Seconds` response).
- **REST endpoints** for everything the GUI does: `POST /hokku/api/upload` (multipart), `DELETE /hokku/api/image/<name>`, plus `status`, `original/<name>`, `thumbnail/<name>`, `dithered/<name>`, `show_next/<name>`, `config`, `clear_cache`. All error paths return JSON with a meaningful message — used to leak HTML 500 pages that the GUI parsed as "Unexpected token `<`".

### Server reliability

- **Sleep-accuracy logging.** New `X-Server-Time-Epoch` response header lets the firmware compare actual vs expected sleep duration on the next wake. Logged as `Sleep check: expected=Ns actual=Ms error=±Ks`.
- **`X-Sleep-Seconds` always set** — including on 503/404 responses (capped retry) so the firmware doesn't fall back to its 3 h default after a transient empty-pool window.
- **Thumbnail generator** flattens RGBA / LA / palette-with-transparency PNGs onto a white background before encoding to JPEG. Previously crashed with `cannot write mode RGBA as JPEG` and the image disappeared from the GUI.

### Firmware (the long road)

The v2.0 firmware had a single subtle bug — `esp_sleep_get_wakeup_cause()` occasionally returns `ESP_SLEEP_WAKEUP_UNDEFINED` instead of `TIMER` after a real timer wake on ESP32-S3. v2.0 treated that as "USB host reset, skip fetch", which on a device that kept misclassifying meant: fetch once, then never update again. v2.1 went through several rounds of fixing it, each round revealing the next problem:

- **RTC-clock-based deadline tracking.** Pre-sleep deadline stored in `esp_clk_rtc_time()` units. On wake from any cause: compare with current RTC clock. At/past deadline ⇒ fetch (timer fired, possibly misclassified). Clearly before deadline ⇒ skip (real USB reset, image is fresh). Backed by a 26 h sanity guard that discards a stored deadline if the gap is implausibly large (RTC counter reset while RTC memory survived).
- **`enter_deep_sleep` now honors its contract.** The 120 s reflash wait inside `enter_deep_sleep` used to be added on top of the caller's `sleep_us`; now it's subtracted from the timer arm so the deadline is what the caller asked for.
- **Unified "60 s post-display awake window".** Replaced three separate stages (30 s scheduled-wake wait, 60 s first-boot button polling, 120 s reflash wait inside `enter_deep_sleep`). Now a single `stay_awake_with_buttons()` runs after every displayed image — including error screens. Buttons polled continuously throughout. Pressing the button fetches the next image and extends the window by another full 60 s. Reflash window and button window are the same window.
- **Button polling on GPIO 1 + GPIO 12** (both RTC-wake-capable). GPIO 40 (legacy "switch photo") dropped — not wake-capable on ESP32-S3, so polling it gave half-broken UX. Either of the two RTC-capable buttons does the same job whether the chip is awake or asleep.
- **Button-pin de-isolation** in `stay_awake_with_buttons`. Without `rtc_gpio_hold_dis` + `rtc_gpio_deinit` first, `gpio_config` is silently ineffective on a pin still in RTC-peripheral mode (left there by factory firmware or our own previous `enter_deep_sleep`). Symptom was "button always pressed" → endless fetch loop until the battery died.
- **Spurious-reset safety valve.** The `is_usb_reset_after_sleep` shortcut (skip display init, immediate sleep) caps at 3 consecutive triggers; the next wake forces the full path with a 60 s reflash window. Prevents a chip stuck in a brownout / silicon-quirk reset loop from being unreflashable.
- **USB polling loop** uses `esp_clk_rtc_time()` as the exit condition (not accumulated `vTaskDelay` durations, which under-count by ~1 % per chunk and drifted ~7 min over a 12 h interval).
- **Charger LED behaviour.** `chg_monitor_stop()` moved to just before `esp_deep_sleep_start()`/`esp_restart()` so the red LED blinks throughout the entire awake window. "Device is on and charging" is visible all the way until the chip actually powers down.
- **Failure feedback over LED.** Green WIFI_LED triple-blinks rapidly if a button-triggered fetch fails — so the button isn't mistaken for broken.
- **`wifi_events` event-group leak** fixed (created once, reused). **`strncpy`** of WiFi SSID/password now always null-terminates.
- Display error messages on screen for cfg-version mismatch, missing config, download failure — with the same 60 s reflash/button window applied.

### Versions in this branch

The v2.1 development was a series of incremental releases (v2.1.0 through v2.1.10) as the firmware refresh-loop bugs were chased down one layer at a time. v2.1.10 is the rolled-up release.

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
- Removed embedded calibration image
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
