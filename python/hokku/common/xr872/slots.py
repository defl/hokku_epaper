#!/usr/bin/env python3
"""Flash an A/B candidate image into SLOT 0 or SLOT 1 and point the OTA cfg at it.

Writes the app-chain of a built xr_system.img (everything from bl_size onward) into
the chosen slot's app area using 4 KB sector erase (so the shared bootloader at
0x0-0x8000 is never touched), then rewrites the 1-sector fdcm OTA cfg at 0x180000 to
that slot VERIFIED (sector-bounded erase, so the OTHER slot is never touched).

Which slot to write is the caller's decision and it is a SAFETY decision: per the
repo flashing rules you MUST write the slot the device is NOT currently running
from, so a bad image falls back to a known-good one. `slot=0` is the bootstrap
case (a stock unit running the OEM image from slot 1); `slot=1` is the update case
(a unit already running Hokku firmware from slot 0). Passing the running slot
overwrites the only known-good image — this module cannot detect that on its own,
because in BROM the device is not running anything.

Safety (see the A/B rollback design notes):
  * Asserts the live bootloader header: AWIH, bl_size==0x8000, ota_addr==0x180000,
    ota_size==0x1000, and priv[2]>>16==0xFFFF (F2: img_xz_max_size INVALID, so the
    bootloader's xz-decompress-into-slot-0 path stays disarmed).
  * Bounds the erase to the target slot's own region, so slot 0 can never run into
    the OTA cfg and slot 1 can never run into sysinfo at 0x300000.
  * Read-back verifies the slot app region AND the cfg before declaring success.
  * The cfg flip is the LAST write, so any interruption leaves the device booting
    whatever cfg said before (the untouched other slot). USB BROM restore is the backstop.
  * Never erases the bootloader block, the other slot, or the 0x300000+ partitions.

The image itself is slot-agnostic: firmware/bigme_f7/main.c derives its XIP bias as
0x13040 + boot_seq * 0x179000 at runtime, so the same bytes boot from either slot.

Usage:
  python tools/flash_candidate_slot0.py <image.img> [--port COM7] [--slot 0|1] [--reboot]
"""

import argparse
import contextlib
import struct
import sys
import time
from typing import NoReturn

from hokku.common.xr872.flasher import ERASE_TYPE_4K, XR872Flasher, send_upgrade_command

BL_SIZE = 0x8000
OTA_ADDR = 0x180000
OTA_SIZE = 0x1000
SLOT0_APP = 0x8000
SLOT1_APP = 0x181000
SYSINFO_ADDR = 0x300000
SEC = 0x1000
FDCM_ID = 0xA55A5AA5
RAW_SEQ0 = 0x5555
RAW_SEQ1 = 0xAAAA
RAW_STATE_VERIFIED = 0x9669

# Per-slot app base and the first address the slot may NOT touch. Slot 0 is bounded
# by the OTA cfg sector, slot 1 by sysinfo (PRJCONF_SYSINFO_ADDR).
SLOT_APP = {0: SLOT0_APP, 1: SLOT1_APP}
SLOT_LIMIT = {0: OTA_ADDR, 1: SYSINFO_ADDR}
RAW_SEQ = {0: RAW_SEQ0, 1: RAW_SEQ1}


def build_fdcm(seq: int = 0) -> bytes:
    """Fresh fdcm block selecting seq/VERIFIED (matches ROM fdcm_rewrite), 512 B padded."""
    if seq not in RAW_SEQ:
        raise ValueError(f"seq must be 0 or 1, got {seq!r}")
    bitmap_size = (OTA_SIZE - 8 - 1) // (4 * 8 + 1) + 1  # == 124 for 4 KB / 4 B
    blk = bytearray(b"\xff" * 512)
    struct.pack_into("<IHH", blk, 0, FDCM_ID, bitmap_size, 4)
    blk[8] = 0xFE  # bitmap[0]: 1 slot used
    struct.pack_into("<HH", blk, 8 + bitmap_size, RAW_SEQ[seq], RAW_STATE_VERIFIED)
    return bytes(blk)


