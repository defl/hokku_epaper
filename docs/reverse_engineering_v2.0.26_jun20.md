# E_Frame stock firmware v2.0.26 (June 20 2025) — reverse-engineering notes

This is the OTA-updated build pulled off a real device in the wild. On most Hokku units this is the firmware actually running — the factory `ota_0` slot still has v2.0.19, but once the device has talked to the vendor's update server it flashes v2.0.26 into `ota_1` and switches to it.

Read [`reverse_engineering_overview.md`](reverse_engineering_overview.md) first for the hardware and tooling background, and [`reverse_engineering_v2.0.19_apr21.md`](reverse_engineering_v2.0.19_apr21.md) for the baseline this version is diffed against. This doc focuses on what v2.0.26 changed and why those changes matter to our firmware.

---

## How we captured this version

The device we analysed had already been auto-updated. Checking `otadata.bin` at `0x0000d000` showed sequence number 2, meaning `ota_1` was the active slot. We dumped both slots with `esptool`:

```bash
esptool.py --chip esp32s3 -p COM3 read_flash 0x010000 0x180000 \
    ota_0_v2.0.19_apr21_inactive.bin
esptool.py --chip esp32s3 -p COM3 read_flash 0x190000 0x180000 \
    ota_1_v2.0.26_jun20_ACTIVE.bin
esptool.py --chip esp32s3 -p COM3 read_flash 0x00d000 0x002000 otadata.bin
```

The `otadata.bin` has two 32-byte entries. Ours read:

```
seq #1  → ota_0, CRC valid
seq #2  → ota_1, CRC valid   ← this is the active one (highest seq)
```

So `ota_1` wins; `ota_0` is retained but dormant (handy for rollback). The image header of `ota_1` gives us the version:

```
Project name:  E_Frame
App version:   2.0.26
Compile time:  2025-06-20
ESP-IDF:       v5.2.2 (same as v2.0.19)
Source path:   D:/Project/ESP32/Eink/ (same)
```

### Files in `.private/v2.0.26_jun20/`

```
ota_0_v2.0.19_apr21_inactive.bin   — the old April build, no longer active
ota_1_v2.0.26_jun20_ACTIVE.bin     — the June build that's running
otadata.bin                         — OTA slot selector showing seq=2
drom.bin  (@ 0x3c100020)
irom.bin  (@ 0x42000020)
iram.bin  (@ 0x40374000)
seg_0 through seg_6 .bin            — raw per-segment extractions
```

---

## Ghidra project

`.private/ghidra_proj/hokku_jun20.gpr` is a Ghidra 12.0.4 project with the `ota_1` image pre-loaded and analyzed. Memory regions are configured via `.private/ghidra_scripts/SetupESP32S3Memory.java` which maps:

- `drom.bin` at `0x3c100020`
- `irom.bin` at `0x42000020`
- `iram.bin` at `0x40374000`
- plus the two DRAM segments at their correct bases

`.private/ghidra_scripts/DumpDisplayFunctions.java` is the post-analysis script that walks a list of string-referenced functions, finds each function by its reference to a known string (`"Write PON"`, `"TSC Data"`, `"app_spi_init"`, etc.), and dumps the decompilation. Its output is `.private/ghidra_output_jun20.txt` — the primary artifact this document is built on.

`.private/ghidra_extra.txt` holds decomps for a few helper functions (`hardware_reset`, `ctrl_pin_setter`, `vTaskDelay_ms`, `epaper_cmd_data`) that the main script missed because they're referenced indirectly.

---

## Function map — June build

Because v2.0.26 was compiled separately, function addresses have shifted from v2.0.19. Here are the June-build addresses we use throughout:

