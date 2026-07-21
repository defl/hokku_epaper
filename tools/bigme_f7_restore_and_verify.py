"""End-to-end Bigme F7 flash verification and OEM restore.

Fully automated — no button press or user interaction required.

If our custom firmware is already flashed, the XR872AT bootloader will have called
bl_upgrade() and the device is silently waiting in BROM mode; PhoenixMC connects
immediately.  If the device is running other firmware, a DTR toggle is attempted first
to trigger a hardware reset into BROM mode.

Steps:
  1. DTR toggle on COM6 (attempts to reset device into BROM mode)
  2. Launch PhoenixMC, select COM6, open debug dialog
  3. Wait silently for BROM connection ('Open comm OK!')
  4. Read back full 4 MB flash  → save + compare against xr_system.img
  5. Flash OEM 4 MB dump        → wait for Write OK!
  6. Click reboot, close PhoenixMC, capture UART at 115200 baud for 30 s

Usage:
    python tools/bigme_f7_restore_and_verify.py
"""

import ctypes
import io
import pathlib
import subprocess
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

try:
    import serial

    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False
    print("WARNING: pyserial not installed — UART capture will be skipped")
    print("         pip install pyserial")

from pywinauto import Desktop  # noqa: E402

from _private import res  # noqa: E402

# ── Paths ──────────────────────────────────────────────────────────────────────

ROOT = pathlib.Path(__file__).parents[1]
# Private resources resolved via tools/_private.py (no .private paths in tracked code)
PHOENIXMC_DIR = res("bigme_flash_tool_dir")
PHOENIXMC_EXE = res("bigme_flash_tool_exe")
OUR_IMAGE = ROOT / "firmware/bigme_f7/image/xr872/xr_system.img"
OEM_DUMP = res("bigme_oem_dump")
READBACK_FILE = res("bigme_flash_tool_readback")

COM_PORT = "COM6"
BAUD_RATE = 115200

# ── Win32 constants ────────────────────────────────────────────────────────────

BM_CLICK = 0x00F5
WM_SETTEXT = 0x000C
LVM_GETITEMCOUNT = 0x1004
LVM_SETITEMSTATE = 0x102B
LVIF_STATE = 0x0008
LVIS_SELECTED = 0x0002
LVIS_FOCUSED = 0x0001
LVIS_STATEIMAGEMASK = 0xF000
PROCESS_ALL_ACCESS = 0x1F0FFF
MEM_COMMIT_RESERVE = 0x3000
PAGE_READWRITE = 0x04
MEM_RELEASE = 0x8000


class LVITEM32(ctypes.Structure):
    _fields_ = [
        ("mask", ctypes.c_uint32),
        ("iItem", ctypes.c_int32),
        ("iSubItem", ctypes.c_int32),
        ("state", ctypes.c_uint32),
        ("stateMask", ctypes.c_uint32),
        ("pszText", ctypes.c_uint32),
        ("cchTextMax", ctypes.c_int32),
        ("iImage", ctypes.c_int32),
        ("lParam", ctypes.c_int32),
    ]


# ── Helpers ────────────────────────────────────────────────────────────────────


def wins():
    return [
        w for w in Desktop(backend="win32").windows() if w.class_name() not in ("tooltips_class32",)
    ]


def win_handles():
    return {w.handle for w in wins()}


def remote_lv_setstate(lv_hwnd, item_index, state, state_mask):
    pid = ctypes.c_ulong(0)
    ctypes.windll.user32.GetWindowThreadProcessId(lv_hwnd, ctypes.byref(pid))
    proc = ctypes.windll.kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid.value)
    if not proc:
        raise RuntimeError(f"OpenProcess failed: {ctypes.GetLastError()}")
    size = ctypes.sizeof(LVITEM32)
    remote = ctypes.windll.kernel32.VirtualAllocEx(
        proc, None, size, MEM_COMMIT_RESERVE, PAGE_READWRITE
    )
    if not remote:
        ctypes.windll.kernel32.CloseHandle(proc)
        raise RuntimeError(f"VirtualAllocEx failed: {ctypes.GetLastError()}")
    item = LVITEM32()
    item.mask = LVIF_STATE
    item.iItem = item_index
    item.state = state
    item.stateMask = state_mask
    written = ctypes.c_size_t(0)
    ctypes.windll.kernel32.WriteProcessMemory(
        proc, remote, ctypes.byref(item), size, ctypes.byref(written)
    )
    ctypes.windll.user32.SendMessageW(lv_hwnd, LVM_SETITEMSTATE, item_index, remote)
    ctypes.windll.kernel32.VirtualFreeEx(proc, remote, 0, MEM_RELEASE)
    ctypes.windll.kernel32.CloseHandle(proc)


