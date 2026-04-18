# Changelog

## 2.1.9

Firmware bug fix. Webserver unchanged.

### Firmware

- **Fix: "button always pressed" loop on first boot.** `stay_awake_with_buttons` configured GPIO 1 / GPIO 12 as digital inputs with pull-up via `gpio_config`, but if the pin was still in RTC-peripheral mode (left there by factory firmware's wake config, or by a previous `enter_deep_sleep`'s `rtc_gpio_init` for ext1 wake), `gpio_config` is silently ineffective — the pin keeps reading whatever the RTC peripheral was driving (LOW in the observed case). Result: every entry into `stay_awake_with_buttons` detected a "press" 50 ms later, triggering an endless fetch loop until the battery died. Fix: same `rtc_gpio_hold_dis` + `rtc_gpio_deinit` + `gpio_reset_pin` pattern that `hw_gpio_init` already uses for the display pins. Plus a 20 ms settle delay after `gpio_config` so the pull-up has time to charge the line HIGH before the first read.

## 2.1.8

Firmware safety + cleanup. Webserver unchanged.

### Firmware

- **Spurious-reset safety valve.** The `is_usb_reset_after_sleep` shortcut (skip display init, immediate sleep) now caps at `MAX_SPURIOUS_RESETS = 3` consecutive triggers via a new RTC counter. After that many short-paths in a row, the next wake takes the full path with `stay_awake_with_buttons` so the user always gets a 60 s reflash window — even if something keeps spurious-resetting the chip (brownout-during-sleep, persistent silicon misclassification). Counter resets to 0 on any non-spurious wake. Battery cost: at most one full 60 s awake window per 3 spurious-reset cycles.
- **Wakeup classifier cleanup.** Removed the `is_true_first_boot` and `is_misclassified_wake` named booleans — they were used only for the log label. Inlined into the `ESP_LOGI` ternary chain and renamed `had_prior_sleep` → `prior_sleep`. Net change: ~25 fewer lines, same behavior.

## 2.1.7

Major firmware simplification: unified awake-window model. Webserver unchanged.

### Firmware

- **Single `stay_awake_with_buttons()` after every displayed image** — replaces three separate stages (30 s "scheduled wake" wait, 60 s first-boot button polling, 120 s `enter_deep_sleep` reflash window). One 60 s window with continuous button polling. Pressing the button fetches the next image and resets the window for another full 60 s.
- **`enter_deep_sleep` no longer does any awake-time wait** — it goes to sleep right away. The reflash window is now a property of the awake period (managed by the caller via `stay_awake_with_buttons`), not a property of the sleep transition.
- **Button polling now uses GPIO 1 + GPIO 12** (the two RTC-capable wake buttons; either one can wake from deep sleep AND trigger next-image while awake). GPIO 40 (legacy "switch photo") is dropped — it isn't wake-capable on ESP32-S3, so polling it gave half-broken UX (worked while awake, did nothing from sleep).
- **Removed BUTTON_1 force-sleep behavior** — only one user button matters, and the awake window expires on its own.
- **Error paths (config-version mismatch, missing config, download failure) also enter `stay_awake_with_buttons`** so the reflash window applies uniformly. Download-failure additionally lets the user retry by pressing the button.
- **Fixed user-facing log timing**: previous "First boot: 60s awake window" was actually only ~30 s in practice (deadline was relative to boot, not to display-done). New log honestly says "Awake for 60s" and the 60 s is from when polling actually starts.
- **`is_usb_reset_after_sleep` shortcut preserved** — spurious early resets still skip display init and go straight back to sleep, no awake window. Once asleep, normal button-wake works.

## 2.1.6

Firmware battery-life tightening + review-driven polish. Webserver unchanged.

### Firmware