| Address | Our name | Maps to v2.0.19 function |
|---|---|---|
| `0x4200acb0` | `display_update(buffer)` | `0x4200acac` |
| `0x4200b45c` | `app_main()` | `0x4200b418` |
| `0x4200b9b0` | `ctrl_pin_setter(level)` | `0x4200b96c` |
| `0x4200b9c8` | `hardware_reset()` | `0x4200b984` |
| `0x4200b9e8` | `display_init()` | `0x4200b9a4` |
| `0x4200bc98` | `display_refresh()` | `0x4200bc70` |
| `0x4200bdb0` | `read_tsc()` | *(new in v2.0.26 — see below)* |
| `0x4200be0c` | `send_panel(which, ptr, len)` | *(new wrapper — see below)* |
| `0x4200bf7c` | `spi_read_bytes(ptr, len)` | *(new — called by `read_tsc`)* |
| `0x4200bfb4` | `gpio_set_level_wrapper(pin, lvl)` | the local wrapper around `esp_idf_gpio_set_level` |
| `0x4200c224` | `combined_app_init()` | *(not covered in v2.0.19 doc; full boot-time GPIO + SPI + I2C + ADC bring-up)* |

Addresses are still in IROM based at `0x42000020`.

---

## Behaviour changes vs v2.0.19

Three substantive changes between v2.0.19 and v2.0.26, in decreasing order of how much they matter for our firmware:

1. **New post-refresh shutdown sequence in `display_update`.** (Biggest delta.)
2. **Init command data tweaks and one new command.**
3. **TSC (internal temperature sensor) read wired into every `send_panel` call.**

Each is described in its own section below. After that, the sections on `combined_app_init` and the boot-time GPIO setup are included because they're where we pinned down the GPIO 3 (EPAPER_PWR_EN) behaviour that was unclear in v2.0.19.

---

## Change 1 — post-refresh shutdown sequence

**This is the single most important finding from the v2.0.26 analysis.** It's also the one that was missing from every previous pass — Capstone had mis-disassembled the tail of `display_update` as data (literal pool alignment bug), so this code was invisible to the first two rounds of analysis. Ghidra decompiled it correctly.

From `FUN_4200acb0`:

```c
void display_update(int buffer) {
    int scratch_size = DAT_420009e4;            // 480000
    memset(scratch_buf_from_DRAM, 0, scratch_size);

    // Pre-refresh — identical to v2.0.19
    gpio_set_level(17, 1);                       // SYS_POWER HIGH
    vTaskDelay_ms(10);
    hardware_reset();                            // RST 100 ms low / 100 ms high
    ctrl_pin_setter(1);                          // CTRL1=1, CTRL2=1

    display_init();                              // same role, different init bytes — see Change 2
    send_panel(0, buffer + 0,            scratch_size);
    send_panel(1, buffer + scratch_size, scratch_size);
    display_refresh();                           // PON / DRF / POF, also slightly updated

    // ──── NEW: graceful shutdown of all signal lines ────
    gpio_set_level(9,    0);   // SCLK       → LOW
    gpio_set_level(7,    0);   // BUSY       → LOW  (input-configured pin; no-op but present in stock)
    gpio_set_level(0x29, 0);   // MOSI  (41) → LOW
    gpio_set_level(0x28, 0);   // BUTTON_2 (40) → LOW  (input; no-op)
    gpio_set_level(0x27, 0);   // BUTTON_3 (39) → LOW  (input; no-op)
    gpio_set_level(0x26, 0);   // WIFI_LED (38) → LOW

    vTaskDelay_ms(1000);       // ── 1 FULL SECOND dwell ──

    gpio_set_level(0x12, 0);   // CTRL1 (18) → LOW
    gpio_set_level(8,    0);   // CTRL2      → LOW
    gpio_set_level(6,    0);   // RST        → LOW   (display INTO reset)
    gpio_set_level(0x11, 0);   // SYS_POWER (17) → LOW  (rail off)
    return;
}
```

### Why this matters

What v2.0.19 did at the end of a refresh was: `vTaskDelay_ms(10); gpio_set_level(17, 0); return;`. Ten milliseconds later, SYS_POWER is cut. At the moment power drops, SCLK / MOSI / CTRL pins are whatever the SPI driver last left them — potentially HIGH. The UC8179C's digital inputs have protection diodes to its own VCC rail. If any of those inputs sit ~3.3 V while VCC is dropping to 0 V, the diodes can forward-bias, source current backward into the controller, and drop the chip into an undefined analog state. The controller appears to then retain that state across the next power-up, because its internal POR is triggered by the VCC rail crossing a threshold and if the rail doesn't get below that threshold (because diode-biasing is holding it up weakly), the controller never sees a POR.