def listview_check_item(lv_ctrl, index=0):
    remote_lv_setstate(
        lv_ctrl.handle, index, LVIS_SELECTED | LVIS_FOCUSED, LVIS_SELECTED | LVIS_FOCUSED
    )
    remote_lv_setstate(lv_ctrl.handle, index, 0x2000, LVIS_STATEIMAGEMASK)


def get_hex_edit_fields(dlg):
    return sorted(
        [
            (c.rectangle().top, c)
            for c in dlg.children()
            if c.class_name() == "Edit"
            and len(c.window_text().strip()) == 8
            and all(ch in "0123456789abcdefABCDEF" for ch in c.window_text().strip())
        ],
        key=lambda x: x[0],
    )


def get_flash_dialog():
    return next(
        (
            w
            for w in wins()
            if w.window_text() in ("flash operation", "phoenixMC") and len(w.children()) > 5
        ),
        None,
    )


def get_dialog_status(dlg):
    NOISE = {
        "写入",
        "写入ram",
        "写入info",
        "读取",
        "擦除",
        "flash id",
        "reboot",
        "memory",
        "flash",
        "extern",
        "system",
        "comm",
    }
    texts = []
    for c in dlg.children():
        try:
            t = c.window_text().strip()
            if t and t.lower() not in NOISE and len(t) > 3:
                texts.append(t)
        except Exception:  # noqa: S110
            pass
    return texts


def inject_file_dialog(before_handles, image_path, timeout=8):
    """Wait for a file-open dialog, inject path, click Open."""
    file_dlg = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for w in wins():
            if w.handle in before_handles:
                continue
            try:
                cls = w.class_name()
                txt = w.window_text()
                if cls in ("#32770", "NativeHWNDHost") or any(
                    kw in txt.lower() for kw in ("open", "打开", "select", "file")
                ):
                    file_dlg = w
                    break
            except Exception:  # noqa: S110
                pass
        if file_dlg:
            break
        time.sleep(0.1)

    if not file_dlg:
        raise RuntimeError("file-open dialog did not appear")

    time.sleep(0.3)

    # Find filename Edit field (may be direct child or inside ComboBox)
    filename_edit = None
    for c in file_dlg.children():
        try:
            if c.class_name() == "Edit":
                filename_edit = c
                break
            if c.class_name() in ("ComboBoxEx32", "ComboBox"):
                for cc in c.children():
                    if cc.class_name() == "Edit":
                        filename_edit = cc
                        break
                if filename_edit:
                    break
        except Exception:  # noqa: S110
            pass
    if not filename_edit:
        for c in file_dlg.children():
            try:
                for cc in c.children():
                    if cc.class_name() == "Edit":
                        filename_edit = cc
                        break
            except Exception:  # noqa: S110
                pass
            if filename_edit:
                break

    if not filename_edit:
        raise RuntimeError("no filename Edit in file dialog")

    ctypes.windll.user32.SendMessageW(filename_edit.handle, WM_SETTEXT, 0, str(image_path))
    time.sleep(0.1)

    # Click Open/OK
    open_btn = None
    for c in file_dlg.children():
        try:
            t = c.window_text()
            if c.class_name() == "Button" and t in ("&Open", "Open", "打开", "&OK", "OK", "确定"):
                open_btn = c
                break
        except Exception:  # noqa: S110
            pass
    if not open_btn:
        for c in file_dlg.children():
            try:
                if (
                    c.class_name() == "Button"
                    and c.window_text()
                    and "cancel" not in c.window_text().lower()
                    and "取消" not in c.window_text()
                ):
                    open_btn = c
                    break
            except Exception:  # noqa: S110
                pass
    if not open_btn:
        raise RuntimeError("Open button not found in file dialog")

    ctypes.windll.user32.SendMessageW(open_btn.handle, BM_CLICK, 0, 0)


