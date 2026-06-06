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

LVM_SETITEMSTATE = 0x102B
LVM_GETITEMCOUNT = 0x1004
LVIS_SELECTED = 0x0002
LVIS_FOCUSED = 0x0001
LVIS_STATEIMAGEMASK = 0xF000


class LVITEM(ctypes.Structure):
    _fields_ = [
        ("mask", ctypes.c_uint),
        ("iItem", ctypes.c_int),
        ("iSubItem", ctypes.c_int),
        ("state", ctypes.c_uint),
        ("stateMask", ctypes.c_uint),
        ("pszText", ctypes.c_void_p),
        ("cchTextMax", ctypes.c_int),
        ("iImage", ctypes.c_int),
        ("lParam", ctypes.c_long),
    ]


def wins():
    return [
        w for w in Desktop(backend="win32").windows() if w.class_name() not in ("tooltips_class32",)
    ]


def listview_check_item(lv_ctrl, index=0):
    """Check item at index in a SysListView32 checkbox listview (no mouse)."""
    hwnd = lv_ctrl.handle

    # First: select and focus the item via LVM_SETITEMSTATE
    item = LVITEM()
    item.mask = 0x0008  # LVIF_STATE
    item.iItem = index
    item.iSubItem = 0
    item.state = LVIS_SELECTED | LVIS_FOCUSED
    item.stateMask = LVIS_SELECTED | LVIS_FOCUSED
    ctypes.windll.user32.SendMessageW(hwnd, LVM_SETITEMSTATE, index, ctypes.byref(item))

    # Then: set checkbox state to checked (state image index 2)
    item2 = LVITEM()
    item2.mask = 0x0008  # LVIF_STATE
    item2.iItem = index
    item2.iSubItem = 0
    item2.state = 0x2000  # checked (state image 2)
    item2.stateMask = LVIS_STATEIMAGEMASK
    ctypes.windll.user32.SendMessageW(hwnd, LVM_SETITEMSTATE, index, ctypes.byref(item2))


def find_main_win(timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for w in wins():
            try:
                if "PhoenixMC version" in w.window_text() and len(w.children()) > 30:
                    return w
            except:
                pass
        time.sleep(0.2)
    return None


def dismiss_error_dialogs():
    for w in wins():
        try:
            for c in w.children():
                if c.window_text() == "OK" and c.class_name() == "Button":
                    c.click()
                    return True
        except:
            pass
    return False


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
    main_w = find_main_win(timeout=12)
    if not main_w:
        print("ERROR: PhoenixMC main window not found")
        sys.exit(1)
    time.sleep(0.8)
    print(f"Main window: {main_w.window_text()!r}")

    # Select COM6 in listview via Windows messages (no mouse)
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

    # Click 调试 via BM_CLICK (no mouse)
    before = {w.handle for w in wins()}
    debug_btn = next((c for c in main_w.children() if c.window_text() == "调试"), None)
    if not debug_btn:
        print("ERROR: no 调试 button")
        sys.exit(1)
    debug_btn.click()
    time.sleep(1.0)

    # Dismiss port error if it appeared
    if dismiss_error_dialogs():
        print("Dismissed port error, retrying...")
        time.sleep(0.3)
        debug_btn.click()
        time.sleep(1.0)

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