That's the mechanism. The fix is what v2.0.26 does:

1. Drive every non-power digital line LOW *first*, so nothing is a 3.3 V source when VCC starts dropping.
2. Hold that state for a full second. Enough time for anything capacitively coupled to settle.
3. *Then* drop CTRL1, CTRL2, and RST LOW. RST-LOW-before-power-off forces the UC8179C into its datasheet-defined hardware-reset state.
4. *Last*, drop SYS_POWER LOW. The rail falls; the controller hits POR cleanly.

Empirically, this is what makes the difference between "stuck state persists across our firmware's reboots" and "stuck state clears the next refresh." Our firmware now does this sequence byte-for-byte (see `epaper_display_dual` in `firmware/main/main.c`).

### Caveats in matching it

- `gpio_set_level(7, 0)` — GPIO 7 is BUSY, configured as input with pull-up. Calling `gpio_set_level` on an input-configured pin is a no-op in ESP-IDF (the output latch is written but not driven). The stock firmware does it anyway; we do too, for consistency, even though it has no physical effect.
- `gpio_set_level(0x28, 0)` and `(0x27, 0)` — BUTTON_2 and BUTTON_3 are inputs. Same no-op. The stock firmware probably has these pins re-purposed as outputs in some other build path, or this is leftover from an earlier revision. Either way it's harmless.
- MOSI/SCLK — in ESP-IDF these pins belong to the SPI peripheral via the GPIO matrix. Calling `gpio_set_level` on them detaches the pin from SPI and drives it directly. The stock firmware must be re-attaching them on the next refresh when it calls the SPI driver again; we verified our firmware survives this too (subsequent refreshes use SPI as normal).

---

## Change 2 — init command deltas

Same 18 commands structurally, but the data bytes for three of them changed, one new command was inserted, and two were removed. Net: 17 commands in v2.0.26 vs 18 in v2.0.19.

| Cmd | v2.0.19 data | v2.0.26 data | Change |
|---|---|---|---|
| `0x74` | `C0 1C 1C CC CC CC 15 15 55` | same | |
| `0xF0` | `49 55 13 5D 05 10` | same | |
| `0x00` PSR | `DF 69` | **`DF 6B`** | one bit flipped (bit 1 of low byte) |
| **`0x30` PLL_CONTROL** | *(not sent)* | **`08`** | **ADDED** — slotted in between `0x00` and `0x50` |
| `0x50` | `F7` | same | |
| `0x60` | `03 03` | same | |
| `0x86` | `10` | same | |
| `0xE3` | `22` | same | |
| `0xE0` | `01` | same | |
| *— phase A ends —* | | | |
| `0x61` TCON_RES | `04 B0 03 20` | same (1200×800) | |
| `0x01` POWER_SETTING | `0F 00 28 2C 28 38` | same | |
| `0xB6` | `07` | same | |
| `0x06` BOOSTER | `E8 28` | **`D8 18`** | booster soft-start timing altered |
| `0xB7` | `01` | same | |
| `0x05` POWER_ON_MEAS | `E8 28` | **`D8 18`** | POM timing altered the same way |
| `0xB0` | `01` | same | |
| `0xB1` | `02` | same | |
| `0xA4` CASCADE | `83 00 02 00 …` | *(removed)* | gone in June |
| `0x76` | `00 00 00 00 04 …` | *(removed)* | gone in June |

### What the changes seem to mean

The pattern reads as a vendor bug-fix of the init sequence rather than a feature addition:

- **`0x00` PANEL_SETTING bit flip from `0x69` to `0x6B`.** Bit 1 of the low byte controls one of the waveform/scan direction options in the UC8179C. A deliberate spec-level tweak, probably a customer-reported ghosting issue.
- **New `0x30` command with data `0x08`.** `0x30` is PLL_CONTROL per the UC8179C datasheet; `0x08` selects a specific frame-rate / PLL divisor. This being *added* in v2.0.26 suggests v2.0.19 was relying on reset defaults and the fix is an explicit set. Our firmware now sends it.
- **`0x06` and `0x05` both change `E8 28 → D8 18`.** These are booster soft-start (`0x06`) and power-on measurement (`0x05`). The bit pattern `E8 → D8` drops one bit in the "strength" nibble and `28 → 18` drops one in the "period" nibble. Softer booster profile — possibly a fix for inrush current on some panel batches.
- **`0xA4` and `0x76` removed.** `0xA4` is CASCADE_SETTING in the datasheet. Its removal together with the unknown `0x76` suggests the cascade configuration was reworked; possibly the dual-panel topology is now handled entirely by the phase-A/phase-B CTRL pin dance rather than an explicit cascade register. We don't need to understand it fully — we just copy.

