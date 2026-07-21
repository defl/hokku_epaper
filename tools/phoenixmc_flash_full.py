"""Launch PhoenixMC, auto-connect, erase+flash xr_system.img, reboot, capture UART.
No user interaction needed — device connects to BROM automatically.
"""

import ctypes
import io
import pathlib
import subprocess
import sys
import time

import serial

sys.stdout = io.TextIOWrapper(
    sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
)
from pywinauto import Desktop

from _private import res

ROOT = pathlib.Path(__file__).parents[1]
IMAGE = ROOT / "firmware/bigme_f7" / "image" / "xr872" / "xr_system.img"
PMC_DIR = res("bigme_flash_tool_dir")
PMC_EXE = res("bigme_flash_tool_exe")
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


def remote_lv_setstate(lv_hwnd, idx, state, mask):
    pid = ctypes.c_ulong(0)
    ctypes.windll.user32.GetWindowThreadProcessId(lv_hwnd, ctypes.byref(pid))
    proc = ctypes.windll.kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid.value)
    size = ctypes.sizeof(LVITEM32)
    remote = ctypes.windll.kernel32.VirtualAllocEx(
        proc, None, size, MEM_COMMIT_RESERVE, PAGE_READWRITE
    )
    item = LVITEM32(mask=LVIF_STATE, iItem=idx, state=state, stateMask=mask)
    written = ctypes.c_size_t(0)
    ctypes.windll.kernel32.WriteProcessMemory(
        proc, remote, ctypes.byref(item), size, ctypes.byref(written)
    )
    ctypes.windll.user32.SendMessageW(lv_hwnd, LVM_SETITEMSTATE, idx, remote)
    ctypes.windll.kernel32.VirtualFreeEx(proc, remote, 0, MEM_RELEASE)
    ctypes.windll.kernel32.CloseHandle(proc)


def wins():
    return [
        w for w in Desktop(backend="win32").windows() if w.class_name() not in ("tooltips_class32",)
    ]


def hex_fields(dlg):
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


# ── Launch PhoenixMC ──────────────────────────────────────────────────────────
print("Launching PhoenixMC...", flush=True)
subprocess.Popen([str(PMC_EXE)], cwd=str(PMC_DIR))

main_w = None
for _ in range(50):
    time.sleep(0.3)
    for w in wins():
        try:
            if "PhoenixMC version" in w.window_text() and len(w.children()) > 30:
                main_w = w
                break
        except Exception:
            pass
    if main_w:
        break
if not main_w:
    print("ERROR: PhoenixMC main window not found")
    sys.exit(1)
print(f"Main window: {main_w.window_text()!r}", flush=True)

# ── Check COM6 checkbox in listview ──────────────────────────────────────────
for ctrl in main_w.children():
    try:
        if ctrl.class_name() == "SysListView32" and ctrl.rectangle().left < 950:
            n = ctypes.windll.user32.SendMessageW(ctrl.handle, LVM_GETITEMCOUNT, 0, 0)
            print(f"COM listview: {n} item(s)", flush=True)
            if n > 0:
                remote_lv_setstate(
                    ctrl.handle, 0, LVIS_SELECTED | LVIS_FOCUSED, LVIS_SELECTED | LVIS_FOCUSED
                )
                remote_lv_setstate(ctrl.handle, 0, 0x2000, LVIS_STATEIMAGEMASK)
                print("COM6 checked", flush=True)
    except Exception as e:
        print(f"Listview error: {e}", flush=True)
time.sleep(0.3)


# ── Click 调试 ────────────────────────────────────────────────────────────────
debug_btn = next((c for c in main_w.children() if c.window_text() == "调试"), None)
if not debug_btn:
    print("ERROR: no 调试 button")
    sys.exit(1)