def wait_for_flash_op(dlg, success_keywords, fail_keywords, timeout=600, label=""):
    """Poll dialog status until success or failure keyword found. Returns True on success."""
    deadline = time.monotonic() + timeout
    last_status = ""
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            for c in dlg.children():
                try:
                    t = c.window_text().strip()
                    if not t or t == last_status:
                        continue
                    tl = t.lower()
                    if any(kw in tl for kw in success_keywords + fail_keywords + ["%"]):
                        print(f"  [{label}] {t!r}")
                        last_status = t
                        if any(kw in tl for kw in success_keywords):
                            return True
                        if any(kw in tl for kw in fail_keywords):
                            return False
                except Exception:  # noqa: S110
                    pass
        except Exception:  # noqa: S110
            pass
    raise TimeoutError(f"{label} timed out after {timeout}s")


# ══════════════════════════════════════════════════════════════════════════════
# Phase 1 — DTR reset + Launch PhoenixMC, open debug dialog
# ══════════════════════════════════════════════════════════════════════════════


def _dtr_reset():
    """Toggle DTR on COM6 to attempt a hardware reset of the XR872AT.

    If the PCB routes CH340 DTR to NRST this will hard-reset the device into the
    BROM window so PhoenixMC can connect without any button press.  If DTR is not
    wired to RESET the toggle is harmless.

    Must be called BEFORE PhoenixMC opens the port.
    """
    if not HAS_SERIAL:
        return
    try:
        ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=0)  # type: ignore[possibly-unbound]
        ser.dtr = False  # assert reset (active-low on most boards)
        time.sleep(0.15)
        ser.dtr = True  # release reset → device boots into BROM
        time.sleep(0.05)
        ser.close()
        print("DTR reset pulse sent")
    except Exception as e:
        print(f"DTR reset skipped ({e})")


def phase_launch():
    print("=" * 60)
    print("PHASE 1 — Launch PhoenixMC")
    print("=" * 60)

    # Kill stale instances
    for w in wins():
        try:
            if "PhoenixMC" in w.window_text() and w.class_name() not in ("Chrome_WidgetWin_1",):
                w.close()
                time.sleep(0.2)
        except Exception:  # noqa: S110
            pass
    time.sleep(0.5)

    # Attempt hardware reset via DTR before PhoenixMC grabs the port.
    # If our custom firmware is already on the device the bootloader will have
    # called bl_upgrade() and the chip is already waiting in BROM mode — DTR
    # is a no-op in that case but harmless.
    _dtr_reset()

    subprocess.Popen([str(PHOENIXMC_EXE)], cwd=str(PHOENIXMC_DIR))
    print("Launched PhoenixMC")

    # Wait for main window with 调试 button
    main_w = None
    debug_btn = None
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        for w in wins():
            try:
                if "PhoenixMC version" in w.window_text() and len(w.children()) > 30:
                    btn = next((c for c in w.children() if c.window_text() == "调试"), None)
                    if btn:
                        main_w, debug_btn = w, btn
                        break
            except Exception:  # noqa: S110
                pass
        if main_w:
            break
        time.sleep(0.2)

    if not main_w or not debug_btn:
        raise RuntimeError("PhoenixMC main window or 调试 button not found")
    print(f"Main window: {main_w.window_text()!r}")

    # Check COM6 listview
    for ctrl in main_w.children():
        try:
            if ctrl.class_name() == "SysListView32" and ctrl.rectangle().left < 950:
                n = ctypes.windll.user32.SendMessageW(ctrl.handle, LVM_GETITEMCOUNT, 0, 0)
                if n > 0:
                    listview_check_item(ctrl, 0)
                    time.sleep(0.2)
                    print(f"COM port checked ({n} item(s) in list)")
        except Exception as e:
            print(f"Listview: {e}")

    # Click 调试 — PhoenixMC does synchronous serial I/O in the button handler,
    # so SendMessageW can block for minutes while it syncs with BROM. Use
    # PostMessageW (async) so we return immediately and poll for the dialog.
    before = win_handles()
    ctypes.windll.user32.PostMessageW(debug_btn.handle, BM_CLICK, 0, 0)

    # Wait up to 8 min for the flash dialog to appear (PhoenixMC may spend
    # several minutes negotiating with the BROM before "Open comm OK!" shows).
    dlg = None
    port_err_dismissed = False
    deadline_dlg = time.monotonic() + 480
    while time.monotonic() < deadline_dlg:
        time.sleep(0.5)

        # Dismiss a port-error popup if it appeared
        if not port_err_dismissed:
            for w in wins():
                try:
                    if w.handle not in before and len(w.children()) <= 5:
                        ok = next((c for c in w.children() if c.window_text() == "OK"), None)
                        if ok:
                            print("Dismissed port error, retrying 调试...")
                            ctypes.windll.user32.PostMessageW(ok.handle, BM_CLICK, 0, 0)
                            time.sleep(0.3)
                            db = next(
                                (c for c in main_w.children() if c.window_text() == "调试"), None
                            )
                            if db:
                                ctypes.windll.user32.PostMessageW(db.handle, BM_CLICK, 0, 0)
                            port_err_dismissed = True
                except Exception:  # noqa: S110
                    pass

        dlg = get_flash_dialog()
        if dlg:
            break

    if not dlg:
        raise RuntimeError("Debug dialog did not open within 8 min")
    print(f"Debug dialog: {dlg.window_text()!r}")
    return dlg


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 — Wait for BROM connection (no user interaction)
# ══════════════════════════════════════════════════════════════════════════════