### Phase structure in v2.0.26

Phases are slightly different because `0x61` moved from phase B in v2.0.19 to phase A in v2.0.26:

- **Phase A** (10 cmds, both panels, CTRL1=0 / CTRL2=0): `0x74, 0xF0, 0x00, 0x30, 0x50, 0x60, 0x86, 0xE3, 0xE0, 0x61`
- **Phase B** (7 cmds, panel 1 only, CTRL1=0 / CTRL2=1): `0x01, 0xB6, 0x06, 0xB7, 0x05, 0xB0, 0xB1`

Our firmware matches this split.

### `display_refresh` in v2.0.26

The PON / DRF / POF sequence is structurally identical to v2.0.19 — same opcodes, same data bytes, same 30 ms pre-delay before DRF. No changes.

---

## Change 3 — TSC read per panel

The stock firmware reads the UC8179C's internal temperature sensor at the top of every `send_panel` call. The new function is at `0x4200bdb0`:

```c
void read_tsc(void) {
    uint16_t local_result = 0;
    gpio_set_level(0x12, 0);         // CTRL1 LOW  → select panel 1 only
    epaper_cmd_only(0x40);           // TSC command — "read temperature"

    // BUSY wait, same ~20 s timeout as elsewhere
    int16_t counter = 0x7d1;
    while (counter-- > 0) {
        if (gpio_get_level(7) != 0) break;
        vTaskDelay_ms(10);
    }

    spi_read_bytes(&local_result, 2);  // raw 2-byte SPI read, no cmd prefix
    gpio_set_level(0x12, 1);           // CTRL1 HIGH  → deselect

    printf("TSC Data = 0x%02X, 0x%02X\r\n",
           (uint8_t)(local_result & 0xFF),
           (uint8_t)(local_result >> 8));
}
```

And `send_panel` (`0x4200be0c`) was changed to start with:

```c
void send_panel(uint8_t which, void *data, uint32_t len) {
    if (which == 0) {
        read_tsc();           // ← ONLY called when which==0, which is panel 1
    }
    gpio_set_level(PTR_CTRL_PIN_TABLE[which], 0);   // select the right panel
    epaper_cmd_data(0x10, data, len);               // DTM (image data)
    gpio_set_level(0x12, 1); gpio_set_level(8, 1);
    printf("Writing data is completed.\r\n");
}
```

Note `read_tsc()` is only called once per refresh (before the panel-0 DTM), not twice as our initial analysis claimed. Both panels share a TSC reading — makes sense, it's temperature.

### What the read returns on this board

We verified over serial on a live device running both stock and our firmware: the TSC read always returns `0x00, 0x00`. The UC8179C has pins for an external RTD; this PCB doesn't have one wired. The controller has an internal sensor too, but it appears to be disabled or misconfigured by the existing init commands. So the value is diagnostic-only — no actual temperature information is extracted.

Our firmware replicates the TSC read anyway, because:

1. It's a behavioural difference from the stock firmware that we want to eliminate (part of the "match the binary bit-for-bit" goal).
2. The BUSY wait and the SPI-read-after-command form a small additional delay at a known point in the refresh flow. The refresh has been seen to work without it, but removing it means one fewer variable if the controller regresses.
3. Logging `TSC Data = 0x00, 0x00` gives us a confidence signal that the SPI+BUSY path is healthy.

---

## `combined_app_init` at `0x4200c224` — full boot-time GPIO dance

This function is the stock firmware's equivalent of our `hw_gpio_init` plus `app_main` GPIO bring-up. It runs exactly once at boot. The Ghidra decompilation is dense (it inlines a lot of `gpio_config` struct setup) but the meaningful extract is:

