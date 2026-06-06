"""Inspect PhoenixMC: launch, select COM6, click 调试, dump flash dialog.

Usage:
    python tools/phoenixmc_inspect.py
"""

import io
import pathlib
import subprocess
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

try:
    from pywinauto import Application, Desktop
except ImportError:
    print("pywinauto not found. Run: pip install pywinauto", file=sys.stderr)
    sys.exit(1)

PHOENIXMC_EXE = str(
    pathlib.Path(__file__).parents[1]
    / ".private/screens/bigme_f7/tools/phoenixmc_v3.1.240901a/phoenixMC.exe"
)
TARGET_PORT = "COM6"


def dump_controls(win, label=""):
    print(f"\n{'=' * 70}")
    print(f"Window: {win.window_text()!r}  class={win.class_name()}  [{label}]")
    print(f"{'=' * 70}")
    print(f"{'#':<4} {'Class':<22} {'Text':<35} {'Rect'}")
    print("-" * 100)
    for i, ctrl in enumerate(win.children()):
        try:
            txt = repr(ctrl.window_text())
        except Exception:
            txt = "?"
        try:
            cls = ctrl.class_name()
        except Exception:
            cls = "?"
        try:
            r = ctrl.rectangle()
            rect_str = f"({r.left},{r.top})-({r.right},{r.bottom})"
        except Exception:
            rect_str = "?"
        print(f"{i:<4} {cls:<22} {txt:<35} {rect_str}")


def find_window_partial(title_fragment, timeout=15, exclude_classes=None):
    exclude_classes = exclude_classes or set()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for w in Desktop(backend="win32").windows():
            try:
                if w.class_name() in exclude_classes:
                    continue
                if title_fragment.lower() in w.window_text().lower():
                    return w
            except Exception:
                pass
        time.sleep(0.2)
    return None


def find_or_launch_phoenixmc():
    win = find_window_partial("phoenixmc", timeout=2, exclude_classes={"tooltips_class32"})
    if win:
        print(f"Found existing: {win.window_text()!r}")
        return win
    print(f"Launching: {PHOENIXMC_EXE}")
    subprocess.Popen([PHOENIXMC_EXE], cwd=str(pathlib.Path(PHOENIXMC_EXE).parent))
    win = find_window_partial("phoenixmc", timeout=15, exclude_classes={"tooltips_class32"})
    if win is None:
        print("ERROR: PhoenixMC did not open", file=sys.stderr)
        sys.exit(1)
    time.sleep(1.5)
    return win


def snapshot_handles(exclude_classes=None):
    exclude_classes = exclude_classes or set()
    handles = set()
    for w in Desktop(backend="win32").windows():
        try:
            if w.class_name() not in exclude_classes:
                handles.add(w.handle)
        except Exception:
            pass
    return handles


def select_com_port(main_win, port=TARGET_PORT):
    """Find the COM port listview and check the target port."""
    port_list = None
    for ctrl in main_win.children():
        try:
            if ctrl.class_name() == "SysListView32":
                r = ctrl.rectangle()
                # The COM port list is on the left (x < 950)
                if r.left < 950:
                    port_list = ctrl
                    break
        except Exception:
            pass

    if port_list is None:
        print("ERROR: COM port listview not found")
        return False

    # Dump listview items
    print("\nCOM port listview contents:")
    try:
        item_count = port_list.item_count()
        print(f"  {item_count} items")
        for i in range(item_count):
            item = port_list.get_item(i)
            print(f"  [{i}] text={item.text()!r}  checked={item.checked()}")
    except Exception as e:
        print(f"  Could not read items: {e}")
        # Fallback: try clicking on the listview area where COM6 should be
        item_count = None

    # Click the checkbox for the target port
    try:
        count = port_list.item_count()
        print(f"  {count} item(s) in list")
        for i in range(count):
            item = port_list.get_item(i)
            txt = item.text()
            print(f"  Item {i}: {txt!r}")
            if port in txt:
                # Click at the checkbox area: leftmost pixel of the item row
                item_rect = item.rectangle()
                # Checkbox is in the first ~18px of the row
                cx = item_rect.left + 9
                cy = (item_rect.top + item_rect.bottom) // 2
                print(f"  Clicking checkbox at ({cx}, {cy})...")
                import pywinauto.mouse as mouse

                mouse.click(button="left", coords=(cx, cy))
                time.sleep(0.3)
                return True
        print(f"  {port} not found in listview — device plugged in?")
        return False
    except Exception as e:
        print(f"  Could not click checkbox: {e}")
        return False


def main():
    main_win = find_or_launch_phoenixmc()
    time.sleep(0.5)
    dump_controls(main_win, "main window")

    # Select COM6
    select_com_port(main_win)

    # Find 调试 button
    debug_btn = None
    for ctrl in main_win.children():
        try:
            if "调试" in ctrl.window_text():
                debug_btn = ctrl
                break
        except Exception:
            pass

    if debug_btn is None:
        print("\nERROR: 调试 button not found")
        sys.exit(1)

    print("\nClicking 调试...")
    before = snapshot_handles(exclude_classes={"tooltips_class32"})
    debug_btn.click_input()
    time.sleep(0.5)

    # Dismiss "select COM port" dialog if it appears
    for _ in range(5):
        for w in Desktop(backend="win32").windows():
            try:
                if w.handle not in before and w.class_name() == "#32770":
                    txt = w.window_text()
                    print(f"  Dialog: {txt!r}")
                    # Look for OK button to dismiss
                    for c in w.children():
                        if c.window_text() == "OK":
                            print("  Dismissing OK dialog...")
                            c.click_input()
                            time.sleep(0.3)
                            break
            except Exception:
                pass
        time.sleep(0.3)

    # Re-click 调试 after dismissing
    time.sleep(0.2)
    before = snapshot_handles(exclude_classes={"tooltips_class32"})
    debug_btn.click_input()

    # Wait for flash operation dialog
    print("Waiting for flash operation dialog (up to 15s)...")
    deadline = time.monotonic() + 15
    new_win = None
    while time.monotonic() < deadline:
        for w in Desktop(backend="win32").windows():
            try:
                if w.handle not in before and w.class_name() not in {"tooltips_class32"}:
                    t = w.window_text()
                    print(f"  New window: {t!r}  class={w.class_name()}")
                    new_win = w
            except Exception:
                pass
        if new_win:
            break
        time.sleep(0.2)

    if new_win is None:
        print("No new window found.")
        sys.exit(1)

    time.sleep(0.5)
    dump_controls(new_win, "flash operation dialog")
    print("\nDone.")


if __name__ == "__main__":
    main()