def phase_wait_brom(dlg):
    print()
    print("=" * 60)
    print("PHASE 2 — Waiting for BROM connection")
    print("=" * 60)
    print("(If device has our firmware it is already in BROM upgrade mode.)")
    print("Connecting...")

    # Wait up to 5 minutes.  If the device is already in bl_upgrade() mode it
    # connects in <1 s.  If DTR reset worked it connects in <3 s.  The long
    # timeout handles edge cases (device was sleeping, USB re-enumeration, etc.)
    deadline = time.monotonic() + 300
    last_texts = set()
    while time.monotonic() < deadline:
        time.sleep(0.3)
        try:
            for c in dlg.children():
                try:
                    t = c.window_text().strip()
                    if not t or t in last_texts:
                        continue
                    last_texts.add(t)
                    tl = t.lower()
                    if "open comm ok" in tl or "comm ok" in tl:
                        print(f"  {t}")
                        print("Connected!")
                        return
                    # Print interesting status changes without spamming
                    if any(
                        kw in tl for kw in ("synchron", "open", "comm", "ok", "connect", "error")
                    ):
                        print(f"  {t}")
                except Exception:  # noqa: S110
                    pass
        except Exception:  # noqa: S110
            pass

    raise TimeoutError(
        "BROM connection timed out after 5 min.\n"
        "  - If device is running OEM firmware: long-press power to enter BROM mode,\n"
        "    then re-run the script.\n"
        "  - If DTR reset is not wired: power-cycle the device manually while\n"
        "    PhoenixMC is open."
    )


# ══════════════════════════════════════════════════════════════════════════════
# Phase 3 — Read back 4 MB flash
# ══════════════════════════════════════════════════════════════════════════════


def phase_readback(dlg):
    print()
    print("=" * 60)
    print("PHASE 3 — Read back flash (4 MB)")
    print("=" * 60)

    # Remove stale readback file
    if READBACK_FILE.exists():
        READBACK_FILE.unlink()
        print(f"Removed stale {READBACK_FILE.name}")

    hex_edits = get_hex_edit_fields(dlg)
    print(f"Hex edit fields: {len(hex_edits)} found")
    if len(hex_edits) < 4:
        raise RuntimeError(f"Expected ≥4 hex edit fields, got {len(hex_edits)}")

    hex_edits[2][1].set_edit_text("00400000")
    hex_edits[3][1].set_edit_text("00000000")
    print(f"FLASH length -> {hex_edits[2][1].window_text()!r}")
    print(f"FLASH addr   -> {hex_edits[3][1].window_text()!r}")

    # Find and click FLASH 读取 (second 读取 by Y)
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

    # Monitor output file growth
    print("Reading flash (4 MB at 115200 baud ≈ 5 min)...")
    deadline = time.monotonic() + 420
    last_sz = -1
    last_pct = -1
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            sz = READBACK_FILE.stat().st_size
            pct = sz * 100 // 0x400000
            if sz != last_sz:
                if pct != last_pct and pct % 10 == 0:
                    print(f"  {pct}%  ({sz:,} bytes)")
                    last_pct = pct
                last_sz = sz
            if sz >= 0x400000:
                print(f"  100%  ({sz:,} bytes)")
                print(f"Readback complete: {READBACK_FILE}")
                return
        except FileNotFoundError:
            pass

    raise TimeoutError("Flash readback timed out")


