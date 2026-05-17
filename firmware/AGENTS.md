# Agent rules — firmware

## Packaging
- Every build MUST produce `hokku-firmware_<version>.bin` (merged: bootloader + partition table + app)
- Do NOT commit/release individual `bootloader.bin` / `partition-table.bin` / `hokku_epaper.bin`
- Merge command:
  ```
  esptool --chip esp32s3 merge-bin --output firmware/release/hokku-firmware_<version>.bin \
      --flash-mode dio --flash-freq 80m --flash-size 16MB \
      0x0      firmware/build/bootloader/bootloader.bin \
      0x8000   firmware/build/partition_table/partition-table.bin \
      0x10000  firmware/build/hokku_epaper.bin
  ```
- GitHub release must attach the merged file as the single firmware asset
- Setup tool aborts if no `hokku-firmware_*.bin` asset is found

## Firmware versioning — `firmware/VERSION`

Format: `PROTOCOL.CONFIG.N`

- **`PROTOCOL`** — server↔client wire protocol (HTTP API between device and server). Bump **only** on backwards-incompatible changes to the wire protocol. If a change requires a `PROTOCOL` bump, WARN the human and wait for their explicit decision — do not bump unilaterally.
- **`CONFIG`** — NVS configuration schema version. Bump when NVS fields are added, removed, or incompatibly changed. When bumping `CONFIG`, also update `CONFIG_VERSION` in `tools/hokku_config.py` to the same integer value. NVS changes do NOT affect `PROTOCOL`.
- **`N`** — monotonic counter for all other firmware changes. **Never resets**, even when `PROTOCOL` or `CONFIG` bumps. Increment `N` for every firmware code commit; include the updated `firmware/VERSION` in the same commit.

`CONFIG_VERSION` in `firmware/main/config.h` and `firmware/main/config.c` is derived from `firmware/VERSION` via CMake-generated `version.h` — do not edit it directly.

Examples:
| `firmware/VERSION` | Meaning |
|---|---|
| `1.2.5` | protocol 1, NVS config v2, 5th change |
| `1.2.6` | bug fix — only N incremented |
| `1.3.7` | NVS schema changed — CONFIG and N incremented, `tools/hokku_config.py` updated |
| `2.3.8` | wire protocol break (human approved) — PROTOCOL and N incremented |

## Display driver (DO NOT MODIFY)
- Do not touch: SPI init, CS management, BUSY polling, GPIO init, `epaper_reset`, `epaper_init_panel`, `epaper_send_panel`, `epaper_display_dual`, `epaper_wait_busy`
- GPIO0 (SPI CS) is a boot strapping pin — must be managed by SPI driver (`spics_io_num = PIN_EPAPER_CS`), never `gpio_set_level`
- GPIO7 (BUSY) has external pull-up on PCB; `gpio_reset_pin` also enables internal pull-up — both required; do not skip
- `display_message()` must use `split_and_display()` with identical buffer layout: first 480K = panel 1, second 480K = panel 2
- After flashing factory firmware dump (`.private/flash_dump.bin`), wait 30 s before flashing our firmware

## Flashing procedure
1. Flash factory dump at offset 0x0
2. Wait 30 s
3. Flash bootloader + partition table + app + NVS config
- `esptool` works any time USB is connected (resets into ROM bootloader)
- `USB_AWAKE`: never deep-sleeps while USB plugged in
- `BATTERY_IDLE`: 5 s awake window per refresh — plug USB first to enter `USB_AWAKE` for reflash

## Coding / compiling
- Always `git commit` firmware code before building and flashing
- Never use ESP32 USB pins (leave in original state)
- Always verify no fast boot loop was introduced
- Firmware never auto-refreshes on boot; triggers: schedule, button press, first install
- `hard_reset` after flashing ESP32 automatically

## Reverse-engineering notes
- Stock firmware findings: `docs/reverse_engineering_overview.md` + per-version files
- New RE pass → update existing docs or add `docs/reverse_engineering_v<VER>_<DATE>.md`
- Binaries and scratch notes stay in `.private/`; digested findings go in `docs/`
- Hardware facts: `docs/hardware_facts.md` (may be inaccurate — treat with caution)
