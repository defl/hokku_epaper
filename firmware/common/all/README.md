# firmware/common/all

SoC-agnostic C shared by **all** hokku firmwares — both ESP32/ESP-IDF boards
(`huessen_epf1301`, `seeedstudio_e1004`) **and** the XR872 F7 (`bigme_f7`),
which uses a completely different SDK.

## The contract (strict)

Code here MUST be **pure C** with **no platform headers** — no ESP-IDF
(`esp_*`, `freertos/*`, `driver/*`) and no XR872 SDK (`kernel/os/*`,
`HTTPClient.h`, `image/*`, …). Only the C standard library
(`stdint`/`stddef`/`string`/`stdio`/`stdbool`).

The way this stays true: the caller gathers all platform-specific values from
its own SDK (WiFi RSSI, free heap, wall clock, battery mV, …) and passes them
in — e.g. `frame_state_build()` takes a filled `frame_state_t`, it never calls
`esp_wifi_*` or `wlan_sta_*` itself. That's what lets the same object file link
into three firmwares built by two different toolchains, and guarantees they all
produce identical wire output.

Anything that needs a platform API (HTTP transport, OTA, NVS/FDCM, GPIO, the
panel driver) does **not** belong here. ESP-IDF-specific shared code lives in
[`../esp32/`](../esp32/) instead (shared only between the two ESP32 boards).

## Modules

| file | what |
|---|---|
| `backoff.c/.h`      | exponential retry interval for consecutive failures |
| `fetch_outcome.c/.h` | what a screen does with the server's reply to an image fetch (display / keep the picture / back off), the shared retry constants, the reply's header parsers and the exact image-size check |
| `firmware_url.c/.h` | derive the model-tagged firmware endpoint from the server base URL |
| `frame_proto.c/.h`  | serial `frame` upload protocol: push a full panel buffer over the console (not used by `seeedstudio_e1004`) |
| `frame_state.c/.h`  | build the `X-Frame-State` telemetry JSON from a `frame_state_t`; the battery plausibility range |
| `http_headers.h`    | the protocol's request/response header names |
| `interactive.c/.h`  | USB-interactive mode policy: no refresh, poll or sleep while a host drives the console (not used by `seeedstudio_e1004`) |
| `json_util.c/.h`    | `json_escape()` — minimal JSON string escaper |
| `logbuf.c/.h`       | circular log buffer under the ESP32 and F7 loggers |
| `messages.c/.h`     | the on-glass messages (outage, config, OTA), when to draw them, and their layout |
| `ota_confirm.c/.h`  | when a freshly OTA'd image confirms itself or rolls back (first fetch that reaches the server; bounded retries) |
| `schedule.c/.h`     | next fetch time (anchored to the server clock), outage streak, drift calibration learned against the server clock |
| `screen_ident.c/.h` | format the `X-Screen-Mac` string; validate the server's name for the screen (response `X-Screen-Name`) |
| `sleep_cal.c/.h`    | learned deep-sleep oscillator-drift correction (ppm) |
| `text_render.c/.h`  | 5x7 bitmap text into the 4bpp panel format, as a framebuffer or row by row |

Each firmware compiles these sources directly (ESP-IDF boards add them to their
`main` component's `SRCS`; the F7's Makefile picks up the whole directory) and the
host-test suites `#include` them like any other unit-under-test.
