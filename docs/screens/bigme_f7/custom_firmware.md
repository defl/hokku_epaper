# Bigme F7 — Custom Hokku Firmware

Custom firmware that turns the Bigme F7 (XRADIO XR872AT) into a first-class Hokku
screen, at feature parity with the production ESP32 firmware: rich reporting,
runtime configuration, USB-aware deep sleep + battery, and A/B over-the-air
updates. Source lives in [`firmware_bigme_f7/`](../../../firmware_bigme_f7);
build notes in [`firmware_build.md`](firmware_build.md); the first working bring-up
is in the git history (`3f21de6`, `2d4f701` … onward on branch `defl/v4`).

Related docs: [`hardware_facts.md`](hardware_facts.md) (pins, battery, display),
[`display_driver.md`](display_driver.md) (EK79655 / Spectra-6),
[`firmware_build.md`](firmware_build.md) (Docker/GCC build),
[`ota.md`](ota.md) (the A/B OTA design + safety),
[`restore_to_stock.md`](restore_to_stock.md) (surgical OEM restore).

## Feature parity with the ESP32 firmware

| Capability | How it works on the F7 |
|---|---|
| **Reporting** | `POST /hokku/screen/` with the activity-log ring as the body, a rich `X-Frame-State` JSON (`fw`, `uptime_s`, `heap_kb`, `rssi`, `regime`, `wake`, `cfg_ver`, `clk_now`, `bat_mv`, `ota`), `X-Firmware-Version` / `X-Firmware-Build`, and a software wall-clock anchored to `X-Server-Time-Epoch`. |
| **Configuration** | FDCM-backed `hokku_config` (`hokku_config.c`) at flash `0x340000`: server URL, screen name, static-IP/DHCP, `power_mode`, default sleep. Provisioned over the UART console (`cfg` command group), versioned (`cfg_ver`), forward-migrating. WiFi creds live separately in **sysinfo** (fdcm) via the `wifi` command. |
| **Power / battery** | USB-aware hibernation: stay awake on USB, deep-sleep on battery (`power_mode` = auto/sleep/awake). `pm_enter_mode(PM_MODE_HIBERNATION)` + `HAL_Wakeup_SetTimer_Sec`; requires `PRJCONF_PM_EN=1` **and** `PRJCONF_NET_PM_EN=1` (WiFi is powered off before hibernate — without this it reset-loops). Battery via ADC ch4/PA14 (see [`hardware_facts.md`](hardware_facts.md#voltage-sensing)). |
| **OTA** | A/B slot update over HTTP — see [`ota.md`](ota.md). |

## Boot + A/B rollback safety net

Every boot is protected by a **try-boot rollback** so a bad image (ours or an
OTA'd one) can never brick the unit:

- `platform_init_level0()` is a **strong override** (runs from SRAM, before XIP is
  up). It captures the booted slot, points the OTA-cfg at the *other* slot, arms a
  16 s `WDG_EVT_RESET` watchdog, then brings up XIP. If the image faults before
  reaching a healthy milestone, the watchdog resets → the bootloader boots the
  other slot.
- `hokku_rollback_commit()` (early in `main()`) points the cfg back at our own
  slot and stops the watchdog once boot is proven healthy.
- The rollback only arms if the fallback slot passes `image_check_sections()` — an
  OTA erases its target slot up front, so a failed OTA can leave it blank; we never
  arm the watchdog into a blank slot.

**XIP is per-slot.** `OPI_MEM_CTRL->BIAS_ADDR0` (`0x4000B088`) must point at the
booted slot's `app_xip`: `0x13040 + boot_seq * 0x179000` (slot0 `0x13040`, slot1
`0x18C040`). A fixed slot-0 offset would fault any image running from slot 1 — this
was the near-brick the adversarial review caught (see [`ota.md`](ota.md#adversarial-review)).

## Flash layout (on-device, from the OEM bootloader header)

| Region | Range | Contents |
|---|---|---|
| Bootloader | `0x0 – 0x8000` | OEM bootloader (`bl_size=0x8000`) — **never written** by our tools |
| slot0 (app) | `0x8000 – 0x180000` | app-chain (app.bin + app_xip + wlan blobs) |
| OTA cfg | `0x180000 – 0x181000` | fdcm A/B config (which slot is active) |
| slot1 | `0x181000 – 0x300000` | the other A/B slot |
| sysinfo | `0x300000 – …` | WiFi creds (fdcm) — `PRJCONF_SYSINFO_ADDR=0x300000` |
| hokku_config | `0x340000 – 0x341000` | our app-config fdcm store |

`sysinfo` was relocated to `0x300000` (the SDK default `0xFF000` overlaps slot 0).

## Versions

`FIRMWARE_VERSION` in `main.c`. Bring-up/test iterations ran `1.0.0` → `1.1.4`;
`1.2.0` was the first clean release, `1.2.1` the first fully user-driven web-GUI
OTA. The version is reported in `X-Firmware-Version` and the frame-state `fw`.

## Console commands (UART @115200)

- `wifi <ssid> <password>` — persist WiFi creds to sysinfo + connect.
- `cfg show | server <url> | name <n> | ip <ip> <gw> <nm> | dhcp | static | sleep <s> | power <auto|sleep|awake> | save`
- `ota` — trigger an OTA from the configured server right now (test hook).
- `upgrade` — SDK command that drops to the mask-BROM (used by the flashers).
  **Only our firmware answers `upgrade`; stock OEM does not.**

## Build & flash

Build: `firmware_build.md`. First-time flash of a unit (bootstrap) is USB-only via
the safe slot-0 flashers (`tools/flash_candidate_slot0*.py`) — the pre-OTA image
must be flashed over USB because it has no OTA client yet. After that, updates go
over the air. See [`ota.md`](ota.md) and [`restore_to_stock.md`](restore_to_stock.md).
