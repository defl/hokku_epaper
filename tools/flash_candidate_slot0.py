#!/usr/bin/env python3
"""Flash an A/B candidate image into SLOT 0 and point the OTA cfg at it — safely.

Writes the app-chain of a built xr_system.img (everything from bl_size onward) to
the slot-0 app area at 0x8000 using 4 KB sector erase (so the shared bootloader at
0x0-0x8000 is never touched), then rewrites the 1-sector fdcm OTA cfg at 0x180000 to
seq 0 VERIFIED (sector-bounded erase, so slot 1 at 0x181000 is never touched).

Safety (see the A/B rollback design notes):
  * Asserts the live bootloader header: AWIH, bl_size==0x8000, ota_addr==0x180000,
    ota_size==0x1000, and priv[2]>>16==0xFFFF (F2: img_xz_max_size INVALID, so the
    bootloader's xz-decompress-into-slot-0 path stays disarmed).
  * Read-back verifies the slot-0 app region AND the cfg before declaring success.
  * The cfg flip is the LAST write, so any interruption leaves the device booting
    whatever cfg said before (the live OEM in slot 1). USB BROM restore is the backstop.
  * Never erases the bootloader block, slot 1, or the 0x300000 config partition.

Usage:
  python tools/flash_candidate_slot0.py <image.img> [--port COM7] [--reboot]
"""

import argparse
import struct
import sys
import time

from xr872_flasher import ERASE_TYPE_4K, XR872Flasher, send_upgrade_command

BL_SIZE = 0x8000
OTA_ADDR = 0x180000
OTA_SIZE = 0x1000
SLOT0_APP = 0x8000
SEC = 0x1000
FDCM_ID = 0xA55A5AA5
RAW_SEQ0 = 0x5555
RAW_STATE_VERIFIED = 0x9669


def build_fdcm_seq0() -> bytes:
    """Fresh fdcm block selecting seq0/VERIFIED (matches ROM fdcm_rewrite), 512 B padded."""
    bitmap_size = (OTA_SIZE - 8 - 1) // (4 * 8 + 1) + 1  # == 124 for 4 KB / 4 B
    blk = bytearray(b"\xff" * 512)
    struct.pack_into("<IHH", blk, 0, FDCM_ID, bitmap_size, 4)
    blk[8] = 0xFE  # bitmap[0]: 1 slot used
    struct.pack_into("<HH", blk, 8 + bitmap_size, RAW_SEQ0, RAW_STATE_VERIFIED)
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


def die(msg):
    print(f"ABORT: {msg}")
    sys.exit(1)


