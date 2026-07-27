# Changelog

## Unreleased

### Added

- **Firmware library — download newer firmware from GitHub and pin it per model.**
  The Admin tab has a new *Firmware library* panel. The server still ships bundled
  firmware and works fully offline, but you can now press *Check GitHub for
  firmware* to list downloadable releases (tick *Include pre-releases* to also see
  betas), *Download* one into a writable overlay (`/var/lib/hokku/firmware`), and
  *pin* which version each screen model is offered over-the-air or at flash time.
  Nothing is downloaded or selected automatically — the server only reaches the
  internet on an explicit button press (that click is the consent), and betas are
  never chosen unless you pin one deliberately. Downloads are validated as real
  images for their model before being admitted. New config keys:
  `firmware_github_repo`, `firmware_dir`.

### Fixed

- **Flashing a screen over USB could leave it running its old firmware.** The
  merged firmware image covers the bootloader, partition table and the `ota_0`
  app slot, but not `otadata` — the record that tells the bootloader which of the
  two A/B slots to run. A screen that had previously taken an over-the-air update
  boots from `ota_1`, so a USB flash wrote the new firmware into a slot the
  bootloader was ignoring: the flash reported success, the version in the GUI
  went up, and the screen quietly carried on running the old build. Flashing now
  clears `otadata` so the freshly written slot is the one that boots. Relatedly,
  the screen scan used to read the version out of `ota_0` regardless of which
  slot was active, which is what made this invisible; it now consults `otadata`
  and reports the version the device actually runs.

- **The server package shipped incomplete data files.** The pip wheel declared
  only the face-detection model as package data, so the Flask `templates/` and
  `static/` trees were silently dropped — a plain `pip install` of the server
  started fine but 500'd the dashboard with `jinja2 TemplateNotFound:
  index.html` (fix contributed by @TaichungLester, #26). The `.deb` had the
  matching gap for `static/fonts/` (hand-listed files, fonts never included).
  Both packages now ship the full `templates/` and `static/` trees — the `.deb`
  install list copies the directories recursively instead of enumerating files,
  so new assets can't be forgotten again.

### Packaging

- The default server config is now shipped as **`config.json.example`** in both
  the wheel and the `.deb`, so `pip`/`apt` users get a working template to copy.
  The machine-specific developer config (`python/config.json`) is no longer
  tracked. On the appliance the systemd unit and installer seed the live config
  from this example, unchanged in behaviour.
- The **pip wheel** (`hokku_server-*.whl`) is now built and content-verified in
  CI (a guard that its data files are actually present, mirroring the existing
  `.deb` content check) and attached to published GitHub releases. `pip install`
  resolves all runtime dependencies automatically. Note the wheel is
  **server-only and does not bundle screen firmware images** (those are release
  build artifacts), so the flash-a-screen and OTA firmware-update features need
  the `.deb`/appliance; everything else works from the wheel.

## 4.0.0 beta 1

The release where Hokku stops being firmware for one photo frame and becomes a
small platform: **three supported screens, and an appliance image that turns a
Raspberry Pi into a photo frame server without ever opening a terminal.**

### Headlines

- **The appliance image** — write one file to an SD card, boot a Pi Zero 2 W,
  join the WiFi network it creates, and fill in a form. No keyboard, no monitor,
  no command line, no Python.
- **Three screens, one library** — the 13.3" Hokku / Huessen frame, the $99 7.3"
  **Bigme F7**, and the **Seeed reTerminal E1004** (experimental). Mix models,
  sizes and orientations against the same photos.
- **A whole SoC reverse-engineered** — the Bigme F7 runs an XRADIOTECH XR872AT
  with no public SDK support and no vendor documentation. Supporting it meant
  recovering the panel init sequence from stock firmware and writing a BROM
  flasher from scratch in Python.
- **Over-the-air firmware updates, per screen** — flip a toggle and a frame
  updates itself on its next refresh, rolling back automatically if the new
  build can't reach the server.
- **Flash a screen from the web app** — adopt a brand-new frame over USB from
  the browser, including a factory-fresh F7.
- **Face-aware cropping** — when a photo is cropped to fill the panel, faces are
  detected locally and kept in frame instead of being sliced off.

> **Beta, and it means it.** Support maturity varies by screen: the
> Hokku / Huessen frame and the Bigme F7 have been run end-to-end on real
> hardware for a long time; the **Seeed reTerminal E1004 has been confirmed
> working on real hardware exactly once.** See [Hardware](docs/hardware.md) for
> per-screen status.

---

### Upgrading from 3.x

**Your existing frames keep working.** Upgrade the server — the `.deb`, or by
writing the appliance image to a fresh SD card — and frames already on 3-series
firmware carry on fetching photos as before. No reflash is needed just to keep
things running.

They will, however, stay on their old firmware until you update them once over
USB. Frames on 3-series firmware predate over-the-air updates entirely, so that
first update has to be a cable: use *Flash a screen* in the web app, or
`tools/hokku_setup.py`. Everything after that can be wireless.

> A late fix in this release matters here. During alpha testing a 3-series frame
> stopped receiving images the moment its server was upgraded: the new
> image-serving path required an `X-Screen-Model` header that only exists from
> firmware 1.2.9, and returned 400 without it. Because that rejection happened
> before any update signalling — and 3-series firmware has no OTA to signal to —
> an affected frame could not be rescued remotely at all; it needed a USB
> reflash purely to start showing photos again. A frame that doesn't identify
> its model is now correctly treated as a Hokku / Huessen frame, which is what
> it must be: no other model existed before that header did.

---

### The appliance image

Previously, getting a server running meant imaging a Pi, SSHing in, and
installing a `.deb` — or running the Windows setup wizard. Now there's a
prebuilt image, attached to this release, that does all of it.

On first boot it raises an open WiFi access point called **`Hokku Setup`** and
serves a captive-portal setup form: WiFi network and password, country,
hostname, timezone, static or DHCP addressing, mDNS name, and optional SSH and
Samba. Submit it and the Pi reboots onto your own network with the server
running at `http://<name>.local:8080`.

