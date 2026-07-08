# Bigme F7 — A/B Over-The-Air Firmware Update

End-to-end OTA for the F7: the device downloads a new firmware image over HTTP,
writes it into the inactive A/B slot, flips the boot pointer, and reboots — with
the try-boot rollback ([`custom_firmware.md`](custom_firmware.md#boot--ab-rollback-safety-net))
as the safety net. Both the firmware client and the server are model-aware, so the
F7 and the huessen ESP32 share the same OTA plumbing.

## Device side (`firmware_bigme_f7/main.c`)

The frame-state advertises `"ota":1`. On a `200` poll response carrying
`X-Firmware-Update: <version>`, `do_refresh()` ignores the image body and runs:

```c
ota_init();
ota_get_image(OTA_PROTOCOL_HTTP, url);   // url = <server>/hokku/firmware.bin?model=bigme_f7
ota_verify_image(OTA_VERIFY_NONE, NULL); // flips the OTA-cfg to the freshly-written slot
ota_reboot();
```

- **Served image = the full `xr_system.img`, verbatim.** The SDK OTA discards the
  first `bl_size` (`0x8000`) bytes itself and writes the rest to the inactive
  slot, so no server-side slicing is needed. The image has no verify trailer
  (built without `mkimage -O`), so `VERIFY_NONE` is correct — `ota_get_image` still
  runs the per-section checksum walk, so a truncated/corrupt download is rejected
  before any cfg flip.
- `libota.a` links unconditionally (`-lota`), so **no `__CONFIG_OTA=y` is needed**
  (that flag only appends the verify trailer).
- The update slot alternates by A/B policy: running seq0 → writes slot1
  (`0x181000`); running seq1 → writes slot0 (`0x8000`). Both directions are valid.

### Bootstrapping

The *first* OTA-capable image must be flashed over **USB** — a pre-OTA image (or
stock OEM) has no OTA client to receive `X-Firmware-Update`. After that, updates
are over-the-air. Reflashing a stock unit needs USB too (stock OEM can't OTA). The
one-time fresh-unit bootstrap is documented in [`bootstrap.md`](bootstrap.md).

## Server side (model-aware)

- `python/hokku/screens/bigme_f7/firmware.py` serves `xr_system.img` verbatim and
  resolves the version from a `<img>.version` sidecar or `main.c`'s
  `FIRMWARE_VERSION`.
- `python/hokku/screens/firmware_registry.py` maps `model_id` → firmware provider
  (`bundled_firmware_version` / `release_app_image`). huessen and bigme_f7 both
  register.
- `flask_app.py`: `/hokku/firmware.bin?model=…`, the `X-Firmware-Update` signal,
  the version comparison, and `/api/status`'s `bundled_firmware_versions` map all
  resolve per-model. `/hokku/firmware.bin` defaults to the huessen reference model
  when no model is given (back-compat). The NVS config path stays huessen-only
  (the F7 has no NVS — its config is device-local FDCM).

### Triggering an update

- **Web GUI:** the screen's *Config* modal → "Update firmware on next refresh".
  The UI compares each screen against **its own model's** bundled version
  (`bundled_firmware_versions[model]`) — a bug where it compared against the single
  (huessen) global was fixed.
- **API:** `POST /hokku/api/screens/<name>/update {"enabled": true}`.
- **Console (test):** the `ota` command on the device.

### Bounded auto-retry

The pending flag is **not** consumed one-shot. For an **upgrade** the server
re-signals `X-Firmware-Update` on every poll until the device reports the target
version — so a transient failure (e.g. a dropped WiFi download) self-heals — capped
at `OTA_MAX_ATTEMPTS` (5) signals, after which it records an `ota_error`. A
same-version **re-flash** stays one-shot (no version change to confirm).

## Adversarial review

Two independent Opus reviews + on-hardware testing found and fixed four defects
before/while flashing. All are in the committed firmware.

| # | Severity | Defect | Fix |
|---|---|---|---|
| 1 | near-brick (feature-broken) | XIP bias hardcoded to slot-0's `app_xip` — a slot-1 (OTA'd) image would execute slot-0 code and fault → rollback, *every* OTA | per-slot bias `0x13040 + seq*0x179000` |
| 2 | flash corruption | no mutex between the console `ota` and the refresh thread (two flash writers / HTTP sessions) | `g_ota_lock` mutex; console try-locks |
| 3 | latent brick | rollback would repoint the boot cfg at a slot an aborted OTA had already erased (blank) | arm only if `image_check_sections(fallback)==VALID` |
| 4 | brick on a non-A/B header | `hokku_do_ota` trusted the bootloader-derived erase address (a `ota_addr=0xFFFFFFFF` header wraps to erase slot0/bootloader) | range-guard the update slot to `[bl_size, 0x300000)`; **allow both A/B directions** (an earlier fix wrongly hardcoded slot1 and refused reverse-direction OTAs — caught on hardware) |

Non-brick items accepted: no hardware watchdog covers the ~1 MB download (bounded
instead by the SDK HTTP client's 180 s inactivity timeout; a hang is power-cycle
recoverable since the cfg flip is last).

## Verified on hardware

OTA proven in **both directions** (seq0↔seq1) via **both triggers** (console `ota`
and the server `X-Firmware-Update` path), with per-slot XIP, rollback-commit, and a
correct battery reading throughout. First fully user-driven web-GUI OTA: `1.2.0` →
`1.2.1`.

## Consequence for the A/B slots

The first OTA overwrites the inactive slot, which on a fresh unit holds the **OEM
factory firmware**. After that, both slots hold A/B versions of our firmware and
the rollback protects between them; the OEM is recoverable only via USB + a saved
dump (see [`restore_to_stock.md`](restore_to_stock.md)). This tradeoff was made
deliberately.
