# Reverse-engineering the Hokku/E_Frame stock firmware — overview

This directory documents what we've learned by reverse-engineering the stock firmware that the Hokku 13.3" ACeP 6-color e-paper frame ships with. Two versions of that firmware have been analyzed so far:

- [`reverse_engineering_v2.0.19_apr21.md`](reverse_engineering_v2.0.19_apr21.md) — the build the device shipped with from the factory (April 21 2025).
- [`reverse_engineering_v2.0.26_jun20.md`](reverse_engineering_v2.0.26_jun20.md) — an OTA-updated build pulled off the device (June 20 2025). This is the build most units in the wild are actually running.

This overview covers the bits that are shared between both documents: why we're doing this, what the hardware is, how we got the binaries, what tooling worked and what didn't, and how the pieces fit together. The per-version files then focus on what's specific to that firmware — function addresses, init-command bytes, behavioural quirks.

The audience for this is humans — someone picking up this project cold who wants to understand why our firmware is written the way it is, or who wants to extend the RE. Claude also reads these docs, but they are written for you.

---

## Why we are reverse-engineering the stock firmware at all

Our firmware (`hokku_epaper`, in this same repo) is a clean-room replacement that drives the display, fetches images from a user-hosted webserver, and runs on battery with scheduled wake-ups. It does not derive any code or data from the stock firmware; we only look at the stock firmware to understand the **display init and refresh sequences** that the UC8179C controller actually expects on this specific board.

The concrete reason reverse-engineering keeps coming back is this observation:

> When the e-paper display gets into a bad state (stuck image, ghosting, partial refresh corruption), flashing and booting the stock firmware reliably clears it. Rebooting or reflashing our firmware does not.

That means the stock firmware knows a recovery path we don't. Every time we've tracked one of these differences down, it turned out to be a detail of the UC8179C init / shutdown sequence that isn't in any datasheet we can find — the vendor apparently tuned timings and command data experimentally. So we steal those details from the binary.

The goal is always the same: **our firmware's display driver should, on the pins the display cares about, be bit-for-bit indistinguishable from what the stock firmware does.** When the two diverge, the display eventually wedges, and the user needs a factory-firmware reflash to recover.

---

## The hardware in one page

- **Frame:** Hokku 13.3" ACeP 6-color e-paper frame (sometimes branded "E_Frame" or "xiaowooya" in the firmware strings — the internal project directory in the stock firmware is `D:/Project/ESP32/Eink/`).
- **SoC:** ESP32-S3 v0.2, 16 MB flash, 8 MB PSRAM. Built-in USB-Serial/JTAG (VID 0x303a, PID 0x1001) — no external debug probe needed.
- **Display controller:** UC8179C in dual-panel mode. The 13.3" panel is actually two 6.6" panels tiled side-by-side at 1200×800, each driven by its own UC8179C with a shared SPI bus and individual CS-like select pins (`CTRL1`, `CTRL2`).
- **Palette:** 6-color ACeP (Advanced Color e-Paper) Spectra 6 — black, white, red, yellow, green, blue. No grayscale. A full refresh takes ~19 s on this hardware, which is why every bit of the init/refresh sequence matters.