# ══════════════════════════════════════════════════════════════════════════════
# Phase 4 — Compare readback against our image
# ══════════════════════════════════════════════════════════════════════════════


def phase_compare():
    print()
    print("=" * 60)
    print("PHASE 4 — Compare readback vs xr_system.img")
    print("=" * 60)

    img = OUR_IMAGE.read_bytes()
    dump = READBACK_FILE.read_bytes()
    n = len(img)

    print(f"Our image:  {n:,} bytes  ({OUR_IMAGE.name})")
    print(f"Flash dump: {len(dump):,} bytes  (comparing first {n:,} bytes)")

    region = dump[:n]
    if region == img:
        print("MATCH — flash contents identical to xr_system.img")
        return True

    diffs = [i for i in range(n) if region[i] != img[i]]
    print(f"MISMATCH — {len(diffs)} differing byte(s)")
    print("First differences (offset | flash | image):")
    for off in diffs[:20]:
        print(f"  0x{off:08X}  flash=0x{region[off]:02X}  img=0x{img[off]:02X}")
    if len(diffs) > 20:
        print(f"  ... and {len(diffs) - 20} more")
    return False


# ══════════════════════════════════════════════════════════════════════════════
# Phase 5 — Flash OEM dump
# ══════════════════════════════════════════════════════════════════════════════


def _erase_flash(dlg, hex_edits, size_bytes, label=""):
    """Erase flash from address 0 covering size_bytes (rounded to 64K boundary).

    NOR flash requires erase before write when any bit must go 0→1.  Writing
    without erase produces bitwise-AND corruption: bits that were 0 from a prior
    image stay 0 even if the new image wants them as 1.
    """
    erase_len = (size_bytes + 0xFFFF) & ~0xFFFF
    print(
        f"Erasing 0x{erase_len:X} bytes ({erase_len:,}) before write{' [' + label + ']' if label else ''}..."
    )
    hex_edits[2][1].set_edit_text(f"{erase_len:08X}")
    hex_edits[3][1].set_edit_text("00000000")

    erase_btns = sorted(
        [
            (c.rectangle().top, c)
            for c in dlg.children()
            if c.class_name() == "Button"
            and "擦除" in c.window_text()
            and "模式" not in c.window_text()
        ],
        key=lambda x: x[0],
    )
    if not erase_btns:
        print("WARNING: no 擦除 button found — skipping erase (write may corrupt)")
        return

    ctypes.windll.user32.SendMessageW(erase_btns[0][1].handle, BM_CLICK, 0, 0)

    deadline = time.monotonic() + 180
    last_status = ""
    while time.monotonic() < deadline:
        time.sleep(1)
        for c in dlg.children():
            try:
                t = c.window_text().strip()
                if not t or t == last_status:
                    continue
                tl = t.lower()
                if any(kw in tl for kw in ("erase", "擦", "%", "ok", "error", "fail")):
                    print(f"  Erase: {t!r}")
                    last_status = t
                    if "ok" in tl or "成功" in tl or "完成" in tl:
                        return
                    if "error" in tl or "fail" in tl or "失败" in tl:
                        raise RuntimeError("Erase failed")
            except Exception:  # noqa: S110
                pass
    print("WARNING: erase timed out — proceeding")