It recovers from its own mistakes. If the WiFi details turn out to be wrong, a
watchdog notices the Pi never connected and puts the setup access point back up
rather than leaving a headless brick on a shelf. A `reset.sh` does the same on
demand.

The image is hardened for unattended appliance duty: logs to RAM instead of the
SD card, security-only unattended upgrades, Bluetooth and swap disabled, and a
USB gadget serial console so a single cable to a laptop gives a login prompt
with no monitor or keyboard. Built with pi-gen from `os/pi/`, and built by CI —
publishing a release attaches the image to it automatically.

Documentation: [appliance guide](docs/appliance.md),
[image build](os/pi/README.md), [installer internals](installer/README.md).

### Multi-screen support

The server used to assume one panel geometry and one palette. It now identifies
each frame by an `X-Screen-Model` header and runs a **model-aware render
pipeline**: images are converted per model, cached per model, and served to each
frame in its own resolution and orientation. A `Display` registry holds the
per-panel specification, so adding a screen means adding one entry rather than
touching the pipeline.

Per-screen settings moved out of global config into a per-frame Config modal:
orientation, letterbox fill limit, OTA opt-in, and a server URL override for
frames that need to be pointed somewhere else.

### Bigme F7 (7.3", ~$99)

The cheapest way into Hokku, and the first non-ESP32 screen. The XR872AT has no
ESP-IDF equivalent and no public documentation, so this took a full
reverse-engineering effort: dumping the stock flash, decoding the EK79655 panel
init sequence byte-for-byte, and implementing the chip's mask-BROM protocol in
Python so a fresh unit can be adopted with no vendor tooling on any platform.

What works: custom firmware, WiFi with persistence across reflashes, image fetch
and display, A/B OTA with automatic rollback, real battery reporting, deep sleep
with USB awareness, and mDNS `.local` resolution (which needed lwIP 2.1.2 rather
than the SDK default).

Flashing only ever writes the inactive A/B slot and its config sector — never
the bootloader, never the OEM slot — so a bad image rolls back instead of
bricking the unit.