dlg = None
for attempt in range(5):
    before = {w.handle for w in wins()}
    # PostMessageW is async — avoids deadlock if click opens a modal popup
    ctypes.windll.user32.PostMessageW(debug_btn.handle, BM_CLICK, 0, 0)
    time.sleep(1.5)
    # Dismiss "Please select a COM port!" or similar error popups
    for w in wins():
        try:
            if w.handle not in before and len(w.children()) <= 6:
                ok = next((c for c in w.children() if c.window_text() in ("OK", "确定")), None)
                if ok:
                    print(f"Dismissed popup: {w.window_text()!r}", flush=True)
                    ctypes.windll.user32.PostMessageW(ok.handle, BM_CLICK, 0, 0)
                    time.sleep(0.5)
        except Exception:
            pass
    # Look for the debug dialog
    dlg = next(
        (
            w
            for w in wins()
            if w.window_text() in ("flash operation", "phoenixMC") and len(w.children()) > 5
        ),
        None,
    )
    if dlg:
        break
    print(f"  attempt {attempt + 1}: dialog not found, retrying...", flush=True)
    time.sleep(0.5)

if not dlg:
    print("ERROR: debug dialog not found after 5 attempts")
    sys.exit(1)
print(f"Dialog: {dlg.window_text()!r}", flush=True)


# ── Wait for Open comm OK! ────────────────────────────────────────────────────
print("Waiting for device to connect (auto)...", flush=True)
deadline = time.monotonic() + 60
connected = False
while time.monotonic() < deadline:
    for c in dlg.children():
        try:
            t = c.window_text().strip().lower().replace(" ", "")
            if "opencommok" in t:
                connected = True
                break
        except Exception:
            pass
    if connected:
        break
    time.sleep(0.5)
if not connected:
    print("ERROR: device did not connect within 60s")
    sys.exit(1)
print("Connected!", flush=True)


# ── Erase ─────────────────────────────────────────────────────────────────────
img_size = IMAGE.stat().st_size
erase_len = (img_size + 0xFFFF) & ~0xFFFF
edits = hex_fields(dlg)
edits[2][1].set_edit_text(f"{erase_len:08X}")
edits[3][1].set_edit_text("00000000")
print(f"Erasing 0x{erase_len:X} ({erase_len:,}) bytes...", flush=True)

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
    print("ERROR: no erase button")
    sys.exit(1)
ctypes.windll.user32.SendMessageW(erase_btns[0][1].handle, BM_CLICK, 0, 0)

deadline = time.monotonic() + 180
last = ""
while time.monotonic() < deadline:
    time.sleep(1)
    done = False
    for c in dlg.children():
        try:
            t = c.window_text().strip()
            if (
                t
                and t != last
                and any(k in t.lower() for k in ("eras", "擦", "%", "ok", "error", "fail"))
            ):
                print(f"  {t}", flush=True)
                last = t
                if "ok" in t.lower():
                    done = True
                if "error" in t.lower() or "fail" in t.lower():
                    print("ERROR: erase failed")
                    sys.exit(1)
        except Exception:
            pass
    if done:
        break
print("Erase done", flush=True)


# ── Write ─────────────────────────────────────────────────────────────────────
edits = hex_fields(dlg)
edits[2][1].set_edit_text(f"{img_size:08X}")
edits[3][1].set_edit_text("00000000")
print(f"Writing {IMAGE.name} ({img_size:,} bytes)...", flush=True)

write_btns = sorted(
    [
        (c.rectangle().top, c)
        for c in dlg.children()
        if c.class_name() == "Button" and "写入" in c.window_text()
    ],
    key=lambda x: x[0],
)
if not write_btns:
    print("ERROR: no write button")
    sys.exit(1)
write_btn = write_btns[1][1] if len(write_btns) > 1 else write_btns[0][1]

before_w = {w.handle for w in wins()}
ctypes.windll.user32.SendMessageW(write_btn.handle, BM_CLICK, 0, 0)