def _flash_image(dlg, image_path, label, erase=True):
    """Set length/addr, optionally erase, click 写入, handle file dialog, wait for OK."""
    img_size = image_path.stat().st_size
    hex_edits = get_hex_edit_fields(dlg)
    if len(hex_edits) < 4:
        raise RuntimeError(f"Expected ≥4 hex edit fields, got {len(hex_edits)}")

    hex_edits[3][1].set_edit_text("00000000")

    if erase:
        _erase_flash(dlg, hex_edits, img_size, label)

    hex_edits[2][1].set_edit_text(f"{img_size:08X}")
    hex_edits[3][1].set_edit_text("00000000")
    print(f"FLASH length -> {hex_edits[2][1].window_text()!r}  ({img_size:,} bytes)")

    write_btns = sorted(
        [
            (c.rectangle().top, c)
            for c in dlg.children()
            if c.class_name() == "Button" and "写入" in c.window_text()
        ],
        key=lambda x: x[0],
    )
    if not write_btns:
        raise RuntimeError("No 写入 button found")
    write_btn = write_btns[1][1] if len(write_btns) > 1 else write_btns[0][1]
    print(f"Clicking FLASH 写入 (y={write_btn.rectangle().top})...")

    before = win_handles()
    ctypes.windll.user32.SendMessageW(write_btn.handle, BM_CLICK, 0, 0)
    inject_file_dialog(before, image_path)
    print("File path injected, writing...")

    ok = wait_for_flash_op(
        dlg,
        success_keywords=["write ok", "success", "complete", "finish", "成功", "完成"],
        fail_keywords=["error", "fail", "失败"],
        timeout=600,
        label=label,
    )
    if not ok:
        raise RuntimeError(f"{label} flash failed")
    print(f"{label}: Write OK!")


def phase_flash_oem(dlg):
    print()
    print("=" * 60)
    print("PHASE 5 — Flash OEM firmware (4 MB, no erase needed)")
    print("=" * 60)

    if not OEM_DUMP.exists():
        raise FileNotFoundError(f"OEM dump not found: {OEM_DUMP}")
    print(f"OEM dump: {OEM_DUMP}  ({OEM_DUMP.stat().st_size:,} bytes)")
    # OEM flash: our xr_system.img has 0xFF in the OEM priv fields, so writing
    # OEM values (non-FF) over our all-FF bits is valid (1→0 transitions only).
    # No erase needed for this direction.
    _flash_image(dlg, OEM_DUMP, "OEM", erase=False)


# ══════════════════════════════════════════════════════════════════════════════
# Phase 7 — Reflash custom firmware (with erase)
# ══════════════════════════════════════════════════════════════════════════════


def phase_flash_custom(dlg):
    print()
    print("=" * 60)
    print("PHASE 7 — Flash custom firmware (with erase)")
    print("=" * 60)

    if not OUR_IMAGE.exists():
        raise FileNotFoundError(f"Image not found: {OUR_IMAGE}")
    print(f"Image: {OUR_IMAGE}  ({OUR_IMAGE.stat().st_size:,} bytes)")
    # Must erase first: OEM has non-FF bits in priv fields; our image wants FF
    # there.  Without erase the bits stay 0, corrupting the header checksum.
    _flash_image(dlg, OUR_IMAGE, "custom", erase=True)


# ══════════════════════════════════════════════════════════════════════════════
# Phase 6 — Reboot and capture UART
# ══════════════════════════════════════════════════════════════════════════════


