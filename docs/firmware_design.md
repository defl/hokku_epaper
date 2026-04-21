This is an e-ink screen. It's an applicance that should have a smooth, reliable and predictable user experience.

Goals
=====
- Swap photos at a preset time from the server over Wifi
- Must be reliable, no boot loops, always must have at least a human scale window to intercept COM for re-flashing
- Button(s) must swap picture, nothing more

Hardware
========
- Extensive reverse engingeering has taken place
- Not everything is understood
- Startup and screen **must** remain as close as possible to the original firmware (June 2015 variant), deviations lead to crashes
- USB or no USB must be reliably detactable
- We're ok with some drift in clock because of the inherent inaccuracy of the crystal

Time
======
- Keep your own time and base sleeps etc off of this clock
- The server give absolute time every HTTP call, sync clock to it.
- Every HTTP call return your current time to the server, it will use it to calculate the error.

When connected to USB
=====================
- More current draw is ok, USB powers everything
- USB can/wilk charge the battery too. Blink the red LED if charging.
- Do not go into deep sleep, COM must remain connected
- When connecting to USB (from battery) do not change the image, only do that on button or when it's time
- Logging enabled

When running on battery
=======================
- Focus on low power usage
- Minimimze wifi time
- Go into deep sleep as often as you can, but **always** at least offer 5 seconds of window where a user can use COM to flashing
- Button must wake from deep sleep and refresh picture
- When disconnecting from USB do not change the image, only do that on button or when it's time
- No need for logging, we cannot see this anyway

USB <-> no USB
==============
- Making the USB connection must wake from deep sleep and move into the "When connected to USB" regime, but no refresh picture
- Breaking the USB connection must move into the "When running on battery" regime
- Do not swap the picture if it's not time yet, only swap the image on button press or when the server-given time says 

================================================================================
DESIGN — State Machine (added 2026-04-19, ties spec to verified hardware)
================================================================================

Hardware reality from probe testing (see hardware_facts.md "USB Detection"):
  - "USB connected" is detectable ONLY for COMPUTER-class hosts via GPIO 14.
  - Wall chargers / USB battery packs / dumb power-only sources are
    INVISIBLE to the firmware — they look exactly like "running on battery".
  - This is a hardware constraint we cannot work around.

Practical consequence: the spec's "When connected to USB" regime maps to
"GPIO 14 == LOW", which means specifically "computer USB host detected".
A wall charger plugged in still puts the device in the battery regime.
The battery is being charged in the background regardless. This is fine.

Signals
-------
| Signal      | Source                                     | Used for                                |
|-------------|--------------------------------------------|-----------------------------------------|
| usb_host    | gpio_get_level(14) == 0  (debounced ~200ms)| USB regime gate                         |
| btn_next    | gpio_get_level(1)  == 0  (debounced ~50ms) | "next image" command                    |
| sched_due   | (server_epoch_now >= next_refresh_epoch)   | Time-driven refresh                     |
| sleep_err   | actual_sleep - expected_sleep              | Diagnostic only, sent in X-Frame-State  |

GPIO 12 (PWR_BUTTON) transitions in lockstep with GPIO 14 on USB plug;
do NOT poll it as a button independently. Either ignore it entirely or
debounce against GPIO 14 transitions. Keep GPIO 12 out of EXT1 wake mask
unless we explicitly want USB-plug to wake the chip via that path
(GPIO 14 in the wake mask is the cleaner way).

States
------
  USB_AWAKE        : usb_host==1; full power, log on, no sleep, no refresh except on triggers
  BATTERY_IDLE     : usb_host==0; lightweight loop awake just long enough to honour
                     the spec's 5-second reflash window, then transition to DEEP_SLEEP
  DEEP_SLEEP       : timer + EXT1 wake configured for sched_due / btn_next / usb_host edge
  REFRESH          : transient — fetch image, display it, return to whatever was the
                     enclosing state (USB_AWAKE or back through BATTERY_IDLE -> DEEP_SLEEP)

