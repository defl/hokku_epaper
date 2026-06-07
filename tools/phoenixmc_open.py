"""Step 1: Launch PhoenixMC, select COM6, open the debug dialog. No mouse used.

Usage:
    python tools/phoenixmc_open.py
"""

import ctypes
import io
import pathlib
import subprocess
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from pywinauto import Desktop

PHOENIXMC_DIR = (
    pathlib.Path(__file__).parents[1] / ".private/screens/bigme_f7/tools/phoenixmc_v3.1.240901a"
)

BM_CLICK = 0x00F5
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


# PhoenixMC.exe is a 32-bit process. Passing ctypes.byref() from 64-bit Python
# truncates the pointer via WOW64 and crashes the target. Instead, allocate the
# LVITEM inside the target process with VirtualAllocEx/WriteProcessMemory.
class LVITEM32(ctypes.Structure):
    _fields_ = [
        ("mask", ctypes.c_uint32),
        ("iItem", ctypes.c_int32),
        ("iSubItem", ctypes.c_int32),
        ("state", ctypes.c_uint32),
        ("stateMask", ctypes.c_uint32),
        ("pszText", ctypes.c_uint32),  # 32-bit pointer, unused
        ("cchTextMax", ctypes.c_int32),
        ("iImage", ctypes.c_int32),
        ("lParam", ctypes.c_int32),
    ]


def wins():
    return [
        w for w in Desktop(backend="win32").windows() if w.class_name() not in ("tooltips_class32",)
    ]


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


def main():
    # Close any stale instances
    for w in wins():
        try:
            if "PhoenixMC" in w.window_text() and w.class_name() not in ("Chrome_WidgetWin_1",):
                w.close()
                time.sleep(0.2)
        except:
            pass
    time.sleep(0.5)

    subprocess.Popen([str(PHOENIXMC_DIR / "phoenixMC.exe")], cwd=str(PHOENIXMC_DIR))
    print("Launched PhoenixMC...")

    # Wait until the main window AND the 调试 button are both present
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
            except:
                pass
        if main_w:
            break
        time.sleep(0.2)

    if not main_w:
        print("ERROR: PhoenixMC main window not found")
        sys.exit(1)
    print(f"Main window: {main_w.window_text()!r}")

    # Select COM6 in listview via remote memory (safe for 32-bit target from 64-bit Python)
    for ctrl in main_w.children():
        try:
            if ctrl.class_name() == "SysListView32" and ctrl.rectangle().left < 950:
                n = ctypes.windll.user32.SendMessageW(ctrl.handle, LVM_GETITEMCOUNT, 0, 0)
                print(f"Listview has {n} items")
                if n > 0:
                    listview_check_item(ctrl, 0)
                    time.sleep(0.2)
                    print("COM port checked (no mouse)")
        except Exception as e:
            print(f"Listview error: {e}")

    # Click 调试 via SendMessage — bypasses pywinauto visibility check
    before = {w.handle for w in wins()}
    debug_btn = next((c for c in main_w.children() if c.window_text() == "调试"), None)
    if not debug_btn:
        print("ERROR: no 调试 button")
        sys.exit(1)
    ctypes.windll.user32.SendMessageW(debug_btn.handle, BM_CLICK, 0, 0)
    time.sleep(1.0)

    # Dismiss port error if it appeared, then retry
    for w in wins():
        try:
            if w.handle not in before and len(w.children()) <= 5:
                ok = next((c for c in w.children() if c.window_text() == "OK"), None)
                if ok:
                    print("Dismissed port error, retrying...")
                    ctypes.windll.user32.SendMessageW(ok.handle, BM_CLICK, 0, 0)
                    time.sleep(0.3)
                    db = next((c for c in main_w.children() if c.window_text() == "调试"), None)
                    if db:
                        ctypes.windll.user32.SendMessageW(db.handle, BM_CLICK, 0, 0)
                    time.sleep(1.0)
        except:
            pass

    # Find the flash operation dialog
    dlg = next(
        (
            w
            for w in wins()
            if w.handle not in before and len(w.children()) > 5 and w.handle != main_w.handle
        ),
        None,
    )
    if dlg:
        print(f"Debug dialog open: {dlg.window_text()!r}")
        print()
        print("Long-press the F7 power button to enter BROM mode.")
        print("When 'Open comm OK!' appears, run:  python tools/phoenixmc_read.py")
    else:
        print("ERROR: debug dialog did not open")
        sys.exit(1)


if __name__ == "__main__":
    main()