def phase_reboot_and_uart(dlg):
    print()
    print("=" * 60)
    print("PHASE 6 — Reboot and capture UART output")
    print("=" * 60)

    if not HAS_SERIAL:
        print("pyserial not available — skipping UART capture")
        return

    # Strategy: click reboot, then IMMEDIATELY kill PhoenixMC (process kill is
    # faster than w.close()) and open COM6.  The XR872AT boot log starts ~100 ms
    # after reset; a process kill + serial open takes ~20–30 ms, so we get there
    # in time.  USB stays enumerated because the CH340 is powered from USB 5V
    # independently of the XR872AT power rail — so COM6 never disappears.
    reboot_btn = next(
        (
            c
            for c in dlg.children()
            if c.class_name() == "Button" and "reboot" in c.window_text().lower()
        ),
        None,
    )

    # Find PhoenixMC PIDs to kill
    phoenixmc_pids = []
    try:
        import subprocess as _sp  # noqa: PLC0415

        out = _sp.check_output(
            [  # noqa: S607
                "powershell",
                "-Command",
                "Get-Process | Where-Object { $_.Name -match 'phoenixMC' } | Select-Object -ExpandProperty Id",
            ],
            text=True,
        )
        phoenixmc_pids = [int(p) for p in out.split() if p.strip().isdigit()]
    except Exception as e:
        print(f"PID lookup failed: {e}")

    if reboot_btn:
        print(
            f"Clicking reboot ({reboot_btn.window_text()!r}) then killing PhoenixMC immediately..."
        )
        ctypes.windll.user32.SendMessageW(reboot_btn.handle, BM_CLICK, 0, 0)
    else:
        print("No reboot button — killing PhoenixMC and using DTR reset...")

    # Kill PhoenixMC immediately to release COM6 (no sleep before this)
    kernel32 = ctypes.windll.kernel32
    for pid in phoenixmc_pids:
        try:
            h = kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
            if h:
                kernel32.TerminateProcess(h, 0)
                kernel32.CloseHandle(h)
        except Exception:  # noqa: S110
            pass

    if not reboot_btn:
        _dtr_reset()

    # Open COM6 immediately — no sleep
    ser = None
    deadline = time.monotonic() + 2  # wait up to 2s for port to free
    while time.monotonic() < deadline:
        try:
            ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=0.1)  # type: ignore[possibly-unbound]
            break
        except serial.SerialException:  # type: ignore[possibly-unbound]
            time.sleep(0.01)

    if ser is None:
        print(f"Could not open {COM_PORT} — UART capture failed")
        return

    print("COM6 open — capturing 30 s of boot output...")
    print("-" * 60)
    deadline = time.monotonic() + 30
    total_bytes = 0
    try:
        while time.monotonic() < deadline:
            chunk = ser.read(256)
            if chunk:
                total_bytes += len(chunk)
                sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                sys.stdout.flush()
    except Exception as e:
        print(f"\nRead error: {e}")
    finally:
        ser.close()

    print()
    print("-" * 60)
    if total_bytes == 0:
        print("UART: NO OUTPUT — device is silent (expected if still in BROM mode)")
        print("  Possible causes:")
        print("  - Reboot went back into BROM mode (try power-cycling manually)")
        print("  - OEM firmware also silent (hardware/wiring issue)")
        print("  - Wrong baud rate or wrong pin mux")
    else:
        print(f"UART: received {total_bytes} bytes — device is alive!")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════


def already_connected_dialog():
    """Return the flash dialog if PhoenixMC is already open and shows 'Open comm OK!'."""
    for w in wins():
        try:
            if w.window_text() in ("flash operation", "phoenixMC") and len(w.children()) > 5:
                for c in w.children():
                    try:
                        if "open comm ok" in c.window_text().lower():
                            return w
                    except Exception:  # noqa: S110
                        pass
        except Exception:  # noqa: S110
            pass
    return None


def main():
    for path, _name in [
        (OUR_IMAGE, "xr_system.img"),
        (OEM_DUMP, "OEM reference dump"),
        (PHOENIXMC_EXE, "flash tool"),
    ]:
        if not path.exists():
            print(f"ERROR: required file not found: {path}")
            sys.exit(1)

    dlg = already_connected_dialog()
    if dlg:
        print("PhoenixMC already connected — skipping launch and BROM phases.")
        print(f"Dialog: {dlg.window_text()!r}")
    else:
        dlg = phase_launch()
        phase_wait_brom(dlg)

    phase_readback(dlg)
    match = phase_compare()
    if not match:
        print()
        print("NOTE: flash mismatch — proceeding with OEM flash anyway")
    phase_flash_oem(dlg)
    phase_reboot_and_uart(dlg)  # boot OEM, capture UART to verify hardware

    # Re-enter BROM mode and reflash custom firmware (with erase this time)
    print()
    print("Re-entering BROM mode to reflash custom firmware...")
    dlg2 = phase_launch()
    phase_wait_brom(dlg2)
    phase_flash_custom(dlg2)
    phase_reboot_and_uart(dlg2)  # boot custom firmware, capture UART

    print()
    print("=" * 60)
    print("Done.")
    print("=" * 60)


if __name__ == "__main__":
    main()
