"""Step 2: Flash xr_system.img to the Bigme F7 via the open PhoenixMC debug dialog.
No mouse used — all via Windows messages.

The FLASH 写入 button opens a Windows file-open dialog; this script handles that
dialog by injecting the path into the filename field and clicking Open.

Usage (after phoenixmc_open.py has connected the device):
    python tools/phoenixmc_flash.py
"""

import ctypes
import io
import pathlib
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from pywinauto import Desktop

IMAGE_PATH = (
    pathlib.Path(__file__).parents[1] / "firmware/bigme_f7/image/xr872/xr_system.img"
).resolve()

BM_CLICK = 0x00F5
WM_SETTEXT = 0x000C


def wins():
    return [
        w for w in Desktop(backend="win32").windows() if w.class_name() not in ("tooltips_class32",)
    ]


def win_handles():
    return {w.handle for w in wins()}


# ── Find flash dialog ──────────────────────────────────────────────────────────

dlg = next(
    (
        w
        for w in wins()
        if w.window_text() in ("flash operation", "phoenixMC") and len(w.children()) > 5
    ),
    None,
)
if not dlg:
    print("ERROR: flash operation dialog not found — run phoenixmc_open.py first")
    sys.exit(1)
print(f"Flash dialog: {dlg.window_text()!r}")

# ── Sanity-check image ─────────────────────────────────────────────────────────

if not IMAGE_PATH.exists():
    print(f"ERROR: image not found: {IMAGE_PATH}")
    sys.exit(1)
print(f"Image: {IMAGE_PATH}  ({IMAGE_PATH.stat().st_size:,} bytes)")

# ── Set FLASH address=0 and length=image size ─────────────────────────────────
# Hex edit fields sorted top-to-bottom. With the full PhoenixMC dialog there are
# 8 fields (MEMORY×2, FLASH×2, EXTERN×2, and two more). From the observed layout:
#   [0] y≈447  MEMORY addr/len
#   [1] y≈480  MEMORY addr/len
#   [2] y≈587  FLASH length  ← must set to image size; default 0x00000200 = 512 B
#   [3] y≈589  FLASH address ← set to 00000000
# PhoenixMC respects the length field for both reads and writes.

img_size = IMAGE_PATH.stat().st_size
img_len_hex = f"{img_size:08X}"

hex_edits = sorted(
    [
        (c.rectangle().top, c)
        for c in dlg.children()
        if c.class_name() == "Edit"
        and len(c.window_text().strip()) == 8
        and all(ch in "0123456789abcdefABCDEF" for ch in c.window_text().strip())
    ],
    key=lambda x: x[0],
)
print("Hex edit fields:")
for i, (y, c) in enumerate(hex_edits):
    print(f"  [{i}] y={y}  {c.window_text()!r}")

if len(hex_edits) >= 4:
    hex_edits[2][1].set_edit_text(img_len_hex)
    hex_edits[3][1].set_edit_text("00000000")
    print(f"FLASH length -> {hex_edits[2][1].window_text()!r}  ({img_size:,} bytes)")
    print(f"FLASH addr   -> {hex_edits[3][1].window_text()!r}")
else:
    print("WARNING: expected 4 hex fields, found fewer — length/addr not set")

# ── Erase before write ────────────────────────────────────────────────────────
# NOR flash can only clear bits (1→0); setting bits back (0→1) requires an
# explicit sector erase.  Without erase, any previously-programmed OEM bytes
# that are 0 stay 0 even if our image wants them to be 1, producing a bitwise-
# AND corruption and an invalid header checksum → bootloader calls bl_upgrade().
# We erase the full image region (rounded up to the next 64K boundary) first.

erase_len = (img_size + 0xFFFF) & ~0xFFFF  # round up to 64K
erase_len_hex = f"{erase_len:08X}"

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
print(f"\n{len(erase_btns)} 擦除 button(s):")
for y, c in erase_btns:
    print(f"  y={y}  {c.window_text()!r}")

if erase_btns:
    # Set length to cover full image (rounded to 64K)
    hex_edits[2][1].set_edit_text(erase_len_hex)
    print(f"Erasing 0x{erase_len:X} bytes ({erase_len:,}) at address 0...")
    ctypes.windll.user32.SendMessageW(erase_btns[0][1].handle, BM_CLICK, 0, 0)

    # Wait for erase to complete (~10 s for 1 MB at 115200 baud)
    deadline = time.monotonic() + 120
    last_status = ""
    erased = False
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
                        erased = True
                    if "error" in tl or "fail" in tl or "失败" in tl:
                        print("ERROR: erase failed")
                        sys.exit(1)
            except Exception:
                pass
        if erased:
            break
    if not erased:
        print("WARNING: erase timeout — proceeding anyway")

    # Restore image length for the write
    hex_edits[2][1].set_edit_text(img_len_hex)
    print(f"FLASH length restored -> {hex_edits[2][1].window_text()!r}")
else:
    print("WARNING: no 擦除 button found — writing without erase (may corrupt)")

# ── Find FLASH 写入 button (second one by Y — below MEMORY 写入) ──────────────

