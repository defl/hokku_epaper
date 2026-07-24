# Bigme F7 — Firmware Dump and Flash Procedure

Full 4 MB flash dump successfully obtained 2026-06-06 (repeated 2026-06-06 to verify automation).
Dumps are organised per physical unit under `.private/units/<serial>_<tag>/flash_full.bin`
(4,194,304 bytes, `AWIH` magic — valid XR872AT images). Two units dumped so far:
one factory unit and one provisioned unit (folders named `<serial>_<tag>`). See each unit's `NOTES.md`.

## Hardware Setup

- Connect via the **USB-C port** on the device (exposes the CH340 USB-serial bridge)
- COM port: **COM6** (Windows), VID 1A86:7523
- No soldering, no extra wires needed — the CH340 is already on the PCB

## Tool

**PhoenixMC v3.1.240901a** — the XRADIO/Allwinner flash tool.
(XRadioTech was acquired by Allwinner; the tool works for XR806/XR809/XR872 family.)

Copies archived at `.private/tools/`:
- `phoenixmc_v3.1.240901a/` ← used for the dump
- `phoenixmc_v3.1.23215d/`
- `phoenixmc_v3.1.21014b/`
- Also available at: https://github.com/openshwprojects/FlashTools/tree/main/XRadioTech-AllWinner

## Critical Setting: Baud Rate

**Set baud rate to 115200 in the PhoenixMC UI** (the ComboBox in the main window).

Do NOT use 921600. The CH340 bridge on the Bigme F7 PCB cannot sustain 921600 bps for bulk
data transfer — it drops bytes, causing every read to return `Read payload data error!`.
The handshake and Flash ID read succeed at 921600 (small transactions), but bulk reads always
fail. 115200 is reliable and reads 4 MB in ~5 minutes.

## settings.ini (phoenixmc_v3.1.240901a)

```ini
[comm]
strComDev = COM6
iBaud = 115200          ; must match UI selection

[setting]
bFlashCompat = 1
bUseNewBrom = 1         ; correct for BROM version 2
```

## Automated Procedure (preferred)

```
python tools/phoenixmc_open.py
```

This launches PhoenixMC, checks the COM6 listview, and opens the debug dialog automatically.
When it prints `Long-press the F7 power button...`, do so to enter BROM mode.
When the dialog shows `Open comm OK!`, run:

```
python tools/phoenixmc_read.py
```

This sets FLASH length to 4 MB, clicks 读取, and monitors progress. Takes ~5 minutes.
Output: `flash_A_0x0_L_0x400000.bin` in the PhoenixMC directory (~5 min, progress logged).

## UART-only Dump (no PhoenixMC GUI)

Confirmed 2026-06-19 on a working unit — produces a 4 MB image **byte-for-byte identical** to the
PhoenixMC dump (SHA256 `349c24c8…`), with no GUI automation and no button presses:

```
python tools/_dump_bigme_f7.py [out_dir]
```

- Auto-enters BROM via the awake firmware console (`upgrade\n` → watchdog reset → BROM), retrying
  until it syncs; if the device is asleep, a single button press wakes it.
- Dumps the whole 4 MB in 256 KB ReadSector frames chained on **one** BROM session (~6 min).
- `out_dir` is optional; pass a per-unit folder, e.g.
  `.private/units/<serial>_<tag>`.

This depends on the **sector-addressed** ReadSector/WriteSector semantics (addr = `byte>>9`,
length = sector count) — see "BROM command wire protocol" in [`hardware_facts.md`](hardware_facts.md).
An earlier version passed raw byte offsets and produced **corrupt** dumps (every chunk past
`0x40000` read 512× too far → erased/garbage blocks); the byte-vs-sector fix in
`python/hokku/common/xr872/flasher.py` resolved it. The `python/hokku/common/xr872/flasher.py` `write`/`erase` paths use the
same corrected addressing (WriteSector sector-indexed, 16 KB/frame; EraseFlash byte-addressed,
64 KB blocks) and were validated by a write→readback→erase round-trip on erased flash regions.

## Manual Procedure (fallback)

1. Plug the device in via USB-C. It will enumerate as COM6.
2. Launch `phoenixMC.exe` from `phoenixmc_v3.1.240901a/`.
3. In the main window:
   - Check the **COM6 checkbox** in the port list (left panel).
   - Set baud rate ComboBox to **115200**.