Documentation: [screen overview](docs/screens/bigme_f7/README.md),
[bootstrap](docs/screens/bigme_f7/bootstrap.md),
[restore to stock](docs/screens/bigme_f7/restore_to_stock.md).

### Seeed reTerminal E1004 (experimental)

A 13.3" Spectra 6 panel on a Seeed XIAO ESP32-S3. The firmware is a thin board
layer over the shared `firmware/common/` modules, so it inherits feature parity
with the huessen frame for free.

**Confirmed working on real hardware once**
([issue #14](https://github.com/defl/hokku_epaper/issues/14)): built with
ESP-IDF v5.5.5, flashed over USB, then WiFi, server fetch, a correctly coloured
and oriented photo on the panel, and battery reporting in the dashboard — which
validates the ESP-IDF SPI/DMA plumbing and the ×2.0 battery divider. One unit,
one session: deep sleep over days, OTA and full-discharge battery behaviour are
still unproven, and app logs do not reach the native USB serial console. More
reports very welcome.

The panel bring-up work it is based on was contributed by
[@TaichungLester](https://github.com/TaichungLester) in
[PR #16](https://github.com/defl/hokku_epaper/pull/16).

### Over-the-air firmware updates

Open a frame's details, turn on *Update firmware on next refresh*, and the frame
downloads and installs new firmware itself on its next check-in. The previous
build stays in the second slot and is restored automatically if the new one
can't reach the server afterwards.

The server serves the right firmware per model, retries transient failures with
a bounded backoff so they self-heal, and flags frames whose firmware is behind
the bundled build — compared per model, not against a single global version.
The first install on any frame is still over USB; everything after can be
wireless.

### Flash a screen from the web app

If the server runs on the machine you plug frames into — the appliance case —
the *Flash a screen* page installs firmware over USB straight from the browser,
picking the right method per model. For the Bigme F7 that includes adopting a
factory-fresh unit: catching the mask BROM, writing slot 0, and provisioning
WiFi and server URL, all from the page. A unit already running Hokku firmware
enters the BROM on its own, with no button press needed.

### Image conversion

**Face-aware cropping** — the existing fill-crop (which trims photos close to
the panel's aspect ratio rather than letterboxing them) now detects faces
locally and biases the crop to keep them in frame.

### Firmware

- **huessen_epf1301 1.2.18** — moved onto the shared `firmware/common/`
  modules, exponential backoff when the server is unreachable, retry before
  rollback on a pending-verify boot, and version/build headers sent to the
  server.
- **bigme_f7 1.2.5** — new: full custom firmware for the XR872AT.
- **seeedstudio_e1004 1.2.1** — new, and now confirmed working on real
  hardware once ([issue #14](https://github.com/defl/hokku_epaper/issues/14)).

Firmware is now built from a shared foundation: `firmware/common/all` (pure C,
every screen), `firmware/common/esp32` (ESP-IDF) and `firmware/common/xr872`.
Release artifacts are named `hokku-<brand_model>-<version>`.

### Documentation

Substantially rewritten for a project with more than one screen: a
[hardware guide](docs/hardware.md) covering every frame with prices and honest
per-screen status, per-screen overview pages, an
[appliance guide](docs/appliance.md), and build/internals READMEs for `os/pi`
and `installer`. Every internal link in the repository was checked and repaired.

### Known issues and caveats

- The **Seeed reTerminal E1004 has one confirmed hardware run**, not a long
  track record — treat it as experimental. Its app logs also do not reach the
  native USB serial console; watch the panel instead.
- The **web app has no authentication.** Anyone who can reach port 8080 can
  manage your library and frames. Fine on a trusted home network; do not expose
  it to the internet.
- The **appliance image ships with a default `hokku`/`hokku` login** so the
  serial console and first SSH session work. Change it, particularly if you
  enable SSH.
- Converting a stock Bigme F7 **replaces the vendor firmware** and is closer to
  a one-way trip than flashing the ESP32 frames. Restoring to stock is
  documented and tested, but read it before buying.
- The Bigme F7's firmware version is hardcoded in `main.c` rather than read from
  a `VERSION` file like the other two screens.

---


## 3.1.0 alpha 1

### Frame log upload

After every refresh the frame now sends a log of everything that happened — WiFi connection, image download, display result — to the server as part of the refresh request. The server stores the last log received per frame and shows it in the screen detail modal. No cable or serial terminal needed to see what the frame has been doing.

The log buffer lives in the frame's RTC memory, so it survives deep sleep and accumulates across multiple cycles. Each upload covers the last several refresh cycles' worth of diagnostics.

### Colour space improvements

Two new dithering colour spaces: **OKLAB** and **CAM16-UCS**, joining the existing CIELAB variants. Each can be used independently for palette matching, adaptive saturation, and dynamic range compression. For most photos the results are similar to CIELAB; CAM16-UCS tends to hold hue accuracy better on saturated colours.

### Orientation filter per screen

Each screen now carries its own orientation (landscape or portrait) and the server serves only images converted for that orientation. Previously orientation was a global server setting. Multiple screens with different orientations can run from the same image library simultaneously.

### Relative next-update time in Screens tab

The "next update" time in the Screens tab now shows both the absolute time and a relative "in X minutes / X hours" label.

### Web app housekeeping

The separate "Clear cache" and "Re-convert all" buttons are merged into one. The image detail modal now shows the minimum letterbox fill threshold.

### Firmware 1.2.4

- **Frame log upload** — ring buffer in RTC memory captures log output across deep sleep cycles and POSTs it to the server on each refresh.
- **Firmware version in log** — every boot logs the firmware version and build timestamp as the first line, visible both on serial and in every log dump shown in the web app.
- **Performance** — CPU bumped to 240 MHz; compiler optimised at -O3; code and rodata execute from PSRAM (eliminating flash/PSRAM cache contention during image downloads); instruction cache doubled to 32 KB; chip revision v0.2 targeting drops a now-unnecessary silicon workaround.
- **Build tooling** — `build.bat` sets all required environment variables; `IDF_TARGET` explicitly declared in `CMakeLists.txt`; `cppcheck` linting added to CI.

---

## 3.0.1 (dev)

### Global orientation removed

The server-wide Orientation setting is gone. Each screen now owns its
own orientation; a brand-new screen defaults to landscape. The global
`orientation` field in `config.json` is dropped (schema bumped to v6 —
existing configs auto-migrate).

Dither previews — both the cached preview shown in the image grid /
detail modal and the on-the-fly preview in the Advanced dither panel —
are now rendered in the image's native orientation. A landscape photo
previews landscape, a portrait photo previews portrait, regardless of
any screen's setting.

## 3.0 beta6

### Upgrade fix

Upgrading the `.deb` package on a running server no longer fails with a
"port in use" error. The postinst script now stops the service before
installing new dependencies.

### Version shown in web app footer

The web app footer now displays the running server version (e.g. `3.0.0b6`),
making it easy to confirm which build is active without checking the command
line.

### AVIF and JPEG XL file extension fix

`.avif` and `.jxl` were missing from the internal list of recognised image
extensions. Files with these types now work correctly throughout the server,
not just at the upload endpoint.

---

## 3.0 beta5

### Per-screen orientation override

Each frame can now be locked to landscape or portrait independently of the global server setting. A frame mounted in portrait shows portrait-rendered images; another mounted in landscape shows landscape ones — both from the same library, without reconfiguring the server. Set and clear the override from the Screens tab in the web app. The server pre-renders both orientations for every image so serving the right one is instant.

### JPEG XL support

JPEG XL (`.jxl`) is now accepted at upload and converted like any other format. Requires `pillow-jxl-plugin`, which the `.deb` postinst installs automatically.

### Multi-face CLAHE protection

Face detection now returns all detected faces, not just the largest one. All detected bounding boxes are passed to the CLAHE preparation step as keepout regions. The boundary between each protected face region and the surrounding image is blended with a Gaussian feather (sigma proportional to the canvas size) so there is no hard edge at the face boundary. The feather width is controlled by `clahe_keepout_feather` in `ImageConfig` (default `0.015`, meaning 1.5% of the shorter canvas dimension).

### Face bounding box overlay in dither preview

The dither preview in the web UI now draws the face bounding boxes (as detected by YuNet) over the rendered preview image. The overlay updates live as you change the dither settings, making it easy to see whether a face region is being handled correctly.

### B&W palette LUT

A new LUT variant (`lut_name = "bw"`) restricts quantisation to Black and White only. This eliminates any possibility of a colour ink landing on a near-greyscale image — previously, compression noise in JPEG-encoded B&W photos could occasionally cause a pink or red speckle even when routed through the B&W pipeline. The B&W preset now uses this LUT by default.

### Panel cache compression

Rendered panel binaries (`.bin`) are now stored compressed with zstd level 1. Each 480 KB panel shrinks to roughly half that on disk with negligible CPU overhead. The decompression cost (microseconds) is absorbed in the HTTP response path. Old uncompressed `.bin` files in the cache are detected and re-queued for re-render automatically.

### Graceful HTTP 503 handling

When the server returns 503 (image pool empty, or all images still converting), the firmware now silently waits and retries at the server-specified interval rather than rendering a "no images" message on screen. The screen stays on its current image until new content is ready.

---

## 3.0 beta3–beta4

### B&W dithering: two-tone palette + classifier cache

- Added B&W detection pipeline and `image_config_bw` config slot.
- B&W detector samples a 200×200 thumbnail and checks 95th-percentile Lab chroma against `GRAYSCALE_CHROMA_THRESHOLD = 8.0`.
- Classifier results cached in `<cache_dir>/image_classifier.json` keyed by file sha1 — no re-detection on restart.
- Clearing the classifier cache now triggers an immediate sync instead of waiting for the next poll interval.

### JXL end-to-end fix

Registration of the JXL PIL plugin moved to the Flask app factory so JXL decodes correctly in all code paths (upload, sync, preview).

### Progress tracking fix

`_progress` is now reset at the start of `sync()` when the previous batch has finished. Previously, a server restart mid-batch left the progress counter stuck at a stale done/total pair until a new batch started.

### Upload error reporting

Full tracebacks for upload and image processing failures now appear in the server log.

---

## 3.0 beta1–beta2

---

## 3.0 alpha

### ~250× faster image conversion

The dither pipeline's inner pixel loop is now compiled to native code via **Numba JIT** (`@numba.njit(nogil=True)`). On a Raspberry Pi the full 3200×1600 panel converts in roughly **1–2 seconds** instead of 4–8 minutes. The GIL is released during the compiled loop, so multiple concurrent renders in the thread pool make genuine CPU progress instead of serialising.

The new default ditherer (`NumbaStreamingDither`) uses the same stripe-by-stripe memory model as before (≤ 50 MB peak), so memory usage is unchanged.

### Architecture: strategy-pattern dither + renderer

- **`AbstractDither`** ABC with four concrete implementations:
  - `StreamingDither` — pure-Python, stripe-based (slow, baseline)
  - `UnconstrainedDither` — pure-Python, full-canvas (~60 MB peak)
  - `NumbaStreamingDither` — JIT stripe-based, production default
  - `NumbaUnconstrainedDither` — JIT full-canvas, for quality comparisons
- **`AbstractImageRenderer`** / **`ImageRenderer`** — renderer takes any `AbstractDither` as a strategy; swapping the dither changes memory/speed without touching rendering logic.

### Face-aware dithering

YuNet face detection (OpenCV DNN) is now integrated into the pre-processing pipeline. When enabled, detected faces are used to bias chroma and sharpness enhancements toward the subject. Configurable via `face_detector` in `AppConfig`.

### Parallel render pool

`image_worker_thread_count` in `AppConfig` controls the number of threads in the render pool. Default 1; increase to overlap renders when multiple images are queued. Safe with `NumbaStreamingDither` because the JIT loop releases the GIL.

---

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