Full pin map (confirmed — matches both stock firmware's `gpio_config` masks and our `firmware/main/main.c`):

| GPIO | Name | Direction | Purpose |
|------|------|-----------|---------|
| 0 | EPAPER_CS | output (SPI) | Display SPI chip-select. Boot-strapping pin — must be owned by the SPI driver (`spics_io_num`), not manual `gpio_set_level`, or the chip won't boot reliably. |
| 1 | BUTTON_1 | input, pulled up | User button 1. Active-LOW. RTC-capable → usable as EXT1 wake source. |
| 2 | WORK_LED | output | Work/activity LED. |
| 3 | EPAPER_PWR_EN | output | Display rail enable. The stock firmware sets this HIGH once at boot and never touches it. **Whether this pin actually powers the display or is decorative is still not fully pinned down** — see "GPIO 3 mystery" below. |
| 4 | CHG_EN1 | output | Charger enable 1 (one of two sink/source control pins on the battery-management IC). |
| 6 | EPAPER_RST | output | Display reset (active LOW). Pulsed LOW 100 ms / HIGH 100 ms during init. |
| 7 | EPAPER_BUSY | input, pulled up | Display busy signal (active LOW). **External pull-up on the PCB** — firmware must also enable the internal pull-up (`gpio_reset_pin` does this) for reliable reads. When nothing drives BUSY (e.g. controller unpowered or wedged) the external pull-up holds it HIGH, which trivially passes `wait_busy` and hides stuck-state bugs. |
| 8 | CTRL2 | output | Select for panel 2 of the dual-panel tiled display. Active-LOW — "LOW selects this panel". |
| 9 | EPAPER_SCLK | output (SPI) | SPI clock. |
| 10 | I2C_SDA | bidir | I2C bus data. The stock firmware creates an I2C master bus at boot but we've never observed it being used per-refresh. Likely for the battery fuel gauge or a PMIC. |
| 11 | I2C_SCL | bidir | I2C bus clock. |
| 12 | PWR_BUTTON | input, pulled up | Power button. Active-LOW. RTC-capable. |
| 13 | CHG_EN2 | output | Charger enable 2. |
| 14 | CHG_STATUS | input, pulled up | Charger status (LOW = actively charging, HIGH = idle or topped off). RTC-capable. Do not use this as a "USB cable connected" signal — a fully-charged battery will float HIGH while USB is still plugged in. |
| 17 | SYS_POWER | output | **Display power rail latch.** Driven HIGH at the start of every display update, LOW at the end. In the stock firmware this pin does the real power-gating of the display controller, not GPIO 3. See the per-version docs for exactly when it's toggled. |
| 18 | CTRL1 | output | Select for panel 1. Same polarity as CTRL2. |
| 38 | WIFI_LED | output | Wi-Fi / network LED. |
| 39 | BUTTON_3 | input | User button 3. (Not wake-capable in our firmware.) |
| 40 | BUTTON_2 | input | User button 2. (Not wake-capable in our firmware.) |
| 41 | EPAPER_MOSI | output (SPI) | SPI MOSI. |

A more rigorous version of the above with register-level references lives in [`../HARDWARE_FACTS.md`](../HARDWARE_FACTS.md). That file is still the authoritative pin map; this table is just a refresher. The "facts" in that file that still carry a caveat are re-documented here, below.

### GPIO 3 (EPAPER_PWR_EN) mystery — status

Every analysis pass so far has looked for the stock firmware driving GPIO 3 LOW after boot. We still haven't found a call site that does it. Two different passes (Capstone with alignment fixed, then Ghidra) both report zero `gpio_set_level(3, 0)` calls in the app binary. The stock firmware sets GPIO 3 HIGH once at boot (inside the combined init function — see `FUN_4200c224` in the June doc) and leaves it HIGH for the entire runtime. Our firmware used to drive GPIO 3 LOW at various points; we've since matched the stock behaviour.

What we don't know is what GPIO 3 actually controls on the board. If it's a redundant enable in parallel with SYS_POWER, its state doesn't matter. If it's the real enable for a 15 V boost, then SYS_POWER may be something else entirely and our labels are off. We haven't traced the schematic yet. The code-level fact is: **set it HIGH, leave it alone**, which matches the stock firmware regardless of its actual function.

### GPIO 17 (SYS_POWER) role — what we know

`SYS_POWER` is the pin most discussed in the per-version docs. Both stock firmware versions drive it:

- HIGH at the start of `display_update` (right before the first hardware reset),
- LOW at the end of `display_update` (after the 1-second shutdown dwell, after RST has been pulled LOW).

That means in the stock firmware, **the display controller is unpowered between refreshes.** Every refresh starts from a cold boot of the UC8179C. Our firmware now matches this.

This is what makes the stuck-state bug possible on our side: if SYS_POWER stays HIGH across a refresh (or is cycled but with the wrong signal-line state), the controller can end up in an internal state that isn't cleared by a simple RST pulse. Only power-cycling + the correct init sequence clears it — and the "correct init sequence" is version-specific (see the init-bytes table in each per-version doc).

---

## How we got the binaries

The device has USB-Serial/JTAG built in, so both reading and JTAG-debugging are possible with just a USB cable. There is no write-protection on the flash, so `esptool read_flash` against a factory-reset device gives you everything.

### Full-flash dump of the factory state

```bash
esptool.py --chip esp32s3 -p COM3 read_flash 0x0 0x1000000 \
    .private/factory_full_flash_dump_2025-04_dual-ota.bin
```

16 MB. Includes bootloader, partition table, both OTA slots, NVS, `imagedata` FAT partition. The filename encodes the date (2025-04) and the fact that both OTA slots were present. This dump is what we flash back onto the device with `esptool write_flash 0x0 …` to force a known-good factory restore; that's documented in the root `CLAUDE.md`.

### Per-OTA-slot dumps

The stock firmware uses OTA for updates. `otadata.bin` at `0x0000d000` selects which of `ota_0` (at `0x10000`) or `ota_1` (at `0x190000`) is active. A freshly-shipped device has v2.0.19 in `ota_0` and the slot is active. An OTA-updated device has v2.0.26 written to `ota_1`, `otadata` incremented to seq 2, and boots from `ota_1`. The older slot is left intact.

We extracted each slot's segments separately (DROM, IROM, IRAM, DRAM, RTC fast/slow) using custom extraction scripts in `.private/` so Ghidra / Capstone could load them at their correct addresses. The per-version docs list which files are in each folder.

---

## Tooling — what worked and what didn't

This was the order we went through tools. Later entries supersede earlier ones but the earlier ones are still useful to know about.

### 1. Capstone linear disassembly — partly reliable, don't trust blindly

We used the Python `capstone` library to disassemble IROM in one linear pass. It works for long stretches of code, but the ESP32-S3 Xtensa build has two things that break linear disassembly:

- **Literal pools.** The compiler emits constant data inline with code (for `l32r` loads from PC-relative literals). Capstone disassembles those bytes as instructions, getting garbage, and then often re-aligns *after* the literal pool to a wrong offset, silently hiding the next few real instructions inside the tail of the garbage. Using `skipdata=True` helps but isn't reliable.
- **Windowed ABI + `call8` target math.** Xtensa call instructions use a non-obvious offset encoding. The target of a `call8 offsetN` is `((PC >> 2) + 1 + offsetN) << 2` — not `PC + offset`. An early version of our analysis used the naive formula and produced an incorrect call graph that hid the real `display_update` function (we were calling it `shutdown_and_poweroff`). Once we fixed the formula, the call graph made sense.

Outcome: Capstone is fine for spot-checking individual functions whose address you already know, but don't use it to build a call graph and don't assume it found every code path. The `.private/v2.0.19_apr21/*.py` scripts show what we ran; keep them only as examples of what *not* to do in isolation.

### 2. JTAG live introspection — useful for confirming behaviours

The ESP32-S3's built-in USB-Serial/JTAG is directly OpenOCD-compatible (v0.12.0+). On Windows, the built-in USB enumerates two interfaces: 0 = CDC serial (goes to whatever COM port driver Windows picks), 2 = JTAG (needs WinUSB). You drive Zadig once to install WinUSB on interface 2 and OpenOCD then works.

Config:

```
openocd -f board/esp32s3-builtin.cfg
```

What we used JTAG for, in order of usefulness:

- **GPIO polling while the stock firmware runs.** This directly confirmed per-refresh SYS_POWER cycling (GPIO 17 goes LOW→HIGH→LOW every refresh) and that CTRL1/CTRL2 are both driven LOW at the tail of the shutdown sequence. No decompilation required. Just `reg` reads at 1 Hz.
- **Snapshot reads of I2C peripheral state,** to confirm the I2C bus isn't used per-refresh. (It's initialized at boot but idle thereafter.)
- **Watchpoints on `gpio_set_level` arguments,** to enumerate all call sites. This was flaky on Xtensa — hardware breakpoints would re-fire 40+ times after a `stepi` that should have cleared them, and OpenOCD's step-over-HW-BP is unreliable on this architecture. We got one useful trace (the TSC read) out of this and mostly stopped using it.

