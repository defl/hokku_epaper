# Bigme F7 — Bootstrapping a fresh unit to Hokku firmware

Getting our firmware onto a **fresh/stock F7 for the first time** is a one-time,
per-unit USB operation. After it, the unit is a normal Hokku screen and everything
else — firmware updates, config — happens **over-the-air** ([`ota.md`](ota.md)); it
never needs USB again.

This is **not** exposed in the web "Flash a screen" GUI (which is ESP32/esptool
only). The F7's mask-BROM can't be entered from Python over the CH340, so the
bootstrap needs the Windows vendor tool for the BROM entry. The GUI recognises an
F7 (CH340) and points here instead of trying to esptool-flash it.

## Why USB (and why not the GUI)

- A stock unit runs OEM firmware, which does **not** answer the `upgrade` console
  command — so there's no software path into the mask-BROM.
- The only remaining entry is the **`0x55` catch during a power-cycle**, and pure
  Python **cannot** hit it: the CH340 re-enumerates ~1.1 s after power-on, by which
  time the mask-BROM UART sync window has already closed. **Confirmed non-working
  2026-07-07** (`tools/_catch_test.py` — hammered `0x55` across real power-cycles,
  never caught). Only the vendor GUI tool, with its own driver-level timing, hits
  the window.
- The vendor tool is **Windows-only**, so it can't run on the Pi the server
  normally lives on — hence a documented local procedure rather than a GUI button.

## Procedure

Prereqs: Windows host, the vendor flash tool installed, the F7 on USB (CH340), and
a built `firmware_bigme_f7/image/xr872/xr_system.img` ([`firmware_build.md`](firmware_build.md)).

1. **Enter the mask-BROM.** Launch the vendor tool and **long-press the power
   button** to power-cycle; the tool catches the BROM window (repeat the long-press
   until it connects). This is the step nothing else can do. The
   `tools/phoenixmc_*.py` helpers (pywinauto GUI automation, same pattern as
   `tools/_oem_restore.py`) drive it headlessly.
2. **Write our firmware.** With the device sitting in BROM, install the app-chain
   into slot 0. Safest is the validated `flash_slot0` write (slot-0 only, cfg
   written last, bootloader + slot 1 untouched) from `tools/flash_candidate_slot0.py`
   — pointed at the already-in-BROM device (skip its `upgrade` entry; the device is
   already in BROM). The vendor tool's own full-image write also works.
   > On-hardware detail to confirm on the next real bootstrap: whether to hand the
   > in-BROM device from the vendor tool to `flash_slot0` (close the tool, then
   > `XR872Flasher(port).sync()`), or write the image straight from the vendor tool.
3. **Cold-boot** (long-press) into the new firmware.
4. **Provision over the UART console** (115200) — no persistent WiFi exists yet:
   ```
   wifi <ssid> <password>          # persists creds to sysinfo + connects
   cfg server http://<server>/hokku/screen/
   cfg name <screen name>
   cfg save
   ```
   (`cfg dhcp` or `cfg ip <ip> <gw> <nm>` if not using the compiled-in static IP.)
5. **Done — OTA from here.** The unit now reports to the server and takes all
   future firmware updates over-the-air. See [`ota.md`](ota.md).

## Reversing it (back to stock)

Once on our firmware the unit answers `upgrade`, so it can be surgically restored
to its stock OEM image without a full erase — see
[`restore_to_stock.md`](restore_to_stock.md). (That, of course, removes the
`upgrade` path again and returns it to "needs the vendor tool" for the next
bootstrap.)
