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
| `firmware_url.c/.h` | derive the model-tagged firmware endpoint from the server base URL |
| `frame_state.c/.h`  | build the `X-Frame-State` telemetry JSON from a `frame_state_t` |
| `json_util.c/.h`    | `json_escape()` — minimal JSON string escaper |

Each firmware compiles these sources directly (ESP-IDF boards add them to their
`main` component's `SRCS`; the F7 adds them to its Makefile source list) and the
host-test suites `#include` them like any other unit-under-test.
