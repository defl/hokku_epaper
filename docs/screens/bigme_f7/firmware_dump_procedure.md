# Bigme F7 — Firmware Dump Procedure

Full 4 MB flash dump successfully obtained 2026-06-06.
Dump stored at `.private/screens/bigme_f7/flash_dump.bin` (4,194,304 bytes, `AWIH` magic — valid XR872AT image).

## Hardware Setup

- Connect via the **USB-C port** on the device (exposes the CH340 USB-serial bridge)
- COM port: **COM6** (Windows), VID 1A86:7523
- No soldering, no extra wires needed — the CH340 is already on the PCB

## Tool

**PhoenixMC v3.1.240901a** — the XRADIO/Allwinner flash tool.
(XRadioTech was acquired by Allwinner; the tool works for XR806/XR809/XR872 family.)

Copies archived at `.private/screens/bigme_f7/tools/`:
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

## Step-by-Step Procedure

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

## Automation Notes

Python automation via `pywinauto` works for the dialog interaction once the dialog is open:
- Use `set_edit_text()` + `SendMessageW(handle, BM_CLICK, 0, 0)` for all dialog actions
- Do NOT use `click_input()` — it moves the real mouse
- Scripts: `tools/phoenixmc_open.py`, `tools/phoenixmc_read.py`

The COM6 listview checkbox and main window button must be clicked manually — automated
approaches caused PhoenixMC instability in testing.