file_dlg = None
dl = time.monotonic() + 8
while time.monotonic() < dl:
    for w in wins():
        if w.handle in before_w:
            continue
        try:
            if w.class_name() in ("#32770", "NativeHWNDHost") or any(
                k in w.window_text().lower() for k in ("open", "file", "打开")
            ):
                file_dlg = w
                break
        except Exception:
            pass
    if file_dlg:
        break
    time.sleep(0.1)
if not file_dlg:
    print("ERROR: file dialog did not appear")
    sys.exit(1)

time.sleep(0.3)
edit = None
for c in file_dlg.children():
    try:
        if c.class_name() == "Edit":
            edit = c
            break
        if c.class_name() in ("ComboBoxEx32", "ComboBox"):
            for cc in c.children():
                if cc.class_name() == "Edit":
                    edit = cc
                    break
            if edit:
                break
    except Exception:
        pass
if not edit:
    print("ERROR: no filename edit in file dialog")
    sys.exit(1)

ctypes.windll.user32.SendMessageW(edit.handle, WM_SETTEXT, 0, str(IMAGE))
time.sleep(0.1)

open_btn = next(
    (
        c
        for c in file_dlg.children()
        if c.class_name() == "Button"
        and c.window_text() in ("&Open", "Open", "OK", "&OK", "打开", "确定")
    ),
    None,
)
if not open_btn:
    open_btn = next(
        (
            c
            for c in file_dlg.children()
            if c.class_name() == "Button"
            and c.window_text()
            and "cancel" not in c.window_text().lower()
            and "取消" not in c.window_text()
        ),
        None,
    )
if not open_btn:
    print("ERROR: no open button in file dialog")
    sys.exit(1)
ctypes.windll.user32.SendMessageW(open_btn.handle, BM_CLICK, 0, 0)

deadline = time.monotonic() + 600
last = ""
while time.monotonic() < deadline:
    time.sleep(1)
    done = False
    for c in dlg.children():
        try:
            t = c.window_text().strip()
            if (
                t
                and t != last
                and any(k in t.lower() for k in ("writ", "%", "ok", "error", "fail"))
            ):
                print(f"  {t}", flush=True)
                last = t
                if "write ok" in t.lower():
                    done = True
                if "error" in t.lower() or "fail" in t.lower():
                    print("ERROR: write failed")
                    sys.exit(1)
        except Exception:
            pass
    if done:
        break
print("Write OK!", flush=True)


# ── Reboot + capture UART ─────────────────────────────────────────────────────
reboot_btn = next(
    (
        c
        for c in dlg.children()
        if c.class_name() == "Button" and "reboot" in c.window_text().lower()
    ),
    None,
)
if reboot_btn:
    print("Rebooting...", flush=True)
    ctypes.windll.user32.SendMessageW(reboot_btn.handle, BM_CLICK, 0, 0)

pids = subprocess.check_output(
    [  # noqa: S607
        "powershell",
        "-Command",
        'Get-Process | Where-Object { $_.Name -match "phoenixMC" } | Select-Object -ExpandProperty Id',
    ],
    text=True,
).split()
k32 = ctypes.windll.kernel32
for pid in pids:
    try:
        h = k32.OpenProcess(0x0001, False, int(pid))
        if h:
            k32.TerminateProcess(h, 0)
            k32.CloseHandle(h)
    except Exception:
        pass

ser = None
dl2 = time.monotonic() + 2
while time.monotonic() < dl2:
    try:
        ser = serial.Serial("COM6", 115200, timeout=0.1)
        break
    except Exception:
        time.sleep(0.01)
if not ser:
    print("Could not open COM6")
    sys.exit(1)

print("COM6 open — capturing 30s boot log...", flush=True)
print("-" * 60, flush=True)
dl3 = time.monotonic() + 30
total = 0
while time.monotonic() < dl3:
    d = ser.read(256)
    if d:
        total += len(d)
        sys.stdout.buffer.write(d)
        sys.stdout.buffer.flush()
ser.close()
print(f"\n--- {total} bytes received ---", flush=True)