```c
void combined_app_init(void) {
    // Release any leftover RTC-hold on buttons / CHG_STATUS
    gpio_reset_pin(1);        // BUTTON_1
    gpio_reset_pin(12);       // PWR_BUTTON
    gpio_reset_pin(14);       // CHG_STATUS

    // First gpio_config: GPIO 2, 3, 13 as OUTPUT with pull-up
    //   (WORK_LED, EPAPER_PWR_EN, CHG_EN2)
    //   mask = 0x200C  →  bits 2, 3, 13
    gpio_config(pin_mask=0x200C, mode=OUTPUT, pull_up=true);

    // Second gpio_config: wider set as OUTPUT (no pull)
    //   mask = 0x3c0000000601d4 →  bits 2, 4, 6, 7, 8, 17, 18, 38, 39, 40, 41
    //   i.e. WORK_LED, CHG_EN1, RST, BUSY(!), CTRL2, SYS_POWER, CTRL1,
    //        WIFI_LED, BUTTON_3, BUTTON_2, MOSI
    gpio_config(pin_mask=0x3c0000000601d4, mode=OUTPUT);

    // ── Boot-time SYS_POWER cycle (10 ms OFF/ON) ──
    gpio_set_level(3,  1);    // EPAPER_PWR_EN HIGH — stays this way for the entire runtime
    gpio_set_level(6,  0);    // RST LOW
    gpio_set_level(4,  0);    // CHG_EN1 LOW
    gpio_set_level(13, 0);    // CHG_EN2 LOW
    gpio_set_level(7,  0);    // BUSY LOW (briefly — configured as output just above)
    gpio_set_level(17, 0);    // SYS_POWER LOW
    vTaskDelay_ms(10);
    gpio_set_level(17, 1);    // SYS_POWER HIGH
    vTaskDelay_ms(10);

    // Reconfigure BUSY as INPUT with pull-up, now that the boot-time
    // "drive everything to a known state" dance is done.
    gpio_config(pin_mask=0x80, mode=INPUT, pull_up=true);
    gpio_set_level(6, 1);     // RST HIGH — release display from reset

    // Third gpio_config: buttons — PWR_BUTTON (12), CHG_STATUS (14)
    //   mask = 0x5000, INPUT with pull-up (details not fully reconstructed)
    gpio_config(pin_mask=0x5000, mode=INPUT, pull_up=true);

    // ADC / SPI / I2C initialization — large block of setup
    adc_oneshot_new_unit(&config, &handle);
    spi_bus_initialize(SPI2_HOST=1, &buscfg, SPI_DMA_CH_AUTO=3);
    spi_bus_add_device(SPI2_HOST, &devcfg, &dev_handle);
    //   devcfg: 8-bit command, 8 MHz clock, SPI mode 0
    i2c_new_master_bus(&i2c_conf, &bus_handle);

    // … then config loading from flash, NVS read, network config parse,
    //     image-id table dump, sleep-time-list dump, event loops, etc.
}
```

### Pinned-down facts about GPIO 3 (EPAPER_PWR_EN)

The GPIO 3 behaviour — uncertain in the v2.0.19 analysis — is now clearly:

1. Configured as OUTPUT with internal pull-up.
2. Driven HIGH exactly once, right here at boot.
3. Never touched again. We searched `FUN_4200c224` and every descendant; no other `gpio_set_level(3, …)` appears.

Our firmware now matches this: set HIGH at boot, leave alone. The earlier "drop GPIO 3 LOW on the theory that the stock firmware doesn't drive it HIGH" approach was based on a bad disassembly and has been reverted.

### Pinned-down facts about GPIO 17 (SYS_POWER)

Two things the combined init does that we should match:

1. **Configures GPIO 17 as OUTPUT** (part of the mask-0x3C0000000601D4 `gpio_config` call).
2. **Explicitly pulses SYS_POWER LOW then HIGH at boot with a 10 ms dwell.** This is a cold-start pulse for the display rail, done before any refresh happens. It guarantees the UC8179C sees VCC crossing zero at least once per power cycle.