Initial state on boot
---------------------
  POR or RTC stale  -> sample usb_host once (no debounce — it's the very first read)
                       -> if usb_host: USB_AWAKE
                       -> else:        BATTERY_IDLE
                       NEVER auto-refresh on boot. Boot is not a refresh trigger.

  Wake from deep sleep -> read wake cause:
                          - timer + sched_due  -> REFRESH (then return to enclosing state)
                          - EXT1, GPIO 1 LOW   -> REFRESH (button), then enclosing
                          - EXT1, GPIO 14 LOW  -> USB_AWAKE (NO refresh — spec)
                          - EXT1, both         -> ambiguous; treat as USB_AWAKE, no refresh
                          - any other          -> classify as spurious; back to DEEP_SLEEP

  Software-restart (esp_restart from a recovery valve) -> use rtc_magic-validated
                                                          state to return to the regime
                                                          that was active before restart

Transitions
-----------
  USB_AWAKE:
    usb_host goes 1->0  -> BATTERY_IDLE   (spec: don't refresh, just switch regime)
    sched_due           -> REFRESH        (then back to USB_AWAKE)
    btn_next            -> REFRESH        (then back to USB_AWAKE)

  BATTERY_IDLE:
    usb_host goes 0->1  -> USB_AWAKE      (spec: don't refresh, just switch regime)
    btn_next            -> REFRESH        (then back to BATTERY_IDLE -> DEEP_SLEEP)
    awake_window expires -> DEEP_SLEEP    (with sched_due timer + EXT1 mask armed)

  DEEP_SLEEP:
    timer fires AND sched_due -> wake to REFRESH
    EXT1 GPIO 14 -> wake, USB_AWAKE
    EXT1 GPIO 1  -> wake to REFRESH (button)

  REFRESH:
    success            -> back to enclosing regime
    BUSY-stuck failure -> log loudly, do NOT auto-restart (only user-action recovers)

Awake-window contract (BATTERY_IDLE)
------------------------------------
- Spec minimum: 5 seconds of "device is awake and serial-attachable" before
  we commit to deep sleep. We use this window to:
  - Finish any in-progress fetch/refresh
  - Honor an immediate button press
  - Be reachable for esptool reflash if a USB host appears
- If usb_host transitions to 1 during this window, immediately switch to
  USB_AWAKE — never deep-sleep with a host attached (spec).

USB_AWAKE awake forever contract
--------------------------------
- The spec is explicit: "Do not go into deep sleep, COM must remain connected".
- This is one process lifetime. NO `esp_restart` inside the USB regime to
  schedule the next refresh.
- This was a real bug in the previous firmware: USB regime called esp_restart
  every cycle, breaking the COM session.

USB_AWAKE event source: poll vs interrupt
-----------------------------------------
- We need to react to: usb_host edge, btn_next edge, sched_due (time-based).
- **Decision: pure polling at 100 ms.** Reasons:
  - We're on USB. Current draw doesn't matter. The cost of a 100 ms
    `vTaskDelay` loop is trivial.
  - Worst-case latency is 100 ms for any event — well under any human-noticeable
    threshold for a photo-frame button.
  - GPIO ISR + queue + ISR-safe handlers + IRAM_ATTR placement is meaningfully
    more code surface and adds failure modes (ISR running before flash mapped
    on early boot, ISR-safe API discipline, queue overflow).
  - sched_due is intrinsically time-based — even with GPIO interrupts we'd
    still need a timer. Mixing the two paths just to save 100 ms of latency
    isn't worth it.
- One simple FreeRTOS task in USB_AWAKE:
    while (in USB_AWAKE) {
        if (gpio_get_level(USB_HOST_DETECT) == 1) -> transition to BATTERY_IDLE
        if (btn_next_pressed_debounced())         -> trigger button-reboot path (see below)
        if (server_epoch_now() >= next_refresh)   -> trigger refresh
        vTaskDelay(100ms)
    }
- BATTERY_IDLE awake window uses the same 100 ms poll loop — power-irrelevant
  (the window is at most 5 s on battery and we want fast button response).
- Interrupts are reserved for the one place they're actually needed: EXT1
  wake out of deep sleep (which is hardware, not software-managed).

Button = full reboot
--------------------
- **Decision: yes — button press triggers `esp_restart()`, with a flag in
  RTC memory telling the next boot to do a refresh.** This gives us a clean
  fresh-state path through every button press.

  Flow:
    1. Button press detected (in USB_AWAKE poll loop, BATTERY_IDLE poll loop,
       OR via EXT1 wake from DEEP_SLEEP)
    2. Set `RTC_NOINIT_ATTR pending_action = ACTION_REFRESH_FROM_BUTTON`
    3. Call `esp_restart()`
    4. On boot, app_main reads `pending_action`:
       - if `ACTION_REFRESH_FROM_BUTTON` → fresh-init everything, fetch +
         display, then return to enclosing regime (USB_AWAKE or BATTERY_IDLE)
         and clear the flag
       - else → normal boot path

  Trade-offs:
    - On USB: COM session DROPS for ~2-3 s while the chip resets and USB
      re-enumerates. The spec says "COM must remain connected" — but a button
      press is a deliberate user action and the drop is intentional. We accept
      this as the cost of the bulletproof recovery property.
    - On battery: identical to a normal EXT1 wake, no extra cost.

  Why this is worth doing:
    - Wedged display controllers, stuck WiFi state, leaked memory, half-finished
      HTTP transactions — all of them get cleared by the reset. The user has
      a one-click escape valve from any pathological state.
    - Same code path for "scheduled wake" and "button wake" on battery (both
      come up via boot → check pending_action). Less branching in the refresh
      logic.
    - No race conditions between "in-progress refresh" and "user pressed
      button mid-refresh". The reset always wins.

  Boot-loop guard:
    - `pending_action` is cleared early in app_main, BEFORE we attempt the
      refresh. If the refresh crashes / causes a watchdog reset, on the next
      boot the flag is gone and we fall through to normal startup. We don't
      enter a button-triggered crash loop.
    - This is in addition to the existing `consecutive_busy_timeouts` and
      `consecutive_spurious_resets` caps which already exist.

  EXT1 wake-from-sleep behaviour with this design:
    - GPIO 1 LOW (button) wakes chip → app_main runs → wake-cause classifier
      sets pending_action = ACTION_REFRESH_FROM_BUTTON → REFRESH → return to
      BATTERY_IDLE → DEEP_SLEEP. Same as a software esp_restart-after-press.
    - GPIO 14 LOW (USB plug) wakes chip → wake-cause classifier sees
      USB_HOST_DETECT = LOW → transition straight to USB_AWAKE, no refresh.

Logging
-------
- USB_AWAKE: ESP_LOG_LEVEL_INFO. Visible on COM.
- BATTERY_IDLE / DEEP_SLEEP / REFRESH-from-battery: ESP_LOG_LEVEL_NONE
  (per spec: no need for logging on battery, can't see it anyway).
  Switch via `esp_log_level_set("*", LEVEL)` at the moment of regime
  transition. Don't use compile-time gating — we need both regimes in
  the same image since they alternate at runtime based on usb_host.

Refresh path (regime-independent)
---------------------------------
- HTTP fetch with X-Frame-State header (current behavior).
- On response: settimeofday() from X-Server-Time-Epoch; compute next
  refresh epoch from X-Sleep-Seconds + server_epoch_now (anchor to
  absolute server time, not relative-to-now — eliminates the +60s
  awake-window drift bug).
- Display via cold-boot sequence (current display_dual + June init).
- Done.

Sleep duration calculation
--------------------------
  next_refresh_epoch = server_epoch_at_download + sleep_seconds_from_server
  remaining_us       = (next_refresh_epoch - now_epoch) * 1_000_000
  enter_deep_sleep(remaining_us)
- Floors at 5 s (spec minimum awake window before sleep).
- The conversion server-epoch -> RTC-µs uses the firmware's wallclock that
  was set by settimeofday(). RTC slow clock is the timing backbone.

Boot-loop guards (already in current firmware, keep)
----------------------------------------------------
- `consecutive_spurious_resets` (cap 3) — RTC_NOINIT_ATTR
- `consecutive_busy_timeouts` (cap 3) — RTC_NOINIT_ATTR
- `rtc_magic` validation — already correct after the RTC_NOINIT_ATTR fix
- (Removed) 120 s reflash-window-on-every-boot rule — superseded by the
  state machine: USB_AWAKE never sleeps so it's always reachable; BATTERY_IDLE
  uses the spec's 5 s minimum window. To reflash a battery-powered frame,
  plug into USB which transitions to USB_AWAKE.

What gets removed vs current firmware
-------------------------------------
- The unconditional refresh-on-every-boot. Boot is not a refresh trigger.
- The USB-polling loop with esp_restart. Replaced by a single long-lived
  USB_AWAKE task.
- The "60 s LED-on awake window" on battery. Replaced by 5 s minimum,
  LED off (already done).
- The DRF-timing reboot valve. Already removed.
- All RTC_DATA_ATTR. Already converted to RTC_NOINIT_ATTR.

What gets added
---------------
- Single explicit state machine (USB_AWAKE / BATTERY_IDLE / DEEP_SLEEP /
  REFRESH) instead of the implicit linear flow.
- usb_host signal driving regime transitions, debounced.
- GPIO 14 as EXT1 wake source.
- Schedule-anchored sleep (epoch-based, not timer-from-now).
- Runtime log-level gating on regime.
- Boot path that does NOT auto-refresh.