def flash_slot0(f, img, reboot=False):
    """Safe A/B slot-0 write over an ALREADY-SYNCED BROM handle `f`.

    This is the validated write sequence (see module docstring):
    assert the live bootloader header, 4 KB-erase + write the app-chain into slot 0,
    read-back verify, then flip the 1-sector OTA cfg to seq0 VERIFIED as the LAST
    write, preserving slot 1 (OEM). Callers differ only in how they enter BROM.
    """
    app_chain = img[BL_SIZE:]
    end = SLOT0_APP + len(app_chain)
    if end > OTA_ADDR:
        die(f"app-chain too big: ends 0x{end:x} >= ota 0x{OTA_ADDR:x}")

    if f.get_flash_id() is None:
        die("BROM sync but GetFlashId failed")

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

    # --- 1) 4 KB-erase slot-0 app region (bootloader at 0x0-0x8000 untouched) ---
    erase_end = (end + SEC - 1) & ~(SEC - 1)
    print(
        f"[1] 4K-erase 0x{SLOT0_APP:x}..0x{erase_end:x} ({(erase_end - SLOT0_APP) // SEC} sectors)"
    )
    for a in range(SLOT0_APP, erase_end, SEC):
        if not f.erase_flash(a, ERASE_TYPE_4K):
            die(f"4K erase failed @0x{a:x}")

    # --- 2) write app-chain at 0x8000 ---
    print(f"[2] write app-chain @0x{SLOT0_APP:x}")
    CHUNK = 0x4000
    for off in range(0, len(app_chain), CHUNK):
        chunk = app_chain[off : off + CHUNK]
        if len(chunk) % 512:
            chunk = chunk + b"\xff" * (512 - len(chunk) % 512)
        if not f.write_sector(SLOT0_APP + off, chunk):
            die(f"write failed @0x{SLOT0_APP + off:x}")
    # --- verify slot-0 readback ---
    print("[3] verify slot-0 app region readback")
    rb = b""
    for off in range(0, len(app_chain), 0x10000):
        d = f.read_sector(
            SLOT0_APP + off, min(0x10000, ((len(app_chain) - off + 511) // 512) * 512)
        )
        if d is None:
            die(f"readback failed @0x{SLOT0_APP + off:x}")
        rb += d
    if rb[: len(app_chain)] != app_chain:
        die("slot-0 readback MISMATCH — candidate not written correctly")
    print("  slot-0 verified byte-identical.")

    # --- 4) cfg -> seq0 (LAST write); sector-bounded erase spares slot1 ---
    print(f"[4] cfg rewrite -> seq0 VERIFIED (4K-erase 0x{OTA_ADDR:x}, spares slot1 @0x181000)")
    if not f.erase_flash(OTA_ADDR, ERASE_TYPE_4K):
        die("cfg sector erase failed")
    if not f.write_sector(OTA_ADDR, build_fdcm_seq0()):
        die("cfg write failed")
    cfg_sec = f.read_sector(OTA_ADDR, 512)
    parsed = parse_cfg(cfg_sec) if cfg_sec else None
    print(
        f"[5] verify cfg readback -> (seq={parsed[0] if parsed else None}, verified={parsed[1] if parsed else None})"
    )
    if parsed != (0, True):
        die(f"cfg verify FAILED: {parsed} (expected seq0/VERIFIED) — NOT rebooting")
    # confirm slot1 head still intact (not all-FF)
    s1 = f.read_sector(0x181000, 512)
    print(f"  slot1 @0x181000 still present (AWIH={s1[0:4] == b'AWIH' if s1 else '?'})")

    print("\nSUCCESS: candidate in slot 0, cfg -> seq0 VERIFIED, slot 1 preserved.")
    if reboot:
        print("Rebooting (sys_reboot)...")
        f.sys_reboot()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--port", default="COM7")
    ap.add_argument("--reboot", action="store_true", help="sys_reboot after flashing")
    args = ap.parse_args()

    img = open(args.image, "rb").read()
    app_chain = img[BL_SIZE:]
    end = SLOT0_APP + len(app_chain)
    if end > OTA_ADDR:
        die(f"app-chain too big: ends 0x{end:x} >= ota 0x{OTA_ADDR:x}")
    print(
        f"image {args.image}: {len(img)} B; app-chain 0x{SLOT0_APP:x}..0x{end:x} ({len(app_chain)} B)"
    )
    _ = app_chain  # (flash_slot0 recomputes from img; kept for the size print above)

    # Persistent BROM entry: retry `upgrade`+sync so we catch the device in a brief
    # awake window (the OEM e-reader re-sleeps quickly). Give up after ~40 attempts.
    f = None
    for attempt in range(40):
        try:
            send_upgrade_command(args.port)
        except Exception:
            pass
        time.sleep(0.4)
        try:
            ftry = XR872Flasher(args.port)
            if ftry.sync(attempts=4, timeout_per=0.25):
                f = ftry
                print(f"BROM sync OK (attempt {attempt + 1})")
                break
            ftry.close()
        except Exception:
            pass
    if f is None:
        die("could not enter BROM after retries (keep the device awake and retry)")

    with f:
        flash_slot0(f, img, reboot=args.reboot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