### 3. Ghidra — authoritative

Ghidra 12.0.4 with its stock Xtensa processor module (`Xtensa:LE:32:default`) decompiles this firmware well. The caveat is loading: ESP32-S3 firmware images don't map to a single address, they get split into DROM (0x3c100020), IROM (0x42000020), IRAM (0x40374000), DRAM, RTC slow/fast. Ghidra won't auto-detect this from an ESP image. We wrote a small pre-script (`.private/ghidra_scripts/SetupESP32S3Memory.java`) that creates the five memory blocks at the right bases before auto-analysis runs.

Once that's loaded, Ghidra's decompiler produces C that's clearly related to the source. Function signatures are of course mangled (everything is `void FUN_xxx(void)` with synthesized locals), so you work by chasing string references: find `"TSC Data"` in DROM, follow the reference, and you've found `read_tsc()`. The output in `.private/ghidra_output_jun20.txt` was produced by a post-script that walks a list of known "key" addresses and dumps each function's decompilation. That file is the single most useful artifact in this whole investigation.

Everything in the per-version docs that says "from Ghidra decompilation" comes from this output. When the per-version docs quote C-like code, read it as "Ghidra's best reconstruction," not stock source — it's accurate to the instruction flow but the variable types are inferred.

---

## How to extend the RE

