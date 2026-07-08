# Bigme F7 — Bootstrapping a fresh unit to Hokku firmware

Getting our firmware onto a **fresh/stock F7 for the first time** is a one-time,
per-unit USB operation. After it, the unit is a normal Hokku screen and everything
else — firmware updates, config — happens **over-the-air** ([`ota.md`](ota.md)); it
never needs USB again.

It is a **fully pure-Python** procedure — no vendor tool required — driven by
`tools/f7_initial_flasher.py`. **Proven end-to-end 2026-07-07** on unit 6000135.

## Why USB (and the entry trick)

- A stock unit runs OEM firmware, which does **not** answer the `upgrade` console
  command — so there's no software path into the mask-BROM. The only entry is the
  **`0x55` catch during a power-cycle**.
- The catch is timing-critical: the mask-BROM opens a short UART sync window right
  after power-on. The trick that makes it reliable is the **trigger**:
  - **Correct: USB replug + power press.** Unplug the cable, replug it, *then* press
    the power button. The replug brings the CH340 port up **first**, so we can hold
    it open and hammer `0x55` straight through the sync window — no re-enumeration
    gap. This works from plain Python.
  - **Wrong: long-press.** A long-press power-cycle drops the CH340 and it takes
    ~1.1 s to re-enumerate — by then the BROM window has closed and the catch misses.
    (Our earlier "catch is impossible from Python" conclusion was this wrong trigger,
    now corrected.)

`tools/_catch_test.py` is the non-destructive probe that confirms the catch (reads
the flash ID, writes nothing).

## Procedure

Prereqs: the F7 on USB (CH340), and a built
`firmware_bigme_f7/image/xr872/xr_system.img` ([`firmware_build.md`](firmware_build.md)).
Runs on any platform (incl. the Pi) — no Windows/vendor tool needed.

```
python tools/f7_initial_flasher.py --port COM7
```

1. **Enter the mask-BROM.** When prompted: **unplug USB → replug USB → short-press
   the power button**, and repeat until it prints `BROM SYNC CAUGHT`. This is the
   only step that needs a physical action.
2. **Write our firmware (automatic).** The flasher hands the in-BROM device to the
   validated `flash_slot0` write: app-chain into **slot 0 only**, read-back verified,
   the A/B cfg flipped to seq0 **last**, and the **bootloader + slot 1 left
   untouched**. Nothing outside slot 0 + its cfg sector is written.
3. **Power-cycle to boot.** `sys_reboot` only re-enters BROM on this chip, so the
   flasher does **not** auto-reboot — **unplug/replug USB (or long-press)** to
   cold-boot the app. The e-paper stays on its old image until WiFi is set (it only
   redraws once it can fetch an image), so use the console to confirm the boot.
4. **Provision over the UART console** (115200) — no persistent WiFi exists yet:
   ```
   wifi <ssid> <password>          # persists creds to sysinfo + connects
   cfg save                        # server URL + name default from the compiled-in config
   ```
   (`cfg server <url>` / `cfg name <n>` / `cfg dhcp` / `cfg ip <ip> <gw> <nm>` to
   override the compiled-in defaults.) `cfg show` verifies the running firmware.
5. **Done — OTA from here.** The unit associates, fetches an image, redraws, reports
   to the server, and takes all future firmware updates over-the-air. See
   [`ota.md`](ota.md).

### Fallbacks (Windows-only, if the pure-Python catch won't land)

- `--phoenixmc` — the vendor GUI catches the BROM (driven headlessly via
  `pywinauto`), is then killed to free the port, and the in-BROM device is handed to
  the same `flash_slot0` write.
- `--full <oem_dump>` — vendor-tool full-erase + write of a composed 4 MB image (OEM
  base + our app in slot 0 + cfg→seq0). Momentarily blanks the bootloader during the
  erase, so it's the last resort.

## From the web GUI

The "Flash a screen" page also drives this bootstrap. Scan for devices, pick the
F7 (recognised by the CH340 VID/PID — it shows a **Bootstrap F7** panel instead of
the ESP32 form), enter the **Wi-Fi + screen name**, and press **Bootstrap F7**. The
server writes slot 0 via the identical `flash_slot0`, then **provisions Wi-Fi/config
over the console** for you (see below), streaming progress into the log with a
**Cancel** button. It's the same one-slot job as the ESP32 flash (scanning is
refused while it runs).

Wi-Fi provisioning caveats: the F7 supports a **single** network (no fallback yet),
and because the console tokenizer splits on whitespace, the **SSID, password, and
screen name can't contain spaces** (the GUI + server reject spaces up front). Leave
the fields blank to skip provisioning and set Wi-Fi by hand later.

Entry is two-phase, so the physical dance is usually unnecessary:

1. **No-touch `upgrade` entry first.** If the unit already runs Hokku firmware, the
   server sends `upgrade` over the console → watchdog reset into the BROM, no replug
   or press needed. This is the common case for re-flashing an existing unit (though
   note such a unit can also just take an **OTA** with no USB at all — see
   [`ota.md`](ota.md)).
2. **Manual catch fallback.** Only if `upgrade` gets no answer (a stock unit) does
   the log ask for the **unplug → replug → press power** catch, repeated until it
   lands.

So a stock unit needs the replug+press; a unit already on our firmware doesn't.

After the write, if you supplied Wi-Fi/config the log asks you to **power-cycle** the
unit (needed to boot on this chip regardless). The server then waits for the console
to come up and writes `cfg server`/`cfg name`/`cfg save` + `wifi <ssid> <psk>`
(password never logged), and briefly watches for the join + first server POST.

Server bits: `POST /hokku/api/flash/start_f7` + `/hokku/api/flash/cancel`,
`hokku/screens/bigme_f7/bootstrap.py` (wraps the `tools/` primitives; only available
where the dev-tree `tools/` dir is present — it gates on `tooling_available()` and
returns 503 otherwise, e.g. on a packaged Pi install), and `FlashJobManager.start_f7`.

## Reversing it (back to stock)

Once on our firmware the unit answers `upgrade`, so it can be surgically restored to
its stock OEM image without a full erase — see [`restore_to_stock.md`](restore_to_stock.md).
(That removes the `upgrade` path again and returns it to "needs the BROM catch" for
the next bootstrap.)
