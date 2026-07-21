"""Read current flash, compare with OEM reference, then erase+write OEM dump.

Wraps bigme_f7_restore_and_verify.py helpers but forces erase=True before
writing the OEM dump — required here because our flash has AND-corrupted test
data that cannot be corrected without erase.

Steps:
  1. Launch PhoenixMC (device already in BROM mode, connects immediately)
  2. Read current 4 MB flash  →  flash_readback_<ts>.bin
  3. Compare byte-by-byte against the OEM reference dump
  4. ERASE 4 MB  (full chip to handle our AND-corrupted test data)
  5. WRITE OEM dump (4 MB)
  6. Read back 4 MB and compare again  →  final verification
"""

import ctypes
import pathlib
import shutil
import sys
import time

from pywinauto import Desktop

# Import helpers from the existing restore script (that module patches sys.stdout)
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from bigme_f7_restore_and_verify import (
    _flash_image,
    already_connected_dialog,
    get_hex_edit_fields,
    phase_launch,
    phase_wait_brom,
)

# bigme_f7_restore_and_verify replaces sys.stdout with a non-line-buffered
# TextIOWrapper. Reconfigure to line_buffering=True so output appears live
# when stdout is captured by a shell redirect.
sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]

from _private import res  # noqa: E402

ROOT = pathlib.Path(__file__).parents[1]
PHOENIXMC_DIR = res("bigme_flash_tool_dir")
OEM_DUMP = res("bigme_oem_dump")
READBACK_DIR = PHOENIXMC_DIR

FLASH_SIZE = 0x400000  # 4 MB


def _readback_file():
    ts = time.strftime("%Y%m%d_%H%M%S")
    return PHOENIXMC_DIR / f"flash_readback_{ts}.bin"


def phase_read_and_compare(dlg, out_file: pathlib.Path) -> bool:
    BM_CLICK = 0xF5

    print()
    print("=" * 60)
    print("PHASE 2 — Read back 4 MB flash")
    print("=" * 60)

    # Remove stale output
    standard_out = res("bigme_flash_tool_readback")
    if standard_out.exists():
        standard_out.unlink()

    hex_edits = get_hex_edit_fields(dlg)
    if len(hex_edits) < 4:
        raise RuntimeError(f"Expected >=4 hex edit fields, got {len(hex_edits)}")

    hex_edits[2][1].set_edit_text("00400000")
    hex_edits[3][1].set_edit_text("00000000")
    print(f"FLASH length -> {hex_edits[2][1].window_text()!r}")
    print(f"FLASH addr   -> {hex_edits[3][1].window_text()!r}")

    # Find FLASH 读取 button (second one by Y position)
    def wins():
        return [
            w
            for w in Desktop(backend="win32").windows()
            if w.class_name() not in ("tooltips_class32",)
        ]

    read_btns = sorted(
        [
            (c.rectangle().top, c)
            for c in dlg.children()
            if c.class_name() == "Button" and "读取" in c.window_text()
        ],
        key=lambda x: x[0],
    )
    if not read_btns:
        raise RuntimeError("No 读取 button found")
    read_btn = read_btns[1][1] if len(read_btns) > 1 else read_btns[0][1]
    print(f"Clicking FLASH 读取 (y={read_btn.rectangle().top})...")
    ctypes.windll.user32.SendMessageW(read_btn.handle, BM_CLICK, 0, 0)

    print("Reading 4 MB at 115200 baud (~5 min)...")
    deadline = time.monotonic() + 420
    last_sz = -1
    last_pct = -1
    while time.monotonic() < deadline:
        time.sleep(2)
        if standard_out.exists():
            sz = standard_out.stat().st_size
            pct = sz * 100 // FLASH_SIZE
            if sz != last_sz:
                if pct != last_pct and pct % 10 == 0:
                    print(f"  {pct}%  ({sz:,} bytes)")
                    last_pct = pct
                last_sz = sz
            if sz >= FLASH_SIZE:
                print(f"  100%  ({sz:,} bytes)")
                break
    else:
        raise TimeoutError("Flash readback timed out")

    # Copy to timestamped file
    shutil.copy2(standard_out, out_file)
    print(f"Saved: {out_file}")

    # Compare
    print()
    print("=" * 60)
    print("PHASE 3 — Compare vs OEM reference")
    print("=" * 60)
    ref = OEM_DUMP.read_bytes()
    dump = out_file.read_bytes()
    n = min(len(ref), len(dump))

    if ref[:n] == dump[:n]:
        print(f"MATCH — all {n:,} bytes identical to OEM reference")
        return True

    diffs = [i for i in range(n) if ref[i] != dump[i]]
    print(f"MISMATCH — {len(diffs):,} differing bytes out of {n:,}")
    print("First 20 differences  (offset | flash | ref):")
    for off in diffs[:20]:
        print(f"  0x{off:08X}  flash=0x{dump[off]:02X}  ref=0x{ref[off]:02X}")
    if len(diffs) > 20:
        print(f"  ... and {len(diffs) - 20:,} more")
    return False