write_btns = sorted(
    [
        (c.rectangle().top, c)
        for c in dlg.children()
        if c.class_name() == "Button" and "写入" in c.window_text()
    ],
    key=lambda x: x[0],
)
print(f"\n{len(write_btns)} 写入 button(s):")
for y, c in write_btns:
    print(f"  y={y}  {c.window_text()!r}")

if not write_btns:
    print("ERROR: no 写入 button found")
    sys.exit(1)

write_btn = write_btns[1][1] if len(write_btns) > 1 else write_btns[0][1]
print(f"Using FLASH 写入 at y={write_btn.rectangle().top}")

# ── Click 写入 — expect a file-open dialog to appear ──────────────────────────

before = win_handles()
print("\nClicking FLASH 写入 (expecting file-open dialog)...")
ctypes.windll.user32.SendMessageW(write_btn.handle, BM_CLICK, 0, 0)

# Wait for the file-open dialog (class #32770, common dialog)
file_dlg = None
deadline = time.monotonic() + 8
while time.monotonic() < deadline:
    for w in wins():
        if w.handle in before:
            continue
        try:
            cls = w.class_name()
            txt = w.window_text()
            # Common file dialog is #32770; also check for Explorer-style dialog
            if cls in ("#32770", "NativeHWNDHost") or any(
                kw in txt.lower() for kw in ("open", "打开", "select", "选择", "file")
            ):
                file_dlg = w
                print(f"File dialog: {txt!r}  class={cls}")
                break
        except Exception:
            pass
    if file_dlg:
        break
    time.sleep(0.1)

if not file_dlg:
    print("ERROR: file-open dialog did not appear — PhoenixMC may have rejected the click")
    sys.exit(1)

# ── Inject file path into filename field ──────────────────────────────────────
# The common dialog filename field is an Edit or ComboBoxEx32/ComboBox child.
# WM_SETTEXT on the Edit handle is the most reliable approach.

time.sleep(0.3)  # let dialog fully render

filename_edit = None
for c in file_dlg.children():
    try:
        cls = c.class_name()
        if cls == "Edit":
            filename_edit = c
            break
        if cls in ("ComboBoxEx32", "ComboBox"):
            # Find the Edit inside the combo
            for cc in c.children():
                if cc.class_name() == "Edit":
                    filename_edit = cc
                    break
            if filename_edit:
                break
    except Exception:
        pass

# If not found as direct child, search one level deeper
if not filename_edit:
    for c in file_dlg.children():
        try:
            for cc in c.children():
                if cc.class_name() == "Edit":
                    filename_edit = cc
                    break
        except Exception:
            pass
        if filename_edit:
            break

if not filename_edit:
    print("ERROR: no filename Edit field found in file dialog — dump of controls:")
    for c in file_dlg.children():
        try:
            print(f"  {c.class_name():<22} {c.window_text()!r}")
        except Exception:
            pass
    sys.exit(1)

img_str = str(IMAGE_PATH)
ctypes.windll.user32.SendMessageW(filename_edit.handle, WM_SETTEXT, 0, img_str)
time.sleep(0.1)
print(f"Filename field: {filename_edit.window_text()!r}")

# ── Click Open/OK ──────────────────────────────────────────────────────────────

open_btn = None
for c in file_dlg.children():
    try:
        t = c.window_text()
        if c.class_name() == "Button" and t in ("&Open", "Open", "打开", "&OK", "OK", "确定"):
            open_btn = c
            break
    except Exception:
        pass

if not open_btn:
    # Fallback: first Button with non-empty text that isn't Cancel
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
        except Exception:
            pass

if not open_btn:
    print("ERROR: Open button not found — dialog controls:")
    for c in file_dlg.children():
        try:
            print(f"  {c.class_name():<22} {c.window_text()!r}")
        except Exception:
            pass
    sys.exit(1)

print(f"Clicking Open: {open_btn.window_text()!r}")
ctypes.windll.user32.SendMessageW(open_btn.handle, BM_CLICK, 0, 0)

# ── Monitor flash progress ─────────────────────────────────────────────────────

print("\nMonitoring flash progress (up to 10 min)...")
deadline = time.monotonic() + 600
last_status = ""

# Labels on static buttons — filter these out of status monitoring
BUTTON_NOISE = {"写入", "写入ram", "写入info", "读取", "擦除", "flash id", "reboot"}

while time.monotonic() < deadline:
    time.sleep(1)
    try:
        for c in dlg.children():
            try:
                t = c.window_text().strip()
                if not t or t == last_status:
                    continue
                if t.lower() in BUTTON_NOISE:
                    continue
                if any(
                    kw in t.lower()
                    for kw in (
                        "write",
                        "writing",
                        "flashing",
                        "success",
                        "ok",
                        "error",
                        "fail",
                        "complete",
                        "finish",
                        "%",
                        "成功",
                        "失败",
                        "完成",
                    )
                ):
                    print(f"  {t!r}")
                    last_status = t
                    tl = t.lower()
                    if "write ok" in tl or any(
                        kw in tl for kw in ("success", "complete", "finish", "成功", "完成")
                    ):
                        print("\nDone! Flash successful.")
                        sys.exit(0)
                    if any(kw in tl for kw in ("error", "fail", "失败")):
                        print("\nERROR: Flash failed.")
                        sys.exit(1)
            except Exception:
                pass
    except Exception:
        pass

print("Timed out — check PhoenixMC status manually.")