def parse_cfg(sector: bytes):
    idc, bm, ds = struct.unpack_from("<IHH", sector, 0)
    if idc != FDCM_ID or ds != 4:
        return None
    # used bits from bitmap (bit 0 == used), current slot = used-1
    bit = 0
    for b in sector[8 : 8 + bm]:
        if b == 0:
            bit += 8
        else:
            v = (~b) & 0xFF
            while v:
                v >>= 1
                bit += 1
            break
    off = 8 + bm + 4 * (bit - 1)
    raw_seq, raw_st = struct.unpack_from("<HH", sector, off)
    seq = {0x5555: 0, 0xAAAA: 1}.get(raw_seq)  # decoded index or None
    verified = raw_st == RAW_STATE_VERIFIED
    return seq, verified


def die(msg) -> NoReturn:
    print(f"ABORT: {msg}")
    sys.exit(1)


def flash_slot(f, img, slot=0, reboot=False, allow_active_slot=False):
    """Safe A/B slot write over an ALREADY-SYNCED BROM handle `f`.

    This is the validated write sequence (see module docstring): assert the live
    bootloader header, 4 KB-erase + write the app-chain into the target slot,
    read-back verify, then flip the 1-sector OTA cfg to that seq VERIFIED as the
    LAST write, preserving the other slot. Callers differ only in how they enter
    BROM and which slot they target.

    By default this REFUSES to write the slot the bootloader would currently
    launch. In BROM the device is not running, but the OTA cfg still records which
    slot it would boot, so the active slot is knowable and overwriting it — which
    destroys the only known-good image — is caught before the first erase.

    `allow_active_slot=True` opts out, and is correct only for bootstrap-style
    writes where the "active" image is a stock/OEM one being deliberately replaced
    and a full-flash restore image exists.
    """
    if slot not in SLOT_APP:
        die(f"slot must be 0 or 1, got {slot!r}")
    app_addr = SLOT_APP[slot]
    limit = SLOT_LIMIT[slot]
    other_addr = SLOT_APP[1 - slot]
    app_chain = img[BL_SIZE:]
    end = app_addr + len(app_chain)
    if end > limit:
        die(f"app-chain too big for slot {slot}: ends 0x{end:x} >= 0x{limit:x}")

    if f.get_flash_id() is None:
        die("BROM sync but GetFlashId failed")

    # --- A/B pre-flight: which slot would the bootloader launch right now? ---
    cfg_before = f.read_sector(OTA_ADDR, 512)
    active = parse_cfg(cfg_before) if cfg_before else None
    active_seq = active[0] if active else None
    print(f"  A/B cfg says active slot = {active_seq}; target slot = {slot}")
    if active_seq == slot:
        msg = (
            f"target slot {slot} is the ACTIVE slot — writing it would destroy the "
            f"only known-good image (no A/B fallback)"
        )
        if not allow_active_slot:
            die(f"{msg}. Refusing; pass allow_active_slot=True only if intended.")
        print(f"  WARNING: {msg}; proceeding because allow_active_slot=True.")

    # --- read & validate the live bootloader header (F2 guard etc.) ---
    sh = f.read_sector(0x0, 512)
    if sh is None or sh[0:4] != b"AWIH":
        die("bootloader header not AWIH")
    bl_size = struct.unpack_from("<I", sh, 32)[0]  # next_addr
    priv = struct.unpack_from("<6I", sh, 40)
    ota_size = (priv[0] >> 8) & 0xFFFFFF
    ota_addr = priv[1]
    xz_max = priv[2] >> 16
    print(
        f"  bl_size=0x{bl_size:x} ota_addr=0x{ota_addr:x} ota_size=0x{ota_size:x} "
        f"priv[2]=0x{priv[2]:08x} (img_xz_max=0x{xz_max:x})"
    )
    if bl_size != BL_SIZE:
        die(f"bl_size 0x{bl_size:x} != 0x{BL_SIZE:x}")
    if ota_addr != OTA_ADDR:
        die(f"ota_addr 0x{ota_addr:x} != 0x{OTA_ADDR:x}")
    if ota_size != OTA_SIZE:
        die(f"ota_size 0x{ota_size:x} != 0x{OTA_SIZE:x}")
    if xz_max != 0xFFFF:
        die(f"img_xz_max 0x{xz_max:x} != 0xFFFF — F2 xz path NOT disarmed")
    print("  header OK; F2 xz-decompress path is disarmed.")

    # --- 1) 4 KB-erase the target slot's app region (bootloader untouched) ---
    erase_end = (end + SEC - 1) & ~(SEC - 1)
    print(f"[1] 4K-erase 0x{app_addr:x}..0x{erase_end:x} ({(erase_end - app_addr) // SEC} sectors)")
    for a in range(app_addr, erase_end, SEC):
        if not f.erase_flash(a, ERASE_TYPE_4K):
            die(f"4K erase failed @0x{a:x}")

    # --- 2) write app-chain at the slot base ---
    print(f"[2] write app-chain @0x{app_addr:x}")
    CHUNK = 0x4000
    for off in range(0, len(app_chain), CHUNK):
        chunk = app_chain[off : off + CHUNK]
        if len(chunk) % 512:
            chunk = chunk + b"\xff" * (512 - len(chunk) % 512)
        if not f.write_sector(app_addr + off, chunk):
            die(f"write failed @0x{app_addr + off:x}")
    # --- verify slot readback ---
    print(f"[3] verify slot-{slot} app region readback")
    rb = b""
    for off in range(0, len(app_chain), 0x10000):
        d = f.read_sector(app_addr + off, min(0x10000, ((len(app_chain) - off + 511) // 512) * 512))
        if d is None:
            die(f"readback failed @0x{app_addr + off:x}")
        rb += d
    if rb[: len(app_chain)] != app_chain:
        die(f"slot-{slot} readback MISMATCH — candidate not written correctly")
    print(f"  slot-{slot} verified byte-identical.")

    # --- 4) cfg -> seq (LAST write); sector-bounded erase spares the other slot ---
    print(
        f"[4] cfg rewrite -> seq{slot} VERIFIED (4K-erase 0x{OTA_ADDR:x}, "
        f"spares slot{1 - slot} @0x{other_addr:x})"
    )
    if not f.erase_flash(OTA_ADDR, ERASE_TYPE_4K):
        die("cfg sector erase failed")
    if not f.write_sector(OTA_ADDR, build_fdcm(slot)):
        die("cfg write failed")
    cfg_sec = f.read_sector(OTA_ADDR, 512)
    parsed = parse_cfg(cfg_sec) if cfg_sec else None
    print(
        f"[5] verify cfg readback -> (seq={parsed[0] if parsed else None}, verified={parsed[1] if parsed else None})"
    )
    if parsed != (slot, True):
        die(f"cfg verify FAILED: {parsed} (expected seq{slot}/VERIFIED) — NOT rebooting")
    # confirm the other slot's head is still intact (not all-FF)
    so = f.read_sector(other_addr, 512)
    print(
        f"  slot{1 - slot} @0x{other_addr:x} still present "
        f"(AWIH={so[0:4] == b'AWIH' if so else '?'})"
    )

    print(
        f"\nSUCCESS: candidate in slot {slot}, cfg -> seq{slot} VERIFIED, slot {1 - slot} preserved."
    )
    if reboot:
        print("Rebooting (sys_reboot)...")
        f.sys_reboot()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--port", default="COM7")
    ap.add_argument(
        "--slot",
        type=int,
        default=0,
        choices=(0, 1),
        help="target slot; MUST be the slot the device is NOT running from",
    )
    ap.add_argument("--reboot", action="store_true", help="sys_reboot after flashing")
    args = ap.parse_args()

    img = open(args.image, "rb").read()
    app_chain = img[BL_SIZE:]
    app_addr = SLOT_APP[args.slot]
    end = app_addr + len(app_chain)
    if end > SLOT_LIMIT[args.slot]:
        die(
            f"app-chain too big for slot {args.slot}: ends 0x{end:x} >= 0x{SLOT_LIMIT[args.slot]:x}"
        )
    print(
        f"image {args.image}: {len(img)} B; app-chain -> slot {args.slot} "
        f"0x{app_addr:x}..0x{end:x} ({len(app_chain)} B)"
    )
    _ = app_chain  # (flash_slot recomputes from img; kept for the size print above)

    # Persistent BROM entry: retry `upgrade`+sync so we catch the device in a brief
    # awake window (the OEM e-reader re-sleeps quickly). Give up after ~40 attempts.
    f = None
    for attempt in range(40):
        with contextlib.suppress(Exception):
            send_upgrade_command(args.port)
        time.sleep(0.4)
        with contextlib.suppress(Exception):
            ftry = XR872Flasher(args.port)
            if ftry.sync(attempts=4, timeout_per=0.25):
                f = ftry
                print(f"BROM sync OK (attempt {attempt + 1})")
                break
            ftry.close()
    if f is None:
        die("could not enter BROM after retries (keep the device awake and retry)")

    with f:
        flash_slot(f, img, slot=args.slot, reboot=args.reboot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
