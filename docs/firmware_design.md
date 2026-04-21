# Firmware Design — Rationale

This is the "why" doc. For *what* the firmware actually does — state diagram, protocol, RTC fields, API — see [`tech_details.md`](tech_details.md). For the hardware reference — pin map, init bytes, registers — see [`hardware_facts.md`](hardware_facts.md).

## Goals

This is an e-ink screen. It's an appliance that should have a smooth, reliable and predictable user experience.

- Swap photos at a preset time from the server over WiFi.
- Must be reliable — no boot loops, always must have at least a human-scale window to intercept COM for re-flashing.
- Button(s) must swap the picture, nothing more.

## Constraints that shaped the design

### Hardware

- Extensive reverse-engineering has taken place; not everything is understood.
- Startup and display sequence *must* remain as close as possible to the original firmware (June 2025 variant) — deviations lead to crashes and a wedged controller.
- We're OK with some drift in the clock due to the inherent inaccuracy of the crystal — the server is authoritative.

### USB detection is not USB-power detection

From probe testing (see [`hardware_facts.md`](hardware_facts.md) → "USB Detection"): GPIO 14 reliably reports "computer USB host detected". But wall chargers, USB battery banks, and dumb power-only sources do **not** trigger it — they look identical to "running on battery" as far as the firmware can tell.

Consequence: the spec's "When connected to USB" regime is really "computer host present via USB". A wall charger plugged in still puts the device in the battery regime; the battery is being charged in the background regardless. This is fine — a wall charger has no workflow that needs the device "awake on USB".

### GPIO 12 + GPIO 14 are paired

GPIO 12 (PWR_BUTTON) transitions in lockstep with GPIO 14 on USB-plug events, independent of any actual button press. So GPIO 12 can't be polled as an independent button source — either ignore it entirely or debounce it against GPIO 14 edges. In this firmware we ignore it for button polling and use GPIO 1 (BUTTON_1) as the sole "next image" button.

## Regime philosophy

### When connected to USB (USB_AWAKE)

- Higher current draw is fine — USB powers everything.
- USB will charge the battery in the background. Blink the red LED while a USB host is present.
- Do **not** deep-sleep — COM must remain connected for re-flashing and debugging.
- When *transitioning* to USB (plug-in), **do not** change the image. Only swap on button or on schedule.
- Logging enabled.

### When running on battery (BATTERY_IDLE → DEEP_SLEEP)

- Focus on low power use; minimize WiFi time.
- Deep-sleep as often as possible, but **always** offer at least 5 s of awake window for a user to plug in and start flashing.
- Button must wake from deep sleep and refresh the picture.
- When *transitioning* off USB (unplug), **do not** change the image.
- No logging — nobody can see it anyway.

## Key design decisions

### USB_AWAKE event source: polling, not interrupts

We need to react to three events in the USB regime: a USB-unplug edge, a button edge, and the scheduled refresh time firing. We chose **pure polling at 100 ms**:

- We're on USB. Current draw is irrelevant. A 100 ms `vTaskDelay` loop costs nothing.
- Worst-case latency is 100 ms — well under any human-noticeable threshold for a photo-frame button.
- GPIO ISR + queue + ISR-safe handlers + `IRAM_ATTR` placement is meaningfully more code surface and introduces failure modes (ISR firing before flash mapped on early boot, ISR-safe API discipline, queue overflow).
- The scheduled-refresh check is intrinsically time-based — even with GPIO interrupts we'd still need a timer. Mixing the two paths just to shave 100 ms of latency isn't worth it.

The BATTERY_IDLE awake window uses the same 100 ms poll loop. The window is at most 5 s on battery and we want fast button response — power is irrelevant for that short a window.

Interrupts are reserved for the one place they're actually needed: EXT1 wake out of deep sleep, which is managed by hardware, not software.

### Button press = full chip restart

The button triggers `esp_restart()` with an `ACTION_REFRESH_FROM_BUTTON` flag in RTC memory. The next boot reads the flag, does the refresh, then returns to whichever regime matches current USB state.

Trade-off:
- On USB, the COM session drops for ~2–3 s while the chip re-enumerates. The spec says "COM must remain connected" — but a button press is a deliberate user action and the drop is intentional.

Why it's worth doing:
- Wedged display controllers, stuck WiFi state, leaked memory, half-finished HTTP transactions, confused queues — all get cleared by the reset. The user has a one-click escape valve from any pathological state.
- Same code path for "scheduled wake" and "button wake" on battery — both come up via boot → check `pending_action`. Less branching in the refresh logic.
- No race conditions between "in-progress refresh" and "user pressed button mid-refresh". The reset always wins.

Boot-loop guard: `pending_action` is cleared early in `app_main`, *before* the refresh attempt. If the refresh crashes or triggers a watchdog reset, the flag is already gone and the next boot falls through to normal startup.

### Schedule anchored to absolute server time

Earlier firmwares computed sleep as "relative to now", which accumulated `+60 s` of awake-window drift per cycle. The current firmware stores `next_refresh_epoch` (Unix seconds, server-provided) in RTC-NOINIT memory and computes `remaining = (next_refresh_epoch − now_epoch) * 1e6`. RTC slow-clock drift between cycles washes out because we re-anchor on every server response.

Floored at 5 s minimum (spec awake window) before entering sleep.

### RTC state uses `RTC_NOINIT_ATTR`, not `RTC_DATA_ATTR`

`RTC_DATA_ATTR` re-runs its initializer on every `esp_restart`, silently wiping any counter you put in it. `RTC_NOINIT_ATTR` does not — the memory persists through both deep sleep *and* software restarts. A magic-value check in `app_main` handles first-power-on init exactly once.

### Logging level switched at runtime

`esp_log_level_set("*", LEVEL)` is called at every regime transition:
- `USB_AWAKE`: `ESP_LOG_LEVEL_INFO`.
- `BATTERY_IDLE` / `DEEP_SLEEP` / `REFRESH`-from-battery: `ESP_LOG_LEVEL_NONE`.

Compile-time gating isn't an option — both regimes need to coexist in the same image since they alternate at runtime based on USB state.

## Reflash reachability

There is no narrow timing window. `USB_AWAKE` never deep-sleeps, so any time a computer USB host is connected the chip is reachable. The first moment VBUS + host are detected, GPIO 14 goes LOW, the boot classifier picks `USB_AWAKE`, and the device stays alive.

If for some reason the frame is unresponsive, pressing the button triggers a full chip restart — which re-enumerates USB and gives the flasher a clean window.

The older "120 s reflash window on every boot" rule was removed. The state machine obsoletes it: `USB_AWAKE` never sleeps, `BATTERY_IDLE` has the spec's 5 s minimum, and the path from battery to reflash is "plug in USB → chip transitions to `USB_AWAKE`".