- **Spurious-reset wake path no longer powers up the display**. On a wake classified as `is_usb_reset_after_sleep` (pre-deadline, screen already showing the right image), we now skip `hw_gpio_init`, `EPAPER_PWR_EN` drive, `chg_monitor_start`, the 500 ms display-controller warm-up, `spi_init`, and the battery read — and go directly to `enter_deep_sleep`. Saves ~600 ms of active-mode CPU plus 120 s of display-rail power per spurious reset.
- **`enter_deep_sleep` teardown is now safe on uninitialised SPI.** Guarded `spi_bus_remove_device`/`spi_bus_free` with a NULL check + pointer clear, enabling the pre-init early-exit path above.
- **New `is_misclassified_wake` log label** for wakes where `esp_sleep_get_wakeup_cause()` came back as UNDEFINED but the RTC clock says we're at/past the deadline (previously logged as "(button)", which was misleading).
- **Defensive single-read of `esp_clk_rtc_time()` per USB polling-loop iteration.** Previously the while-check and the subtraction were two separate clock reads; a context switch between them could push `now` past the deadline and underflow `deadline - now` to a huge value, costing one unnecessary 1 s `vTaskDelay` before the loop exited.
- **Removed duplicate `gpio_set_level(PIN_SYS_POWER, 1)`** in app_main — `hw_gpio_init` already drives it HIGH.
- **Documented** that only TIMER + EXT1 are enabled as wake sources; any other value from `esp_sleep_get_wakeup_cause()` falls through to the deadline-based analysis.

## 2.1.5

Firmware follow-up after adversarial review of 2.1.4. Webserver unchanged.

### Firmware

- **Fix: USB polling loop used vTaskDelay-accumulated time to decide when to exit, drifting ~1% short per chunk.** For a 12 h refresh interval that's ~7 min of under-count, triggering `esp_restart()` well before the deadline. The next boot's classifier then saw `gap > SLACK` and incorrectly skipped the fetch. Now the loop checks `esp_clk_rtc_time() < deadline_rtc_us` directly, so tick-rounding can't exit early.
- **Fix: misclassified-but-at-deadline wakes fell into the 60 s first-boot button-polling window.** When `esp_sleep_get_wakeup_cause()` returns `UNDEFINED` at/past the deadline, we correctly fetch — but then the `if (is_scheduled_wake)` gate after display didn't match, so control fell through to the first-boot button-polling loop (60 s extra awake per scheduled refresh). Snapshot `was_sleeping` before clearing it and extend the gate to `is_scheduled_wake || had_prior_sleep` so any post-sleep wake takes the quick-sleep-again path.
- **Cleanup: removed unreachable `else` fallback in `is_usb_reset_after_sleep` branch.** The classifier guarantees `scheduled_wake_rtc_us > 0` when reaching this branch; the `last_sleep_seconds` fallback was dead code.

## 2.1.4

Firmware correctness fix. Webserver unchanged.

### Firmware

- **Fix: deep-sleep refresh lockout on battery.** Scheduled timer wakes that the ESP32-S3 misreported as `ESP_SLEEP_WAKEUP_UNDEFINED` were treated as "USB host reset after sleep" and skipped the fetch. On a device that kept hitting the misreport, the screen would fetch once, enter the skip-path on every subsequent wake, and never update again. Fix uses the RTC slow clock (`esp_clk_rtc_time()`) to compare the actual elapsed time against a deadline set pre-sleep: at/past deadline ⇒ fetch (timer fired, just misclassified); clearly before ⇒ skip (real USB reset). Handles silicon cases where `esp_sleep_get_wakeup_cause()` is unreliable. Backed by a 26 h sanity guard that discards a stored deadline if it points implausibly far into the future (RTC counter reset without memory wipe).
- **`enter_deep_sleep` now honors its contract.** Internal 120 s reflash window no longer shifts the caller's deadline forward; the function computes the remaining time against the recorded deadline and arms the timer for that. Previously, repeated early-resets compounded the drift.
- **Charger LED keeps blinking until actual deep sleep.** `chg_monitor_stop()` moved from the top of `enter_deep_sleep` to just before `esp_deep_sleep_start()`/`esp_restart()`. The red LED now blinks throughout the 120 s reflash window and any on-USB polling wait, so "device is awake and charging" stays visible all the way through.
- **Triple green-LED blink on button-press download failure.** Previously a failed "next image" button press was silent over serial only. Now the WIFI_LED triple-blinks rapidly so a user without a serial connection gets visible feedback.
- **`wifi_events` leak fixed.** Was creating a fresh `EventGroup` on every `wifi_connect()` call (leaked on button-press retries).
- **`strncpy` null-termination** on `wifi_cfg.sta.ssid`/`.password` — an SSID or password exactly the buffer length could leave the WiFi stack reading past the end.

### Web UI (mentioned in top-level README)

- LED documentation updated to describe the new behaviors (triple-blink on green, red blinks throughout the reflash window).

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