If a future version of the stock firmware ships (say, v2.0.30) and you want to compare against what we have:

1. Pull the new image off the device. If OTA, only `ota_1` changes — `esptool read_flash 0x190000 0x200000 ota_1.bin`. If factory-reflashed, do a full-flash dump like we did above.
2. Split the image into segments. The ESP32 image header tells you the base of each segment; our existing scripts are in `.private/ghidra_scripts/`.
3. Load into Ghidra using the same pre-script.
4. Diff against the previous version's `ghidra_output_*.txt`. Start with `FUN_4200acb0` (`display_update`), `FUN_4200b9e8` (`display_init`), `FUN_4200bc98` (`display_refresh`), `FUN_4200c224` (`combined_app_init`). These are the display-relevant entry points. Function addresses will move — follow the string references (`"Write PON"`, `"TSC Data"`, `"app_spi_init"`, etc.) to locate them.
5. Check the init command bytes. The byte-for-byte init table in the per-version docs was produced by dereferencing the data pointers in `display_init` (`PTR_DAT_42000ac4`, etc.) and reading them out of DROM. If the init data pointers have moved or their content has changed, record the diff.
6. Write a new `reverse_engineering_v<VERSION>_<DATE>.md` alongside the existing ones, and update this overview file's list at the top.
7. If the new version's behaviour differs from what our firmware does, port the change (or at least document why you chose not to).

A good RE pass covers: (a) the boot init / combined setup function, (b) the display update function's pre- and post-refresh GPIO dance, (c) the display init's hardware-reset and BUSY-wait timing, (d) the full init command list with data bytes, (e) the PON/DRF/POF refresh sequence, (f) anything else the binary does per-refresh (the stock firmware reads the controller's internal temp sensor via TSC every refresh, for instance).

---

## What to keep in `.private/` vs `docs/`

The dumps themselves (binaries, Ghidra project, disassembly text) stay in `.private/`. They are vendor firmware; we don't redistribute them.

The *analysis* — what the binary does, why, how that relates to our implementation — lives in `docs/`. These files are the ones meant to be readable by humans, checked in, and updated when new information comes in. No vendor code is reproduced here; only the facts we derived.

If you produce new scratch notes during an RE pass (`FINAL_FINDINGS.md`, `ANALYSIS.md`, `ERRATA.md` etc. — we've had all three), put them in `.private/` next to the dumps. When you're confident in the findings, fold them into the per-version doc here and delete the scratch.