4. Click **调试** (Debug). The "flash operation" debug dialog opens.
   - It will show `Synchron error!` — that is normal while waiting for the device.
5. **Long-press the power button** on the device until it reboots into BROM mode.
   - The dialog status changes to `Open comm OK !`
   - If you accidentally short-press (sleep/wake), just long-press again — no power cycle needed.
6. In the "flash operation" dialog, **FLASH section**:
   - Set **长度 (length)**: `00400000` (4 MB)
   - Set **地址 (address)**: `00000000`
7. Click **读取** (Read) in the FLASH row.
8. Status changes to `Reading flash data...N%`. Wait ~5 minutes.
9. Output file: `flash_A_0x0_L_0x400000.bin` in the PhoenixMC directory.

## Flash Chip

- **JEDEC ID**: `0x5E4016` (Zbit Semiconductor, 4 MB NOR flash)
- **BROM version**: 2
- First bytes of dump: `41 57 49 48` = `AWIH` (XR872AT firmware image header, confirmed valid)

## What Fails and Why

| Symptom | Cause |
|---------|-------|
| `Read payload data error!` on every read | Baud rate set to 921600 — CH340 drops bytes |
| `Addr error!` | Address outside flash range (e.g. 0x10000000 is XIP virtual, not raw flash) |
| `Synchron error!` on open | Normal — device not yet in BROM mode; long-press power |
| `Please select a COM port!` | COM6 checkbox not checked in main window list |
| Device auto-connects to PhoenixMC; zero UART output after flashing | Write-without-erase corruption — see "Root Cause: Zero UART Output" below |
| UART boot log missing after USB power-cycle | USB re-enumeration (~925 ms) is slower than the boot log (~400 ms) — see "UART Capture Timing" |

## Automation Notes

Scripts: `tools/phoenixmc_open.py`, `tools/phoenixmc_read.py`

**Key constraints for automation:**
- Do NOT use `click_input()` — it moves the real mouse
- Do NOT use `ctypes.byref()` to pass struct pointers to PhoenixMC via `SendMessageW` —
  PhoenixMC.exe is **32-bit**; from 64-bit Python the pointer is truncated by WOW64, crashing it
- For `LVM_SETITEMSTATE` (listview checkbox): use `VirtualAllocEx` + `WriteProcessMemory` to
  write the `LVITEM` struct into the target process's own 32-bit address space, then pass that
  remote pointer. See `remote_lv_setstate()` in `phoenixmc_open.py`.
- **Use `PostMessageW` (not `SendMessageW`) for the 调试 button click.** PhoenixMC does
  synchronous serial I/O in its button-click handler (opening COM6, sending sync bytes, waiting
  for BROM). `SendMessageW` blocks until the handler returns — this can stall the Python process
  for several minutes if the BROM negotiation is slow. `PostMessageW` fires the click
  asynchronously; poll for the flash dialog to appear separately (see `phase_launch()` in
  `bigme_f7_restore_and_verify.py`).
- Dialog title varies by version: `"flash operation"` or `"phoenixMC"` (both handled)

**stdout buffering gotcha:** `bigme_f7_restore_and_verify.py` replaces `sys.stdout` with
`io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")` at module level. This
wrapper uses block buffering when stdout is a pipe (not a TTY), so all `print()` output is held
in an 8 KB buffer. Any script that imports from this module must call
`sys.stdout.reconfigure(line_buffering=True)` after the import to restore live output.

**Don't touch COM6 before PhoenixMC:** if you run any Python BROM v1 sync (sending `0x55` bytes)
and leave the device mid-handshake, PhoenixMC's v2 BROM negotiation can get confused and stall.
Always use `XR872Flasher.sys_reboot()` to return the device to a clean BROM-entry state before
launching PhoenixMC, or ensure no Python process has the port open.

## Flash Verification and OEM Restore (end-to-end)

To read back the current flash, verify it matches `xr_system.img`, flash the OEM dump, and
capture UART output — all in one step:

```
python tools/bigme_f7_restore_and_verify.py
```

Requires `pyserial` for the UART capture phase (`pip install pyserial`).  
The only manual action: **long-press the power button** when prompted to enter BROM mode.