Our firmware does a similar pulse at the start of `epaper_display_dual` with a 1000 ms LOW dwell. That's overkill relative to the stock firmware's 10 ms, but it doesn't break anything — any dwell long enough for the rail's bulk caps to discharge is fine, and we're conservative.

---

## `app_main` at `0x4200b45c` — the main entry point

The stock firmware's `app_main` is much larger than ours. Relevant to display behaviour, it:

- Calls `combined_app_init` (above).
- Starts a handful of FreeRTOS tasks: `screen_task`, `battery_task`, `key_task`, `button_task`, `led_inti_task`, `app_http_task`, `app_sync_time_task`.
- Contains a "voltage low, power off" path (string at DROM `0x3c105458`) that drops SYS_POWER LOW and waits for brown-out.

`screen_task` is the interesting one — it's the task that calls `display_update` on the refresh path. We haven't fully RE'd its state machine. That's OK for our purposes because the display-relevant interface (the sequence of GPIO and SPI operations that the UC8179C sees) is fully contained in `display_update` and its callees.

If someone later needs to understand the refresh *schedule* (how often, what triggers it, how it interacts with the NTP clock sync and the HTTP download), `screen_task` at `0x4200b45c` is where to start reading.

---

## What our firmware now matches vs. v2.0.26

As of this writing:

- **Pre-refresh GPIO sequence** — match.
- **Double hardware reset + 1 s settle** — match.
- **BUSY wait after reset** — match.
- **17-command init with June's data bytes** — match.
- **Phase A (10 cmds to both) / Phase B (7 cmds to CTRL1 only)** — match.
- **TSC read before panel-0 DTM** — match (though we read before *each* panel; stock only reads once. Cheap; leaving it for now).
- **Post-refresh shutdown sequence with 1 s dwell** — match.
- **PON / 30 ms / DRF with `{0x00}` / BUSY wait / POF with `{0x00}` / BUSY wait** — match.
- **SYS_POWER HIGH only across refresh, LOW between refreshes** — match.
- **GPIO 3 (EPAPER_PWR_EN) HIGH at boot, never toggled** — match.

What we *don't* match:

- We still deep-sleep between refreshes. The stock firmware probably deep-sleeps too (IRAM references suggest `esp_deep_sleep_start` is linked in), but we haven't proved it. Our deep-sleep path has its own edge cases — see the firmware source for what we do with `rtc_gpio_isolate` on EPAPER_PWR_EN / SYS_POWER / etc. — but those are specific to our architecture, not to matching the stock firmware.
- We run a different task set (no `screen_task` FreeRTOS loop; we use a refresh-then-deep-sleep cycle instead).
- Our image fetch is against the user's local Hokku webserver, not the vendor's `http://us.xiaowooya.eframe.sunga...` endpoint.

---

## Open questions that would be worth another RE pass

These are things we didn't fully nail down. A future analysis that improves on this doc could:

1. **Figure out what the `memset(scratch_buf, 0, 480000)` at the top of `display_update` is for.** It targets a DRAM region that doesn't appear to be used later in the same function. Possibly an unused buffer from an earlier revision. Low priority but a loose end.
2. **Decompile `send_panel` (`0x4200be0c`) fully.** We have enough to know it calls `read_tsc()` conditionally and then issues `0x10` DTM — but there may be additional GPIO state we're missing. The current `ghidra_extra.txt` has it partially.
3. **Understand the `0xA4 / 0x76` removal in context.** We recorded the change but didn't reason about what CASCADE_SETTING removal implies for the dual-panel mode. If the controllers now depend purely on CTRL1 / CTRL2 for master/slave selection, that has implications for how future builds might change — worth understanding.
4. **Pin down whether the stock firmware uses `esp_deep_sleep_start`.** Strings and register refs suggest yes; we haven't traced a concrete call. If it does, the wake path would tell us what GPIO state it relies on at cold-boot, which would be useful.
5. **Find the `screen_task` state machine.** Specifically, how it handles battery-low, network-down, and config-missing cases. We have our own equivalents but haven't cross-checked.

None of these are blockers for making our firmware drive the display correctly — we've done that. They're just threads left pulled.
