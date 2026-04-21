# E_Frame stock firmware v2.0.19 (April 21 2025) — reverse-engineering notes

This is the version the device shipped with from the factory. On an unmodified unit this firmware lives in the `ota_0` partition and is the active OTA slot until the device pulls down a v2.0.26 update over the air.

Read [`reverse_engineering_overview.md`](reverse_engineering_overview.md) first — it has the pin map, tooling, and methodology that both per-version docs assume.

---

## Image metadata

From the ESP32 app image header at the start of the partition:

```
Project name:  E_Frame
App version:   2.0.19
Compile time:  2025-04-21 14:19:40
ELF SHA256:    5fb0a527eb28b101ff0f1cc09dd33cc1236db7f2da2284bce7763f2dcffc23cc
ESP-IDF:       v5.2.2
Source path:   D:/Project/ESP32/Eink/
```

The "Source path" is a filesystem path embedded in every assertion string in the binary (it's the compile-time `__FILE__`). So the vendor's dev environment is Windows with the project tree at `D:/Project/ESP32/Eink/`.

Partition table (same across both stock versions):

| Name | Offset | Size | Purpose |
|------|--------|------|---------|
| `nvs` | `0x00009000` | 16 K | Non-volatile settings. |
| `otadata` | `0x0000d000` | 8 K | OTA slot selector. |
| `phy_init` | `0x0000f000` | 4 K | Wi-Fi PHY calibration. |
| `ota_0` | `0x00010000` | 1.5 MB | App slot 0. |
| `ota_1` | `0x00190000` | 1.5 MB | App slot 1. |
| `imagedata` | `0x00310000` | 12 MB | FAT filesystem for cached images. |

### Files in `.private/v2.0.19_apr21/`

```
ota_0.bin                        — app partition image (1.5 MB)
bootloader.bin                   — 32 KB standard ESP-IDF v5.2.2 bootloader
drom.bin  (@ 0x3c100020)         — read-only data segment (~246 KB)
irom.bin  (@ 0x42000020)         — instruction ROM segment (~1 MB)
iram.bin  (@ 0x40374000)         — IRAM (ISRs and critical code, ~100 KB)
dram_1.bin, dram_3.bin           — initialized data segments
seg_0 through seg_6 .bin         — raw per-segment extractions (address in filename)
```

Scratch notes from the initial analysis pass:

```
ANALYSIS.md        — first pass, several claims that didn't hold up
ERRATA.md          — self-falsification of ANALYSIS.md (honest corrections)
FINAL_FINDINGS.md  — what survived
display_init_disasm.txt, app_main_disasm.txt, gpio_power_analysis.txt
analyze_app_main.py, analyze_v2.py, analyze_v3.py  — Capstone scripts
```

Treat `ANALYSIS.md` and `ERRATA.md` as historical context only — the conclusions there are partly wrong (see "Things we got wrong during the v2.0.19 analysis" below). `FINAL_FINDINGS.md` is the consolidated truth, and this document supersedes it.

---

## Function map

These are the functions that matter for display behaviour. Addresses are in IROM (`0x42000020` base).

| Address | Our name | What it does |
|---|---|---|
| `0x4200acac` | `display_update(buffer)` | **The main refresh entry point.** Clears a scratch buffer, raises SYS_POWER, does an outer hardware reset, calls `display_init`, sends both panels, calls `display_refresh`, drops SYS_POWER. |
| `0x4200ad04` | `screen_task()` | FreeRTOS task that drives refreshes. Calls `display_update` on the normal refresh path (caller @ `0x4200b1e7`) and the error-buffer-filled path (caller @ `0x4200b3ce`). |
| `0x4200ab28` | `battery_task()` | ADC read and floating-point averaging. |
| `0x4200ab68` | `key_task()` | Button/power-event handler. |
| `0x4200b418` | `app_main()` | FreeRTOS entry. Does WiFi bring-up, spawns tasks. Also contains two `gpio_set_level(17, 0)` calls on the battery-low / error paths. |
| `0x4200b96c` | `ctrl_pin_setter(level)` | Drives CTRL1 and CTRL2 to the same level simultaneously. `level=1` deselects both; `level=0` selects both for broadcast. |
| `0x4200b984` | `hardware_reset()` | `RST=0 / 100 ms / RST=1 / 100 ms`. Two-pulse sequence. |
| `0x4200b9a4` | `display_init()` | Raises SYS_POWER (redundantly), waits 1 s, does its own hardware reset, waits on BUSY, then sends 18 init commands. |
| `0x4200bc70` | `display_refresh()` | PON / DRF / POF with BUSY waits between each. |
| `0x4200bdcc` | `vTaskDelay_ms(ms)` | Thin wrapper converting ms to ticks. |
| `0x4200bde0` | `epaper_cmd_only(cmd)` | SPI transaction: one cmd byte, no data. |
| `0x4200bec8` | `epaper_cmd_data(cmd, ptr, len)` | SPI transaction: one cmd byte + N data bytes. |
| `0x4200f340` | `app_http_task()` | Image fetch over HTTP. |
| `0x4200f910` | `led_inti_task()` | LED init. Typo in the stock source: "inti" not "init". Survives in multiple string references. |
| `0x4200c138` | `button_task()` | Button polling. |
| `0x42010cb8` | `app_sync_time_task()` | NTP time sync. |

---

## `display_update` — the main refresh flow

Disassembled from `0x4200acac` (bytes long). Pseudocode follows the actual control flow:

```c
void display_update(uint8_t *buffer) {
    memset(scratch_buf_from_DRAM, 0, 480000);  // 1 panel's worth
    gpio_set_level(17, 1);                      // SYS_POWER HIGH
    vTaskDelay_ms(10);
    hardware_reset();                           // RST 100 ms low / 100 ms high
    ctrl_pin_setter(1);                         // CTRL1=1, CTRL2=1 (deselect both)
    display_init();                             // 1 s settle + another RST + BUSY wait + 18 cmds
    send_panel(0, buffer + 0,      480000);
    send_panel(1, buffer + 480000, 480000);
    display_refresh();                          // PON / DRF / POF
    vTaskDelay_ms(10);
    gpio_set_level(17, 0);                      // SYS_POWER LOW
    return;
}
```

Key observations:

- **Every refresh does two hardware resets.** One inside `display_update` (the "outer" reset), one inside `display_init` (the "inner" reset). Between them, `display_init` does its own 1-second settle. That's the belt-and-suspenders reset sequence our firmware now matches.
- **SYS_POWER is dropped LOW on exit.** This is the single most important behavioural fact about this firmware. Combined with the opening `gpio_set_level(17, 1)`, every refresh starts the UC8179C from cold. There is no opportunity for a bad internal state in the controller to survive across refreshes — the controller is physically unpowered between them.
- The `memset(scratch_buf, 0, 480000)` at entry is zeroing a DRAM buffer. Purpose isn't fully clear but it's a scratch area used later in the function path — probably a pre-cleared working buffer for an error / fallback "show a black panel" code path elsewhere.

**What v2.0.19 does not have** (added in v2.0.26 — see that doc): the extended shutdown sequence at the end. v2.0.19 just drops SYS_POWER LOW; v2.0.26 first drives SCLK/MOSI/BUSY/buttons/LED LOW, sits on that for a full second, then drops CTRL and RST, *then* drops SYS_POWER. That difference turns out to matter for the stuck-state bug and is the primary reason we steal from v2.0.26 specifically.

---

## `display_init` — the init sequence

Disassembled from `0x4200b9a4`:

```c
void display_init(void) {
    gpio_set_level(17, 1);              // SYS_POWER HIGH (redundant after display_update)
    vTaskDelay_ms(1000);                // wait 1 s for DC-DC to stabilise
    gpio_set_level(6, 0);               // RST LOW
    vTaskDelay_ms(100);
    gpio_set_level(6, 1);               // RST HIGH
    vTaskDelay_ms(100);

    // BUSY wait — up to 2001 iterations * 10 ms = ~20 s timeout
    int16_t counter = 0x7d1;            // 2001
    while (counter-- > 0) {
        if (gpio_get_level(7) != 0) break;  // BUSY HIGH = idle, controller ready
        vTaskDelay_ms(10);
    }

    // 18 command-data pairs follow, in two phases
    send_cmd_data(0x74, ...); after_phase_a_frame();
    send_cmd_data(0xF0, ...); after_phase_a_frame();
    send_cmd_data(0x00, ...); after_phase_a_frame();
    send_cmd_data(0x50, ...); after_phase_a_frame();
    send_cmd_data(0x60, ...); after_phase_a_frame();
    send_cmd_data(0x86, ...); after_phase_a_frame();
    send_cmd_data(0xE3, ...); after_phase_a_frame();
    send_cmd_data(0xE0, ...); after_phase_a_frame();

    // Phase transition — CTRL2 stays HIGH from here on
    send_cmd_data(0x61, ...); after_phase_b_frame();
    send_cmd_data(0x01, ...); after_phase_b_frame();
    send_cmd_data(0xB6, ...); after_phase_b_frame();
    send_cmd_data(0x06, ...); after_phase_b_frame();
    send_cmd_data(0xB7, ...); after_phase_b_frame();
    send_cmd_data(0x05, ...); after_phase_b_frame();
    send_cmd_data(0xB0, ...); after_phase_b_frame();
    send_cmd_data(0xB1, ...); after_phase_b_frame();
    send_cmd_data(0xA4, ...); after_phase_b_frame();
    send_cmd_data(0x76, ...); after_phase_b_frame();
}
```

`after_phase_a_frame()` is `CTRL1=1 / CTRL2=1 / CTRL1=0 / CTRL2=0` — raise both, then drop both, leaving both LOW for the next command (broadcast mode).

`after_phase_b_frame()` is `CTRL1=1 / CTRL2=1 / CTRL1=0` — raises both, then drops only CTRL1. CTRL2 stays HIGH, so the next command goes to panel 1 only.

So the two phases are:
- **Phase A** (8 commands): sent to both panels simultaneously with CTRL1=0, CTRL2=0.
- **Phase B** (10 commands): sent to panel 1 only with CTRL1=0, CTRL2=1.

### The 18 init commands — exact byte values

Dereferenced from the data pointers `PTR_DAT_...` in `display_init`'s literal pool and read out of DROM:

| Cmd | Bytes | UC8179C meaning |
|---|---|---|
| `0x74` | `C0 1C 1C CC CC CC 15 15 55` | (undocumented) |
| `0xF0` | `49 55 13 5D 05 10` | (undocumented) |
| `0x00` | `DF 69` | PANEL_SETTING (PSR) |
| `0x50` | `F7` | VCOM / data interval |
| `0x60` | `03 03` | TCON |
| `0x86` | `10` | (undocumented) |
| `0xE3` | `22` | (undocumented) |
| `0xE0` | `01` | (undocumented) |
| *— phase A ends here —* | | |
| `0x61` | `04 B0 03 20` | TCON_RESOLUTION = 0x04B0 × 0x0320 = 1200 × 800 |
| `0x01` | `0F 00 28 2C 28 38` | POWER_SETTING |
| `0xB6` | `07` | (undocumented) |
| `0x06` | `E8 28` | BOOSTER_SOFT_START |
| `0xB7` | `01` | (undocumented) |
| `0x05` | `E8 28` | POWER_ON_MEASURE |
| `0xB0` | `01` | (undocumented) |
| `0xB1` | `02` | (undocumented) |
| `0xA4` | `83 00 02 00 00 00 00 00 00` | CASCADE_SETTING |
| `0x76` | `00 00 00 00 04 00 00 00 83` | (undocumented) |

The "undocumented" commands are ones we couldn't find in public UC8179C datasheets. They appear to be vendor-specific register accesses the UC8179C silicon accepts. The data bytes are what matters — we just have to send them verbatim.

---

## `display_refresh` — PON / DRF / POF

From `0x4200bc70`:

```c
void display_refresh(void) {
    // PON — power on
    ctrl_low();                           // both panels selected
    epaper_cmd_only(0x04);                // PON, no data bytes
    ctrl_high(); busy_wait();

    // DRF — refresh
    ctrl_low();
    vTaskDelay_ms(30);                    // 30 ms pre-delay before DRF
    epaper_cmd_data(0x12, {0x00}, 1);     // DRF with 1 data byte (waveform select)
    ctrl_high(); busy_wait();             // ~19 s on this display

    // POF — power off
    ctrl_low();
    epaper_cmd_data(0x02, {0x00}, 1);     // POF with 1 data byte
    ctrl_high(); busy_wait();
}
```

Three things to note:

1. **DRF has a 30 ms pre-delay.** The datasheet doesn't document a minimum gap between writing image data and issuing DRF; the stock firmware waits 30 ms anyway. We match this.
2. **DRF is sent with one data byte `0x00`.** That byte appears to control temperature-based waveform selection (`0x00` = default / auto). The datasheet is unclear; we copy the value.
3. **POF is also sent with a data byte.** Again `0x00`, again undocumented. We copy.

"BUSY wait" throughout is the same ~20 s timeout loop as in `display_init`.

---

## Our firmware ↔ v2.0.19 init-sequence diff

This was the state of play after the v2.0.19 analysis. Most of these have since been closed by matching v2.0.26 instead (see that doc), but the v2.0.19-specific gaps were:

| Aspect | v2.0.19 stock | Our firmware (at time of analysis) |
|---|---|---|
| Power-on delay before reset | 1000 ms | 500 ms |
| RST LOW pulse width | 100 ms | 20 ms |
| Post-RST delay | 100 ms | 200 ms |
| BUSY wait after reset | yes, ~20 s timeout | **missing** |
| Explicit SYS_POWER=HIGH pre-refresh | yes | yes |
| Second hardware reset inside init | yes | missing |
| Phase A / Phase B command grouping | 8 / 10 split as above | different grouping |

The most load-bearing missing item was **BUSY wait after reset**. The UC8179C takes a variable amount of time to become responsive after RST goes HIGH, and there's no documented upper bound. Our firmware at the time dove straight into `send_cmd` right after the reset pulse — if the controller wasn't ready yet, the first few commands were dropped and the init finished in a silently-wrong state.

Fixing this (the "Fix B" from the v2.0.19 ERRATA) closed most of the stuck-display cases on its own. It's a single `epaper_wait_busy()` call after `epaper_reset()`.

---

## Things we got wrong during the v2.0.19 analysis

This is worth recording because it's the kind of thing that's easy to repeat.

**Wrong: "GPIO 17 is not SYS_POWER".** The initial analysis found `gpio_set_level(17, 1)` at the start of `display_update` and `(17, 0)` at the end, and also observed `screen_task` never touches GPIO 17. That got mis-read as "GPIO 17 must be controlling something local to the display, not the whole system." It was then *falsified* in the ERRATA: on-board capacitors give the ESP32 tens of ms to execute a few more instructions even after dropping system power, so "code continuing after `gpio_set_level(17, 0)`" isn't evidence of non-power-latch behaviour. The honest position is: GPIO 17 is SYS_POWER by the pin-map (this is the name the board schematic uses if we had it), and it gates display controller power, which is exactly how one would use a system-power pin if the "system" is the UC8179C rather than the ESP32.

**Wrong: "The stock firmware doesn't use ESP-IDF deep sleep."** Initially claimed based on absence of `esp_deep_sleep_start` and similar strings in DROM. Falsified by finding RTC-memory code, `rtc_gpio_*` strings, and `RTC_GPIO_HOLD` register accesses — all linker-pruned if the deep-sleep functions weren't called. Correct position: we don't know, and it doesn't matter for the stuck-state bug because the display power-cycle happens inside `display_update`, not in any wake path.

**Wrong: "The stock firmware never drives GPIO 3 HIGH."** Initially claimed after a naive `(movi a10, 3) + (movi a11, 1) + call` grep of IROM. The `call8` target alignment bug in Capstone meant the search was running on mis-aligned instruction bytes in places, hiding real instructions inside what looked like data. The later Ghidra analysis found a clean `gpio_set_level(3, 1)` call inside the combined init function (`FUN_4200c224` — documented in the v2.0.26 file). Correct position: GPIO 3 is driven HIGH once at boot, never toggled again.

**Wrong: "ESP32 reset during flash-write power-cycles the display and that's why flashing the stock firmware fixes stuck states."** On re-examination this is physically implausible — the reset is µs, the display power caps retain charge for much longer, and any external pull-up on `EPAPER_PWR_EN` would prevent the rail from dropping during reset. So "flashing original fixes it" *has* to be a software effect, not a power-cycle effect. Which points at the init-sequence differences — and that's what it turned out to be.

---

## TL;DR for v2.0.19

If you never look at v2.0.26, these are the things you'd steal from v2.0.19 to make our firmware match the stock one:

1. **Extend power-on settle to 1000 ms.** (Our old code used 500 ms.)
2. **Widen RST LOW pulse to 100 ms.** (Our old code used 20 ms.)
3. **Do a second hardware reset inside init.** (Our old code did one.)
4. **`epaper_wait_busy()` between reset and the first init command.** (This is the big one.)
5. **Split init into an 8-command phase A (both panels) and a 10-command phase B (panel 1 only).**
6. **Cycle SYS_POWER HIGH→LOW around every refresh — never leave it HIGH between refreshes.**

v2.0.26 further refines some of this (swaps two command data bytes, adds a new command, drops two commands, and — critically — adds the post-refresh shutdown sequence). See [`reverse_engineering_v2.0.26_jun20.md`](reverse_engineering_v2.0.26_jun20.md) for the delta.
