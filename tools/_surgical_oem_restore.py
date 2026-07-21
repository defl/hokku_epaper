#!/usr/bin/env python3
"""Surgically restore a Bigme F7 to a stock OEM dump — writing ONLY the flash
blocks that differ, and NEVER touching the bootloader (0x0-0x8000).

Unlike a full-chip erase+write (PhoenixMC), this reads the current 4 MB, diffs it
against the OEM dump in 4 KB blocks, and erase+writes only the blocks that differ
(slot0/slot1/OTA-cfg/sysinfo/our config store — i.e. exactly what we changed).
The bootloader is diffed but never written, so there is no bootloader-brick window.

BROM entry uses the `upgrade` console command, so this needs the device running
firmware that answers `upgrade` (our custom fw does; stock OEM does NOT).

Safety:
  * Dry-run by default: reads + diffs + reports, then reboots — writes nothing.
  * --write: for each differing non-bootloader 4 KB block, erase(4K)+write+readback
    verify. Any block that fails verification aborts immediately.
  * Never erases/writes < BL_SIZE (bootloader). If the bootloader region differs it
    is reported and SKIPPED.
  * Stays in BROM after a write (does NOT reboot) — verify-before-reboot; you
    long-press to boot the restored stock firmware.

Usage:
  python tools/_surgical_oem_restore.py <oem_dump.bin> [--port COM7]        # dry-run
  python tools/_surgical_oem_restore.py <oem_dump.bin> [--port COM7] --write
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import sys
import time

from xr872_flasher import ERASE_TYPE_4K, XR872Flasher, send_upgrade_command

BL_SIZE = 0x8000  # bootloader region [0, 0x8000) — never touched
FLASH_SIZE = 0x400000  # 4 MB
BLK = 0x1000  # 4 KB diff/erase granularity
READ_CHUNK = 0x10000  # 64 KB reads


def enter_brom(port: str) -> XR872Flasher | None:
    """Retry `upgrade`+sync to catch the device's BROM window (needs custom fw)."""
    for attempt in range(40):
        with contextlib.suppress(Exception):
            send_upgrade_command(port)
        time.sleep(0.4)
        with contextlib.suppress(Exception):
            f = XR872Flasher(port)
            if f.sync(attempts=4, timeout_per=0.25):
                print(f"BROM sync OK (attempt {attempt + 1})")
                return f
            f.close()
    return None


def read_flash(f: XR872Flasher, size: int) -> bytes:
    out = bytearray()
    for off in range(0, size, READ_CHUNK):
        n = min(READ_CHUNK, size - off)
        d = None
        for _try in range(3):
            d = f.read_sector(off, n)
            if d is not None and len(d) == n:
                break
            time.sleep(0.2)
        if d is None or len(d) != n:
            raise RuntimeError(f"read failed @0x{off:x}")
        out += d
        if off % 0x80000 == 0:
            print(f"  read 0x{off:06x}  ({off * 100 // size}%)")
    print(f"  read 0x{size:06x}  (100%)")
    return bytes(out)


def contiguous_ranges(blocks: list[int]) -> list[tuple[int, int]]:
    """Collapse sorted block start-addrs into (start, end) byte ranges."""
    ranges: list[tuple[int, int]] = []
    for a in blocks:
        if ranges and a == ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], a + BLK)
        else:
            ranges.append((a, a + BLK))
    return ranges


def label(addr: int) -> str:
    if addr < BL_SIZE:
        return "bootloader"
    if addr < 0x180000:
        return "slot0 (app)"
    if addr < 0x181000:
        return "OTA-cfg"
    if addr < 0x300000:
        return "slot1"
    if addr < 0x340000:
        return "sysinfo/wifi"
    if addr < 0x400000:
        return "config store"
    return "?"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dump", help="OEM reference dump (4 MB)")
    ap.add_argument("--port", default="COM7")
    ap.add_argument("--write", action="store_true", help="actually erase+write (default: dry-run)")
    args = ap.parse_args()

    oem = open(args.dump, "rb").read()
    if len(oem) != FLASH_SIZE:
        return _die(f"dump is {len(oem)} B, expected {FLASH_SIZE}")
    print(f"OEM dump: {args.dump}")
    print(f"  size {len(oem):,} B  sha256 {hashlib.sha256(oem).hexdigest()[:8].upper()}")
    print(f"mode: {'WRITE (erase+restore)' if args.write else 'DRY-RUN (read+diff only)'}\n")

    print(f"Entering BROM on {args.port} (needs running custom fw that answers `upgrade`)...")
    f = enter_brom(args.port)
    if f is None:
        return _die("could not enter BROM (keep the device awake; is it running our fw?)")

    with f:
        if f.get_flash_id() is None:
            return _die("BROM sync but GetFlashId failed")
        print("reading current 4 MB flash (~5-6 min @115200)...")
        cur = read_flash(f, FLASH_SIZE)

        diffs = [a for a in range(0, FLASH_SIZE, BLK) if cur[a : a + BLK] != oem[a : a + BLK]]
        bl_diffs = [a for a in diffs if a < BL_SIZE]
        app_diffs = [a for a in diffs if a >= BL_SIZE]

        print(f"\n{len(diffs)} differing 4 KB blocks ({len(diffs) * BLK // 1024} KB):")
        for start, end in contiguous_ranges(diffs):
            print(f"  0x{start:06x}..0x{end:06x}  ({(end - start) // 1024:>5} KB)  {label(start)}")
        if bl_diffs:
            print(
                f"\n  ** {len(bl_diffs)} block(s) in the BOOTLOADER region differ — will be SKIPPED **"
            )

        if not args.write:
            print("\nDRY-RUN complete — nothing written. Rebooting device back to normal.")
            f.sys_reboot()
            return 0

        if not app_diffs:
            print("\nNothing to restore outside the bootloader. Rebooting.")
            f.sys_reboot()
            return 0

        print(f"\nRestoring {len(app_diffs)} block(s) (bootloader untouched)...")
        for i, a in enumerate(app_diffs):
            if not f.erase_flash(a, ERASE_TYPE_4K):
                return _die(f"erase failed @0x{a:x} (still in BROM — safe to re-run)")
            if not f.write_sector(a, oem[a : a + BLK]):
                return _die(f"write failed @0x{a:x} (still in BROM — safe to re-run)")
            rb = f.read_sector(a, BLK)
            if rb is None or rb[:BLK] != oem[a : a + BLK]:
                return _die(f"VERIFY MISMATCH @0x{a:x} — NOT rebooting")
            if i % 64 == 0 or i == len(app_diffs) - 1:
                print(f"  {i + 1}/{len(app_diffs)}  @0x{a:06x}  ({label(a)})")

        print(f"\nSUCCESS: {len(app_diffs)} block(s) restored + verified byte-identical to OEM.")
        print("Bootloader NOT touched. NOT rebooting (verify-before-reboot).")
        print(">>> Long-press the power button to cold-boot the restored stock firmware. <<<")
    return 0


def _die(msg: str) -> int:
    print(f"ABORT: {msg}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