Phases:
1. Launch PhoenixMC, select COM6, open debug dialog
2. Wait for BROM mode (`Open comm OK!`)
3. Read 4 MB flash → `flash_A_0x0_L_0x400000.bin` in PhoenixMC dir (~5 min)
4. Compare first 1 MB against `firmware/bigme_f7/image/xr872/xr_system.img`
5. Flash any unit's OEM image `.private/units/<serial>_<tag>/flash_full.bin` (4 MB) (~5 min). Dumps are effectively interchangeable: firmware is identical and the MAC lives in efuse (not flash), so the device keeps its own MAC / `BIGME_<MAC>` cloud ID / `XRZ_<MAC>` AP regardless. The only thing inherited from the donor dump is the in-flash config blob (`sn=` serial, prior owner's WiFi creds/email, cached picture) — overwritten on re-provision. Prefer the unit's *own* dump only to preserve its original serial/config.
6. Click reboot, close PhoenixMC, open COM6 at 115200 baud, capture 30 s of boot output

### OEM Restore from AND-Corrupted Flash

If the device flash has been AND-corrupted by a write-without-erase (device enters BROM mode
immediately on power-up — see "Root Cause: Zero UART Output" below), use:

```
python tools/_oem_restore.py
```

This performs a **full erase** before writing the OEM dump. Required when the corrupted flash
has bits cleared to 0 that need to be 1 in the OEM firmware (write-without-erase would leave
those bits at 0).

Phases:
1. Launch PhoenixMC (device auto-connects from BROM mode, no button press needed)
2. Read current 4 MB flash → `flash_readback_<timestamp>.bin`
3. Compare vs the matching unit's `.private/units/<serial>_<tag>/flash_full.bin` (prints diff count and first mismatches)
4. Full chip erase (4 MB)
5. Write OEM dump (4 MB, ~5 min)
6. Read back 4 MB → byte-by-byte compare vs OEM reference

If the device already has OEM firmware (comparison passes), the script exits without writing.

**Confirmed 2026-06-13**: after test-write corruption (915,426 differing bytes), OEM restore
completed with `VERIFICATION PASSED — all 4,194,304 bytes match OEM reference`.

## Building Custom Firmware

### Build environment

A Dockerfile is provided at `firmware/bigme_f7/Dockerfile`. Build the image once:

```
docker build -t hokku-xr872-builder firmware/bigme_f7/
```

Then build from `firmware/bigme_f7/gcc/`:

```
docker run --rm \
  -v "$PWD:/hokku" \
  -v "/path/to/xr872_sdk:/xr872_sdk" \
  -w /hokku/firmware/bigme_f7/gcc \
  hokku-xr872-builder \
  make build XR872_SDK=/xr872_sdk CC_DIR=/usr/bin IMAGE_TOOL=/xr872_sdk/tools/mkimage
```

On Windows with PowerShell (xr872_sdk is a sibling of hokku_epaper):

```powershell
$xr872 = "c:/path/to/xr872_sdk"
docker run --rm `
  -v "c:/path/to/hokku_epaper:/hokku" `
  -v "${xr872}:/xr872_sdk" `
  -w /hokku/firmware/bigme_f7/gcc `
  hokku-xr872-builder `
  make build XR872_SDK=/xr872_sdk CC_DIR=/usr/bin IMAGE_TOOL=/xr872_sdk/tools/mkimage
