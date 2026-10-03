# Agent rules — firmware

## Packaging
- Every build MUST produce `firmware/release/hokku-huessen_epf1301-<version>.bin` via `ci-build.sh` (merged: bootloader @0x0 + partition table @0x8000 + app @0x10000)
- otadata is not in the image; the flasher (`hokku.common.esp32.flasher`) writes a blank otadata @0x610000 alongside it so the bootloader boots `ota_0`
- Do NOT commit/release individual `bootloader.bin` / `partition-table.bin` / `hokku_epaper.bin`
- GitHub release must attach the merged file as the single firmware asset
- Setup tool aborts if no `hokku-huessen_epf1301-*.bin` asset is found

## Firmware versioning — `firmware/huessen_epf1301/VERSION`

Format `PROTOCOL.CONFIG.N`; bump rules in the root [`AGENTS.md`](../../AGENTS.md) → "Versioning — firmware".

The firmware's `CONFIG_VERSION` (`firmware/common/esp32/config.h`) is derived from `VERSION` via the CMake-generated `version.h` — do not edit it directly. The host-side copy is `CONFIG_VERSION` in `python/hokku/screens/huessen_epf1301/constants.py` (and `seeedstudio_e1004/constants.py`, which shares the schema); update it when `CONFIG` bumps.

Examples:
| `VERSION` | Meaning |
|---|---|
| `1.2.5` | protocol 1, NVS config v2, 5th change |
| `1.2.6` | bug fix — only N incremented |
| `1.3.7` | NVS schema changed — CONFIG and N incremented, host `CONFIG_VERSION` updated |
| `2.3.8` | wire protocol break (human approved) — PROTOCOL and N incremented |

## Display driver (DO NOT MODIFY)
- Do not touch: SPI init, CS management, BUSY polling, GPIO init, `epaper_reset`, `epaper_init_panel`, `epaper_send_panel`, `epaper_display_dual`, `epaper_wait_busy`
- GPIO0 (SPI CS) is a boot strapping pin — must be managed by SPI driver (`spics_io_num = PIN_EPAPER_CS`), never `gpio_set_level`
- GPIO7 (BUSY) has an external pull-up on the PCB; configure it as input with the internal pull-up DISABLED and never `gpio_reset_pin` it — an internal pull-up masks BUSY LOW (`docs/screens/huessen_epf1301/hardware_facts.md`)
- `display_message()` must use `split_and_display()` with identical buffer layout: first 480K = panel 1, second 480K = panel 2

## Flashing procedure
Root `AGENTS.md` STOP rules apply. A working unit is reflashed with the A/B recipe below.
- The setup tool / web flasher (first install) writes the merged image @0x0 (bootloader included) + blank otadata @0x610000, then NVS config @0x9000 — a bootloader write, so root rule 4 (explicit approval) applies to agents
- Restoring a factory dump (write at 0x0) is likewise a bootloader write; after one, wait 30 s before flashing our firmware
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
- New RE pass → update existing docs or add `docs/screens/huessen_epf1301/reverse_engineering_v<VER>_<DATE>.md`
- Binaries and scratch notes stay in `.private/`; digested findings go in `docs/`
- Hardware facts: `docs/screens/huessen_epf1301/hardware_facts.md` (may be inaccurate — treat with caution)
