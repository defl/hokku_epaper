# Agent rules — firmware

## Packaging
- Every build MUST produce `hokku-huessen_epf1301-<version>.bin` (merged: bootloader + partition table + app + otadata)
- Do NOT commit/release individual `bootloader.bin` / `partition-table.bin` / `hokku_epaper.bin`
- Merge command (run from repo root; note underscore args and `ota_data_initial.bin`):
  ```
  esptool.py --chip esp32s3 merge_bin --output firmware/release/hokku-huessen_epf1301-<version>.bin \
      --flash_mode dio --flash_freq 80m --flash_size 16MB \
      0x0      firmware/huessen_epf1301/build/bootloader/bootloader.bin \
      0x8000   firmware/huessen_epf1301/build/partition_table/partition-table.bin \
      0x10000  firmware/huessen_epf1301/build/hokku_epaper.bin \
      0x610000 firmware/huessen_epf1301/build/ota_data_initial.bin
  ```
  The `ota_data_initial.bin` is required so the bootloader boots `ota_0` on a
  fresh flash; it is generated automatically by `idf.py build`.
- GitHub release must attach the merged file as the single firmware asset
- Setup tool aborts if no `hokku-huessen_epf1301-*.bin` asset is found

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

### A/B reflash of a working unit (does NOT touch the bootloader)

**Never assume which slot is active.** Read `otadata` (0x610000, 0x2000) first.
Two 32-byte entries at offsets 0 and 4096: `ota_seq` u32 @+0, `ota_state` u32
@+24, `crc` u32 @+28 where `crc = crc32(pack('<I', seq), 0xFFFFFFFF)`. The
bootloader takes the copy with the highest *valid* seq, and `slot = (seq-1) % 2`.
One unit here was found running `ota_1` — flashing the "obvious" `ota_0`… the
other way round would have overwritten the running image and destroyed the
recovery hatch in a single step.

1. `esptool ... write_flash <inactive slot offset> build/hokku_epaper.bin`
   (`ota_0` @0x010000, `ota_1` @0x310000). Bootloader @0x0 and partition table
   @0x8000 are never written.
2. Patch **only otadata sector 0**: lowest `seq` that is both higher than the
   other copy and maps to the target slot, recompute the CRC, leave `ota_state`
   alone; write those 4096 bytes to 0x610000. The other copy stays valid
   throughout, so even an interrupted write still boots the old slot.
3. Power-cycle, then `ping` on the console to confirm.

**Rollback trap.** `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y` and the app only
calls `esp_ota_mark_app_valid_cancel_rollback()` after a *successful network
refresh*. An image left `PENDING_VERIFY` reverts on the next reset — which during
colour calibration (poll URL parked, no server) would never be cleared. Writing
only `seq`+CRC and leaving `ota_state` at `VALID` arms no rollback timer.

**Do NOT put `components/esptool_py/esptool` on `PYTHONPATH`.** That directory's
`esptool.py` is a shim whose whole body re-invokes `python -m esptool`; putting it
ahead of site-packages makes `-m esptool` resolve back to the shim, which spawns
another, forever. It presents as a process hung on serial I/O at ~0 % CPU, and
killing the root PID does nothing because every child spawns its own. This took a
workstation down on 2026-08-12 (~20k processes in 28 minutes, reboot required).
If `parttool`/`otatool` report `No module named 'parttool'`, add **only**
`components/partition_table` — or use the plain-esptool recipe above instead.

## Coding / compiling
- Always `git commit` firmware code before building and flashing
- Never use ESP32 USB pins (leave in original state)
- Always verify no fast boot loop was introduced
- Firmware never auto-refreshes on boot; triggers: schedule, button press, first install
- `hard_reset` after flashing ESP32 automatically

## Reverse-engineering notes
- Stock firmware findings: `docs/screens/huessen_epf1301/reverse_engineering_overview.md` + per-version files
- New RE pass → update existing docs or add `docs/reverse_engineering_v<VER>_<DATE>.md`
- Binaries and scratch notes stay in `.private/`; digested findings go in `docs/`
- Hardware facts: `docs/screens/huessen_epf1301/hardware_facts.md` (may be inaccurate — treat with caution)
