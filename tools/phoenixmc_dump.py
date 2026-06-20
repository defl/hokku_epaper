"""Automate PhoenixMC GUI to dump the Bigme F7 flash.

Run this once. It will:
  1. Open PhoenixMC (or reuse existing)
  2. Select COM6 and open the Debug dialog
  3. Print READY — at that point press and hold the F7 power button
  4. The instant 'Open comm OK!' appears, click FLASH 读取
  5. Wait for the 2MB dump to complete (~30s)

Usage:
    python tools/phoenixmc_dump.py
"""

import io
import pathlib
import subprocess
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

try:
    import pywinauto.mouse as mouse
    from pywinauto import Desktop
except ImportError:
    print("Run: pip install pywinauto")
    sys.exit(1)

from _private import res

PHOENIXMC_DIR = res("bigme_flash_tool_dir")
PHOENIXMC_EXE = str(res("bigme_flash_tool_exe"))
BROM_WAIT = 120  # seconds to wait for device after READY


def all_wins():
    result = []
    for w in Desktop(backend="win32").windows():
        try:
            if w.class_name() not in ("tooltips_class32",):
                result.append(w)
        except Exception:
            pass
    return result


def find_win(fragment, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for w in all_wins():
            try:
                if fragment.lower() in w.window_text().lower():
                    return w
            except Exception:
                pass
        time.sleep(0.15)
    return None


def win_handles():
    return {w.handle for w in all_wins()}


# --- Step 1: get main window ---
def get_main_win():
    win = find_win("phoenixmc", timeout=2)
    if win:
        print(f"Found PhoenixMC: {win.window_text()!r}")
        return win
    print("Launching PhoenixMC...")
    subprocess.Popen([PHOENIXMC_EXE], cwd=str(PHOENIXMC_DIR))
    win = find_win("phoenixmc", timeout=12)
    if not win:
        print("ERROR: PhoenixMC did not open")
        sys.exit(1)
    time.sleep(0.8)
    return win


# --- Step 2: select COM6 ---
def select_com(main_win):
    for ctrl in main_win.children():
        try:
            if ctrl.class_name() == "SysListView32":
                r = ctrl.rectangle()
                if r.left < 950 and ctrl.item_count() > 0:
                    # Find header bottom
                    hdr_bottom = r.top + 24
                    for ch in ctrl.children():
                        try:
                            if ch.class_name() == "SysHeader32":
                                hdr_bottom = ch.rectangle().bottom
                        except Exception:
                            pass
                    cy = hdr_bottom + 9
                    # Click item row center, then checkbox
                    mouse.click(button="left", coords=((r.left + r.right) // 2, cy))
                    time.sleep(0.1)
                    mouse.click(button="left", coords=(r.left + 9, cy))
                    time.sleep(0.15)
                    print(f"COM port selected (row y={cy})")
                    return True
        except Exception:
            pass
    print("WARNING: COM port listview not found")
    return False


# --- Step 3: open debug dialog ---
def find_flash_dialog(exclude_handles=None):
    """Return the flash operation dialog if it's open."""
    exclude_handles = exclude_handles or set()
    for w in all_wins():
        if w.handle in exclude_handles:
            continue
        try:
            title = w.window_text()
            # Flash dialog is titled "flash operation" or "phoenixMC" (both seen)
            if title.lower() in ("flash operation", "phoenixmc") and len(w.children()) > 5:
                return w
        except Exception:
            pass
    return None


def click_debug_btn(main_win):
    for ctrl in main_win.children():
        try:
            if "调试" in ctrl.window_text():
                ctrl.click_input()
                return True
        except Exception:
            pass
    return False


def open_debug(main_win):
    # Reuse if already open
    existing = find_flash_dialog(exclude_handles={main_win.handle})
    if existing:
        print(f"Flash dialog already open, reusing: {existing.window_text()!r}")
        return existing

    before = win_handles()
    click_debug_btn(main_win)

    # Wait up to 3s for either: flash dialog (>5 controls) or error dialog (OK button)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        for w in all_wins():
            if w.handle in before:
                continue
            try:
                children = w.children()
                # Real flash dialog has many controls
                if len(children) > 5:
                    return w
                # Error dialog has OK button — dismiss and try again
                for c in children:
                    if c.window_text() == "OK":
                        print("Dismissing 'select COM port' error...")
                        c.click_input()
                        time.sleep(0.15)
                        before2 = win_handles()
                        click_debug_btn(main_win)
                        # Wait for flash dialog
                        deadline2 = time.monotonic() + 5
                        while time.monotonic() < deadline2:
                            for w2 in all_wins():
                                if w2.handle in before2:
                                    continue
                                try:
                                    if len(w2.children()) > 5:
                                        return w2
                                except Exception:
                                    pass
                            time.sleep(0.1)
                        return None
            except Exception:
                pass
        time.sleep(0.1)
    return None


# --- Step 4: find FLASH 读取 button ---
def find_read_btn(dlg):
    btns = []
    for ctrl in dlg.children():
        try:
            if ctrl.class_name() == "Button" and "读取" in ctrl.window_text():
                btns.append((ctrl.rectangle().top, ctrl))
        except Exception:
            pass
    btns.sort(key=lambda x: x[0])
    if not btns:
        return None
    # FLASH 读取 is the second from top (index 1); fallback to first
    target = btns[1] if len(btns) > 1 else btns[0]
    r = target[1].rectangle()
    print(f"FLASH 读取 at ({(r.left + r.right) // 2}, {(r.top + r.bottom) // 2})")
    return target[1]


# --- Step 5: monitor for "Open comm OK!" and click ---
def set_flash_length(dlg, value="00200000"):
    """Find the FLASH section length Edit and set it to value."""
    edits = []
    for ctrl in dlg.children():
        try:
            t = ctrl.window_text().strip()
            if (
                ctrl.class_name() == "Edit"
                and len(t) == 8
                and all(c in "0123456789abcdefABCDEF" for c in t)
            ):
                edits.append((ctrl.rectangle().top, ctrl))
        except Exception:
            pass
    edits.sort(key=lambda x: x[0])
    # FLASH length is typically the second hex Edit field (index 1), after MEMORY length
    for i, (y, ctrl) in enumerate(edits):
        print(f"  Edit[{i}] y={y} val={ctrl.window_text()!r}")
    if len(edits) >= 2:
        ctrl = edits[1][1]
        try:
            ctrl.click_input()
            time.sleep(0.05)
            ctrl.select()
            ctrl.type_keys(value, with_spaces=False)
            print(f"  Length set: {ctrl.window_text()!r}")
            return True
        except Exception as e:
            print(f"  Could not set length: {e}")
    return False


def wait_and_click(dlg, read_btn):
    r = read_btn.rectangle()
    cx = (r.left + r.right) // 2
    cy = (r.top + r.bottom) // 2

    print()
    print("=" * 60)
    print("  READY — press and hold the F7 power button NOW")
    print(f"  Monitoring for up to {BROM_WAIT}s...")
    print("=" * 60)

    deadline = time.monotonic() + BROM_WAIT
    last_status = ""
    while time.monotonic() < deadline:
        try:
            status = ""
            for ctrl in dlg.children():
                try:
                    t = ctrl.window_text().strip()
                    if "open comm ok" in t.lower():
                        print("\nConnected! Setting length to 2MB then reading...")
                        set_flash_length(dlg, "00200000")
                        time.sleep(0.1)
                        print(f"Clicking FLASH 读取 at ({cx},{cy})")
                        mouse.click(button="left", coords=(cx, cy))
                        return True
                    if t in ("Synchroning...", "Synchron error!"):
                        status = t
                except Exception:
                    pass
            if status != last_status:
                print(f"  {status!r}")
                last_status = status
        except Exception:
            pass
        time.sleep(0.05)
    return False


# --- Step 6: wait for dump file ---
def wait_for_dump():
    out = res("bigme_flash_tool_readback_2mb")
    print(f"\nWaiting for {out.name}...")
    deadline = time.monotonic() + 120
    last = -1
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            sz = out.stat().st_size
            if sz != last:
                pct = sz * 100 // 0x200000
                print(f"  {sz:,} / {0x200000:,} bytes ({pct}%)")
                last = sz
            if sz >= 0x200000:
                print(f"\nDump complete: {out}")
                return True
        except FileNotFoundError:
            pass
    print("Timed out.")
    return False


def main():
    main_win = get_main_win()
    select_com(main_win)

    print()
    print("=" * 60)
    print("  Press and hold the F7 power button at any time.")
    print("  Script will keep hammering sync attempts until it catches BROM.")
    print("=" * 60)

    # Rapidly open dialog -> sync attempt -> close -> repeat until "Open comm OK!"
    deadline = time.monotonic() + 120
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        print(f"\nAttempt {attempt} — opening debug dialog...")

        # Close any existing flash dialog first
        for w in all_wins():
            try:
                if (
                    w.window_text().lower() in ("flash operation", "phoenixmc")
                    and w.handle != main_win.handle
                    and len(w.children()) > 5
                ):
                    for c in w.children():
                        try:
                            if c.window_text() in ("退出", "OK", "Close"):
                                c.click_input()
                                break
                        except Exception:
                            pass
            except Exception:
                pass
        time.sleep(0.15)

        before = win_handles()
        click_debug_btn(main_win)

        # Dismiss port-selection error if it appears
        t_end = time.monotonic() + 1.5
        dlg = None
        while time.monotonic() < t_end:
            for w in all_wins():
                if w.handle in before:
                    continue
                try:
                    children = w.children()
                    for c in children:
                        if c.window_text() == "OK":
                            c.click_input()
                            break
                    if len(children) > 5 and w.window_text().lower() in (
                        "flash operation",
                        "phoenixmc",
                    ):
                        dlg = w
                        break
                except Exception:
                    pass
            if dlg:
                break
            time.sleep(0.1)

        if not dlg:
            print("  Dialog didn't open, retrying...")
            continue

        # Check status immediately
        time.sleep(0.3)
        for ctrl in dlg.children():
            try:
                t = ctrl.window_text().strip()
                if "open comm ok" in t.lower():
                    print("\n*** Connected! ***")
                    read_btn = find_read_btn(dlg)
                    if read_btn:
                        r = read_btn.rectangle()
                        cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
                        print(f"Clicking 读取 at ({cx},{cy})")
                        mouse.click(button="left", coords=(cx, cy))
                        wait_for_dump()
                        return
                if t in ("Synchroning...", "Synchron error!"):
                    print(f"  {t!r}")
            except Exception:
                pass

    print("Timed out — could not catch BROM mode.")


if __name__ == "__main__":
    main()