def phase_flash_oem_with_erase(dlg):
    print()
    print("=" * 60)
    print("PHASE 4+5 — Erase + Write OEM dump (4 MB, with erase)")
    print("=" * 60)
    if not OEM_DUMP.exists():
        raise FileNotFoundError(f"OEM dump not found: {OEM_DUMP}")
    print(f"OEM dump: {OEM_DUMP}  ({OEM_DUMP.stat().st_size:,} bytes)")
    print("NOTE: forcing erase=True — flash has AND-corrupted test data")
    _flash_image(dlg, OEM_DUMP, "OEM", erase=True)


def main():
    for path, _name in [
        (OEM_DUMP, "OEM reference dump"),
        (res("bigme_flash_tool_exe"), "flash tool"),
    ]:
        if not path.exists():
            print(f"ERROR: required file not found: {path}")
            sys.exit(1)

    print("Bigme F7 — OEM firmware restore with pre-erase")
    print(f"OEM source: {OEM_DUMP}")
    print()

    # Phase 1 — connect
    dlg = already_connected_dialog()
    if dlg:
        print("PhoenixMC already connected!")
        print(f"Dialog: {dlg.window_text()!r}")
    else:
        dlg = phase_launch()
        phase_wait_brom(dlg)

    out_file = _readback_file()

    # Phase 2+3 — read current flash and compare
    match_before = phase_read_and_compare(dlg, out_file)
    if match_before:
        print()
        print("Device already has correct OEM firmware — no write needed.")
        print("Done.")
        return

    # Phase 4+5 — erase + write OEM
    phase_flash_oem_with_erase(dlg)

    # Phase 6 — read back and verify
    print()
    print("=" * 60)
    print("PHASE 6 — Read back and verify after write")
    print("=" * 60)
    out_verify = PHOENIXMC_DIR / "flash_verify_after_write.bin"
    if out_verify.exists():
        out_verify.unlink()

    # Re-read by clicking 读取 again (device still in BROM mode)
    BM_CLICK = 0xF5
    hex_edits = get_hex_edit_fields(dlg)
    hex_edits[2][1].set_edit_text("00400000")
    hex_edits[3][1].set_edit_text("00000000")

    standard_out = res("bigme_flash_tool_readback")
    if standard_out.exists():
        standard_out.unlink()

    read_btns = sorted(
        [
            (c.rectangle().top, c)
            for c in dlg.children()
            if c.class_name() == "Button" and "读取" in c.window_text()
        ],
        key=lambda x: x[0],
    )
    if not read_btns:
        print("WARNING: no 读取 button — skipping final verification")
    else:
        read_btn = read_btns[1][1] if len(read_btns) > 1 else read_btns[0][1]
        print("Clicking FLASH 读取 for verification...")
        ctypes.windll.user32.SendMessageW(read_btn.handle, BM_CLICK, 0, 0)
        print("Reading 4 MB for final verification (~5 min)...")
        deadline = time.monotonic() + 420
        last_sz = -1
        while time.monotonic() < deadline:
            time.sleep(2)
            if standard_out.exists():
                sz = standard_out.stat().st_size
                if sz != last_sz:
                    pct = sz * 100 // FLASH_SIZE
                    if pct % 10 == 0:
                        print(f"  {pct}%")
                    last_sz = sz
                if sz >= FLASH_SIZE:
                    print("  100%")
                    break
        else:
            print("WARNING: verification read timed out")

        if standard_out.exists():
            shutil.copy2(standard_out, out_verify)
            ref = OEM_DUMP.read_bytes()
            dump = out_verify.read_bytes()
            n = min(len(ref), len(dump))
            diffs = [i for i in range(n) if ref[i] != dump[i]]
            if not diffs:
                print(f"VERIFICATION PASSED — all {n:,} bytes match OEM reference!")
            else:
                print(f"VERIFICATION FAILED — {len(diffs):,} differing bytes")
                for off in diffs[:10]:
                    print(f"  0x{off:08X}  flash=0x{dump[off]:02X}  ref=0x{ref[off]:02X}")

    print()
    print("=" * 60)
    print("OEM restore complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()
