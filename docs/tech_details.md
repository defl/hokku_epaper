# Technical Details

Architecture and implementation notes for the curious, the developer, and the next person to debug this thing. For a user-level overview see the [top-level README](../README.md).

## Contents

- [Server architecture](#server-architecture)
- [Frame firmware — state machine](#frame-firmware--state-machine)
- [Request / response protocol](#request--response-protocol)
- [REST API](#rest-api)
- [Storage and caching](#storage-and-caching)
- [Configuration](#configuration)
- [Packaging and service layout](#packaging-and-service-layout)
- [Reflashing a configured frame](#reflashing-a-configured-frame)

## Server architecture

1. Images dropped into the upload directory are converted to the 6-colour Spectra palette using perceptual Lab colour matching and the configured dither algorithm.
2. When a frame requests an image, the server picks the least-shown one (with random tie-breaking among ties) and serves it as a 960 KB binary — 480 K for panel 1, 480 K for panel 2. Newly-uploaded images take priority over the rotation.
3. The response includes two important headers:
   - `X-Sleep-Seconds` — how long to sleep until the next refresh.
   - `X-Server-Time-Epoch` — the server's wall-clock for the frame to `settimeofday()` from.
4. The frame's request carries an `X-Frame-State` JSON header with a full snapshot of its internal state (firmware version, boot count, wake cause, current regime, USB state, battery voltage, WiFi signal strength, free heap, clock drift, next scheduled refresh, last sleep error, WiFi cache hit). The server stores the whole dict per screen and renders it in the Details modal.

### Dither pipeline

Three algorithms, pickable from the config panel:

- **Atkinson + hue-aware** (default, "V10 recipe") — adaptive saturation + hue-constrained palette selection. Prevents the "warm skin tones cascade into blue speckle" and "white umbrella picks up pink noise" failure modes of naive diffusion.
- **Atkinson** — classic, softer texture, no hue correction.
- **Floyd–Steinberg** — full error cascade; consolidates small saturated features better but with the classic over-amplification risk on near-neutrals.

The pipeline uses measured palette values from a real panel (not theoretical sRGB) and dynamic range compression to the display's actual L* range. B&W auto-detection avoids a pink cast on grayscale inputs. Full walkthrough in [`dithering.md`](dithering.md).

## Frame firmware — state machine

Four regimes, selected on each boot from GPIO 14 (LOW = computer USB host detected, HIGH = no USB host — battery or a dumb wall charger):

```
                     button                         timer fires at
                     pressed                        scheduled time
                        │                                │
                        ▼                                ▼
    ┌─────────────┐  full reset  ┌──────────┐  fetch + display  ┌──────────┐
    │  USB_AWAKE  │─────────────▶│ REFRESH  │─────────────────▶ │ (back to │
    │             │              │          │                   │ regime)  │
    │ never       │◀─────────────┤          │                   │          │
    │ sleeps      │ USB plug,    └──────────┘                   └──────────┘
    │ logs on     │ NO refresh         ▲
    │ COM alive   │                    │
    └─────┬───────┘                    │
          │ USB unplugged               │ EXT1 wake on GPIO 1 (button)
          ▼                             │ or GPIO 14 (USB plug)
    ┌─────────────┐                     │ or timer
    │BATTERY_IDLE │                     │
    │ 5 s awake   │                     │
    │ window,     │─────────────▶  ┌─────────────┐
    │ logs off    │ window expires │ DEEP_SLEEP  │
    └─────────────┘                └──────────────┘
```

| Regime | Behaviour |
|---|---|
| `USB_AWAKE` | Full power, logging on, never deep-sleeps. Reflash-reachable indefinitely. |
| `BATTERY_IDLE` | 5 s awake window post-refresh, then deep sleep. Logging off. |
| `DEEP_SLEEP` | Timer wake on schedule; EXT1 wake on GPIO 1 (button) or GPIO 14 (USB plug). |
| `REFRESH` | Transient fetch + display, returns to the enclosing regime. |

Concrete facts (see [`firmware_design.md`](firmware_design.md) for the rationale behind each):

- Boot is never a refresh trigger. Only schedule, button, or first-ever install trigger a refresh.
- Button press → `esp_restart()` with `ACTION_REFRESH_FROM_BUTTON` in RTC memory → boot classifies as `WAKE_PENDING_ACTION` → fetch + display → return to whichever regime current USB state dictates.
- Schedule anchored to absolute server time: `next_refresh_epoch` stored as Unix epoch; sleep duration = `next_refresh_epoch − now_epoch`.
- Every failure path (WiFi, DNS, server, download) retries in 60 s.
- WiFi fast reconnect via BSSID + channel cached in RTC memory; reported as `wifi_cached: true`.
- All persistent state uses `RTC_NOINIT_ATTR` (not `RTC_DATA_ATTR`) so it survives `esp_restart`. A magic-value check in `app_main` handles first-power-on init.
- Up to 3 consecutive spurious resets are tolerated before the frame bails out to 60 s sleep.
- Error messages (config missing, version mismatch, download failure, WiFi auth failure) render on the e-paper itself.
- Display is cold-power-cycled on every refresh and initialised with a byte-for-byte match of the June 2025 factory init sequence. See [`hardware_facts.md`](hardware_facts.md) for the sequence itself.

## Request / response protocol

Every image fetch is one HTTP GET. The frame attaches state in headers; the server replies with the next image and timing metadata.

### Request headers (frame → server)

| Header | Purpose |
|---|---|
| `X-Frame-State` | JSON dict, see [below](#x-frame-state-fields) |
| (standard HTTP) | User-Agent identifies firmware version |

### Response headers (server → frame)

| Header | Purpose |
|---|---|
| `X-Sleep-Seconds` | Integer seconds until next refresh |
| `X-Server-Time-Epoch` | Unix epoch for frame wall-clock sync |
| `X-Image-Hash` | SHA-1 of the served image (for caching) |

### X-Frame-State fields

| Key | Meaning |
|---|---|
| `fw` | Firmware build timestamp `YYYYMMDDHHMMSSZ` |
| `boot` | Monotonic boot counter (survives deep sleep and `esp_restart`) |
| `wake` | Wake cause enum: `first_boot`, `pending_action`, `timer`, `button`, `usb_plug`, `spurious` |
| `regime` | Current regime: `usb_awake`, `battery_idle` |
| `uptime_s` | Seconds since this boot |
| `bat_mv` | Battery voltage in mV |
| `usb` | `true` if GPIO 14 reads LOW |
| `wifi_cached` | `true` if the fast-reconnect path was used |
| `last_sleep` | Seconds of the most recent deep sleep (`0` for first boot, `-1` for a post-`esp_restart` boot) |
| `rssi` | WiFi signal strength in dBm |
| `heap_kb` | Free heap in KB |
| `spurious` | Count of spurious resets since last clean boot |
| `cfg_ver` | NVS config schema version |
| `clk_now` | Unix epoch from the frame's current clock |
| `next_ep` | Anchored next-refresh epoch in RTC memory |
| `sleep_err_s` | Actual-vs-expected deep-sleep duration error, seconds |
| `seen_at` | (server-side) Timestamp when the server received this state |

## REST API

Every web GUI action is a JSON endpoint:

| Endpoint | Method | Purpose |
|---|---|---|
| `/hokku/api/status` | GET | Full server status: screens, images, stats |
| `/hokku/api/upload` | POST | Upload one or more images (multipart) |
| `/hokku/api/image/<name>` | DELETE | Remove an image + all cached derivatives |
| `/hokku/api/show_next/<name>` | POST | Force this image next on the upcoming refresh |
| `/hokku/api/original/<name>` | GET | Original upload (auto-JPEG-converted for browser preview) |
| `/hokku/api/thumbnail/<name>` | GET | 256 px thumbnail |
| `/hokku/api/dithered/<name>` | GET | Dithered preview PNG (as the screen renders it) |
| `/hokku/api/config` | GET/PATCH | Read / update server config |
| `/hokku/api/clear_cache` | POST | Clear dither cache — forces re-dither of everything |
| `/hokku/api/time` | GET | Server wall-clock — for the dashboard clock-drift display |
| `/spectra6/frame` | GET | The endpoint the frame hits: serves the next image + headers |
| `/spectra6/playlist` | GET | Debug view of serve order and current position |

## Storage and caching

- **Originals** stored untouched in the upload directory.
- **Dithered binaries** cached under a SHA-1 key derived from `(source_sha1, orientation, dither_algorithm)` — switching back to a previously-rendered variant is instant.
- **Preview PNGs** and **thumbnails** cached alongside.
- **Cache self-heals** on restart: stale entries are pruned when their source file changes or disappears.
- **Per-image stats** and **per-screen history** stored in `database.json` — show counts, last-displayed timestamps, and the full most-recent `X-Frame-State` per screen.
- **Config auto-migration** on upgrade — e.g. the retired `fs_hue_aware` dither is silently mapped to `atkinson_hue_aware` at load so old configs keep working.

## Configuration

Server configuration lives in one JSON file. Relevant keys:

| Key | Purpose |
|---|---|
| `timezone` | IANA zone, e.g. `Europe/Amsterdam` |
| `refresh_times` | List of `"HHMM"` strings, e.g. `["0600","1200","1800"]` |
| `orientation` | `landscape` or `portrait` |
| `dither_algorithm` | `atkinson_hue_aware`, `atkinson`, or `floyd_steinberg` |
| `poll_interval_s` | (advanced) server-side polling granularity |
| `debug_fast_refresh` | Overrides the schedule to 180 s intervals |

Frame configuration is stored in the ESP32-S3's NVS partition under the `hokku` namespace: WiFi SSID/password, server URL, screen name, and a `cfg_ver` schema version. Re-configurable via `hokku_setup.py` without rebuilding the firmware.

## Packaging and service layout

Debian package:

- Installs as `hokku-server` systemd service.
- Uses `DynamicUser=yes` for isolation — a per-run user with no home directory.
- `StateDirectory=hokku` gives the service `/var/lib/hokku/` for config, uploads, and the cache database.
- Upload directory: `/var/lib/hokku/upload/`.
- Cache directory: `/var/lib/hokku/cache/`.
- Config: `/var/lib/hokku/config.json` (webserver-owned so it can be edited from the web GUI).
- Legacy migration: existing `/etc/hokku/config.json` is read on first boot and migrated over.

Or run from source on any Python 3.9+ host — works on Linux, macOS, Windows, and probably a Raspberry Pi if you're so inclined.

## Reflashing a configured frame

The frame exposes its USB-serial interface whenever it's awake. There's no narrow timing window to hit — the moment USB is connected, the chip detects it (GPIO 14 goes LOW) and enters `USB_AWAKE`, which by definition never deep-sleeps.

- **USB plugged into a cold-shut frame:** the chip powers up into `USB_AWAKE` as soon as VBUS is detected and immediately becomes reachable.
- **Frame already awake (common):** just plug in and run the flasher.
- **If for some reason it's not responding:** press the button on the back. The restart path re-enumerates USB and gives the flasher a clean window.

The setup tool handles flashing automatically. For manual flashing with `esptool`, see [`firmware/README.md`](../firmware/README.md).