```

Output: `firmware/bigme_f7/image/xr872/xr_system.img` (~1 MB).

**Note**: `make build` (not just `make`) is required — the `image` target (invoked by `build`)
runs `mkimage` to produce `xr_system.img` from the linked binaries. Plain `make` only links.

## Flashing Custom Firmware

To write `firmware/bigme_f7/image/xr872/xr_system.img` back to the device:

### Full automated flash (preferred)

```
python tools/phoenixmc_flash_full.py
```

This launches PhoenixMC, checks the COM6 checkbox, opens the debug dialog, waits for the device
to auto-connect to BROM, erases `(img_size + 0xFFFF) & ~0xFFFF` bytes, writes `xr_system.img`,
then reboots and captures 30 seconds of UART output. No user interaction needed.

### Quick Flash (PhoenixMC already open and connected)

When PhoenixMC is already running and showing `Open comm OK!` in the debug dialog:

```
python tools/_flash_now.py
```

This script skips all setup — it finds the open dialog, erases `(img_size + 0xFFFF) & ~0xFFFF`
bytes, writes `xr_system.img`, then immediately reboots: it kills PhoenixMC via
`TerminateProcess` (releasing COM6 instantly), opens COM6 at 115200 baud within ~30 ms of the
reboot command, and captures 30 seconds of UART output.

### Manual Procedure (fallback)

1. Follow steps 1–5 of the Manual Read Procedure to open the debug dialog and connect.
2. In the FLASH section, ensure **地址** is `00000000`.
3. Click **写入** in the FLASH row. A file-open dialog appears.
4. Navigate to `firmware/bigme_f7/image/xr872/xr_system.img` and click Open.
5. Status changes to `Writing flash data...N%`, then `Write OK!` when done.
6. Click **reboot** in the SYSTEM section (or power-cycle the device).

### After Flashing

- The device auto-connects to BROM when PhoenixMC is open — no power-press needed.
- After reboot the device boots the new firmware and waits for WiFi provisioning.
- Provision WiFi via UART console: `net sta config <ssid> <password>` then `net sta enable`.

## Root Cause: Zero UART Output After Flashing

**Symptom**: after flashing `xr_system.img`, the device produced zero UART output on every boot
and immediately entered BROM mode whenever PhoenixMC was opened. The screen showed no change
from the OEM display state.

**Root cause: NOR flash write-without-erase produces bitwise-AND corruption.**

NOR flash bits start as 1 after erase. Programming sets bits 1→0. The only way to restore a bit
to 1 is a sector erase. If you write without erasing first, any bit that was 0 in the existing
content stays 0 even if the new data wants it to be 1. The result is `flash = AND(old, new)`.

The OEM firmware has non-FF bytes in the AWIH boot-section header's `priv` fields (offsets
0x28–0x33), which encode OTA parameters:

```
OEM priv[0] = 0x001000FF
OEM priv[1] = 0x00180000
OEM priv[2] = 0xFFFF05FC
```

Our `xr_system.img` sets all priv fields to 0xFF (unused). Writing without erase left OEM's 0
bits in place:

```
flash priv[2] = AND(0xFFFF05FC, 0xFFFF03FC) = 0xFFFF01FC
```

This corrupted the AWIH 64-byte section header. The XR872AT bootloader validates the header via
a 16-bit checksum (sum of all 64 bytes = 0xFFFF). The AND-corruption changed the checksum from
`0x3305` to `0x2300`, making it invalid.

**Bootloader response to an invalid header (`bl_upgrade()`):**

When `image_check_header()` returns `IMAGE_INVALID`, `bl_load_app_bin()` fails and the
bootloader calls `bl_upgrade()`. This sets the `PRCM_CPUA_BOOT_FROM_SYS_UPDATE` flag and
reboots. The BROM then enters silent upgrade mode — it sends no UART output and waits forever
for PhoenixMC sync bytes. This is why the device "auto-connected" immediately every time the
PhoenixMC debug dialog was opened.

**Fix: always erase before writing.** `phoenixmc_flash.py` and `tools/_flash_now.py` both erase
`(img_size + 0xFFFF) & ~0xFFFF` bytes (rounded up to the next 64 KB boundary) before clicking
写入. Confirmed by readback comparison: after erase+write, the flash matches `xr_system.img`
exactly in the header region.

The OEM firmware (a unit's `flash_full.bin`) can be re-flashed without an erase step because our
previously written all-FF priv fields already have all bits at 1, so the OEM's 0 bits program
cleanly.

## UART Capture Timing

**Problem with USB power-cycle**: the XR872AT outputs its boot log ~300–400 ms after reset. A
USB power-cycle forces the CH340 to re-enumerate, which takes ~925 ms on Windows. COM6 only
becomes available after enumeration — the entire boot log is lost before you can open the port.

**Solution: use PhoenixMC's BROM reboot button.**

The BROM reboot (clicking the `reboot` button in PhoenixMC's debug dialog) issues a reset
command over the already-open serial port. The CH340 stays powered and enumerated throughout.
COM6 never disappears. By immediately killing PhoenixMC via `TerminateProcess` after sending the
reboot command (which releases COM6 instantly) and opening `serial.Serial('COM6', 115200)` in
Python, COM6 is open within ~30 ms — well before the boot log appears.

This is why `tools/_flash_now.py` (and phase 6 of `bigme_f7_restore_and_verify.py`) uses
`TerminateProcess` rather than `w.close()`. `w.close()` sends `WM_CLOSE` but PhoenixMC does not
release the COM port synchronously; `TerminateProcess` is immediate.

## Confirmed Custom Firmware Boot Log

Captured 2026-06-07 using `tools/_flash_now.py` (erase + write + BROM reboot + immediate
COM6 capture). 906 bytes received in the 30 s window:

```
use default flash chip mJedec 0x0
[FD I]: mode: 0x4, freq: 96000000Hz, drv: 0
wlan information: R-XR_C10.08.52.64_01.80 Jul 6 2019 20:05:10
XRADIO Skylark SDK 1.2.2 Jun  7 2026 03:34:48
sram heap space [0x215818, 0x26dc00), total size 361448 Bytes
cpu clock 240000000 Hz  /  HF clock 40000000 Hz  /  XIP: enable
mac address: efuse: 18:9e:2d:f9:87:54 / in use: 48:73:c1:0d:f1:0d
hokku bigme-f7 firmware
WiFi provisioning: net sta config <ssid> <password>, then: net sta enable
```

The `XRADIO Skylark SDK 1.2.2 Jun  7 2026 03:34:48` timestamp confirms the build date.
XIP is enabled, the WLAN firmware loaded, and the device is waiting for WiFi provisioning.

## Confirmed End-to-End: WiFi → HTTP → EPD Refresh

Confirmed working 2026-06-07. After flashing, provision WiFi via UART:

```
net sta config <ssid> <password>
net sta enable
```

Full working UART log from provisioning through first EPD refresh:

```
net sta config <ssid> <password>
<ACK> 200 OK
net sta enable
<ACK> 200 OK
en1: Trying to associate with <bssid> (SSID='<ssid>' freq=2462 MHz)
en1: Associated with <bssid>
en1: WPA: Key negotiation completed with <bssid> [PTK=CCMP GTK=CCMP]
en1: CTRL-EVENT-CONNECTED - Connection to <bssid> completed [id=0 id_str=]
[net INF] netif is link up
[net INF] start DHCP...
hokku: net event 0x0000 data=0x00000000
[net INF] netif is up
[net INF] address: 192.168.6.199
[net INF] gateway: 192.168.6.254
[net INF] netmask: 255.255.255.0
hokku: static IP set  192.168.6.199  gw=192.168.6.254
[net INF] msg <network up>
hokku: net event 0x0012 data=0x00000000
hokku: network up  ip=192.168.6.199
hokku: refresh thread started
hokku: EPD init
hokku: GET http://192.168.6.226:8080/hokku/screen/
hokku: image received, refreshing display...
hokku: refresh done, sleeping 300 s
```

**Static IP bypass**: DHCP does not complete on this network (router filtering by MAC?). The
firmware sets static IP 192.168.6.199 directly in `NET_CTRL_MSG_WLAN_CONNECTED` by writing to
`nif->ip_addr`, `nif->netmask`, `nif->gw` directly (bypassing `netif_set_addr()` which fires
a spurious NETWORK_DOWN callback when called with a still-zero IP), then calling
`netif_set_up()`. This triggers `netif_status_callback()` with a valid IP, which fires
`NET_CTRL_MSG_NETWORK_UP` cleanly.

**lwIP 1.4.1 types**: XR872 SDK uses `__CONFIG_LWIP_VER ?= 10401` (lwIP 1.4.1) by default.
Use `ip_addr_t` and `IP4_ADDR()` macro — not `ip4_addr_t` / `ip4addr_aton()` which are lwIP 2.x
APIs. Use `ipaddr_ntoa(&nif->ip_addr)` to print IP addresses.

**Server format note**: the Hokku server at `http://192.168.6.226:8080/hokku/screen/` currently
serves Huessen EPF1301 format (960,000 bytes for 1200×1600 6-color display). The bigme_f7
firmware reads only the first 192,000 bytes and streams them to the EPD. The display does update
but shows the wrong image and wrong colors. Correct bigme_f7 server support (800×480 7-color
format, server-side `python/hokku/screens/bigme_f7/`) is the next step.
