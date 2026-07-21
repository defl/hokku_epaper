# Bigme F7 — Restore to Stock (surgical)

Restore a unit running our custom firmware back to its stock OEM image **without**
a full-chip erase — by rewriting only the flash blocks we actually changed and
never touching the bootloader. Done successfully on 2026-07-06.

Tool: [`tools/_surgical_oem_restore.py`](../../../tools/_surgical_oem_restore.py).

## Why surgical instead of a full erase

Our flashing/OTA only ever wrote **slot0, slot1, the OTA-cfg, the sysinfo WiFi
sector, and our config store** — the **bootloader (`0x0–0x8000`) was never
touched**. A full-chip erase (the PhoenixMC path, `tools/_oem_restore.py`)
momentarily blanks *everything including the bootloader*; if the write then fails,
the bootloader is gone and the unit is very hard to recover (mask-BROM entry on a
dead unit is unreliable — see [`hardware_facts.md`](hardware_facts.md)). A surgical
restore diffs against the OEM dump and rewrites only the differing blocks, so the
bootloader is provably untouched and there is **no brick window**.

## Prerequisites

- The unit must be running **our custom firmware** (it enters the mask-BROM via the
  `upgrade` command; **stock OEM does not answer `upgrade`**, so a unit already on
  stock can only be re-flashed via a long-press `0x55` catch or PhoenixMC).
- The unit's **own** OEM dump, 4 MB. Dumps are filed per-unit at
  `.private/screens/bigme_f7/units/<serial>/flash_full.bin`. Use the unit's *own*
  dump — the serial (`6000xxx`) lives in flash, so flashing another unit's dump
  changes the reported serial (the MAC/cloud-ID is in efuse and is preserved).

## Procedure

```bash
DUMP=.private/screens/bigme_f7/units/<serial>/flash_full.bin   # this unit's OWN 4 MB dump

# 1. Dry-run: enter BROM, read the current 4 MB, diff vs the OEM dump in 4 KB
#    blocks, print exactly what differs, then reboot back — writes NOTHING.
python tools/_surgical_oem_restore.py "$DUMP" --port COM7

# 2. Review the diff. Expect ONLY: slot0, slot1, OTA-cfg, sysinfo/wifi, config
#    store — and ZERO bootloader (<0x8000) blocks. In our run this was
#    469 blocks (~1.88 MB); the first diff was at 0x008000 (bootloader clean).

# 3. Write: re-enter BROM and erase(4K)+write+read-back-verify each differing
#    non-bootloader block. Stays in BROM afterwards (does NOT reboot).
python tools/_surgical_oem_restore.py "$DUMP" --port COM7 --write

# 4. Long-press the power button to cold-boot the restored stock firmware.
```

## Safety properties

- **Dry-run first** — you see exactly which blocks would change before any write.
- **Bootloader never written** (blocks `<0x8000` are diffed but skipped).
- **Per-block read-back verify** against the OEM dump; any mismatch aborts.
- **Verify-before-reboot** — the tool leaves the device in BROM; you power-cycle to
  boot. A mid-write serial drop leaves it safely in BROM (the tool is re-runnable —
  a re-run re-diffs and writes only what still differs).
- Result is **byte-identical to the OEM dump** across the whole chip (the unwritten
  regions were already OEM).

## After restore — what "stock" looks like

- The device boots the OEM firmware. **Its early boot log prints during the ~1 s
  USB re-enumeration blackout after a power-cycle**, so a UART watcher that
  reconnects afterward will see nothing — this is normal, not a failure.
- It no longer answers our `cfg` / `upgrade` console commands.
- It has the factory WiFi/cloud config (not your network), so it won't fetch new
  content, and e-paper retains its last image — the screen may look unchanged until
  it's set up via the Bigme app. This is expected for an un-provisioned unit.
- Re-flashing custom firmware onto a now-stock unit requires USB BROM entry via the
  **replug+press `0x55` catch** — a one-command pure-Python bootstrap
  (`tools/f7_initial_flasher.py`); see [`bootstrap.md`](bootstrap.md). (It can't OTA
  and won't answer `upgrade` until our firmware is back on.)
