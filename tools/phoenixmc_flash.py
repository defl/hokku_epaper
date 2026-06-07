"""Step 2: Flash xr_system.img to the Bigme F7 via the open PhoenixMC debug dialog.
No mouse used — all via Windows messages.

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

PHOENIXMC_DIR = (
    pathlib.Path(__file__).parents[1] / ".private/screens/bigme_f7/tools/phoenixmc_v3.1.240901a"
)

IMAGE_PATH = (
    pathlib.Path(__file__).parents[1] / "firmware_bigme_f7/image/xr872/xr_system.img"
).resolve()

BM_CLICK = 0x00F5
WM_SETTEXT = 0x000C


def wins():
    return [
        w for w in Desktop(backend="win32").windows() if w.class_name() not in ("tooltips_class32",)
    ]


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

# ── Dump all controls for diagnosis ───────────────────────────────────────────

print(f"\nDialog: {dlg.window_text()!r}  ({len(dlg.children())} controls)")
print(f"{'#':<4} {'Class':<22} {'Text':<40} {'Rect'}")
print("-" * 100)
children = sorted(dlg.children(), key=lambda c: c.rectangle().top)
for i, c in enumerate(children):
    try:
        txt = repr(c.window_text())
    except Exception:
        txt = "?"
    try:
        cls = c.class_name()
    except Exception:
        cls = "?"
    try:
        r = c.rectangle()
        rect_str = f"({r.left},{r.top})-({r.right},{r.bottom})"
    except Exception:
        rect_str = "?"
    print(f"{i:<4} {cls:<22} {txt:<40} {rect_str}")

# ── Find FLASH 写入 (write/burn) button ────────────────────────────────────────

write_btns = sorted(
    [
        (c.rectangle().top, c)
        for c in dlg.children()
        if c.class_name() == "Button" and ("写入" in c.window_text() or "烧录" in c.window_text())
    ],
    key=lambda x: x[0],
)
print(f"\n{len(write_btns)} write button(s):")
for y, c in write_btns:
    print(f"  y={y}  {c.window_text()!r}  handle={c.handle}")

if not write_btns:
    print("\nERROR: no 写入/烧录 button found — check control dump above")
    sys.exit(1)

# FLASH 写入 is the second from top (below MEMORY 写入), same pattern as read
write_btn = write_btns[1][1] if len(write_btns) > 1 else write_btns[0][1]
print(f"\nUsing write button: {write_btn.window_text()!r} (y={write_btn.rectangle().top})")

# ── Find the file path Edit field ─────────────────────────────────────────────
# PhoenixMC puts a file path field near the write button.
# Strategy: find Edit fields that look like file paths (contain '\\' or '.bin' or '.img'),
# or the Edit field closest to (and above) the write button.

write_btn_y = write_btn.rectangle().top

file_edits = []
for c in dlg.children():
    try:
        if c.class_name() != "Edit":
            continue
        t = c.window_text()
        ry = c.rectangle().top
        # Prefer fields that contain file path indicators or are close above the write btn
        if "\\" in t or ".bin" in t or ".img" in t or ".BIN" in t or ".IMG" in t:
            file_edits.append((abs(ry - write_btn_y), c))
        elif ry < write_btn_y and ry > write_btn_y - 200:
            file_edits.append((abs(ry - write_btn_y), c))
    except Exception:
        pass

file_edits.sort(key=lambda x: x[0])
print(f"\n{len(file_edits)} file edit candidate(s):")
for dist, c in file_edits:
    print(f"  dist={dist}  val={c.window_text()!r}  y={c.rectangle().top}")

if not file_edits:
    print("\nERROR: no file path Edit field found — check control dump above")
    sys.exit(1)

file_edit = file_edits[0][1]
print(f"\nImage: {IMAGE_PATH}")

if not IMAGE_PATH.exists():
    print(f"ERROR: image not found: {IMAGE_PATH}")
    sys.exit(1)

# Set file path via WM_SETTEXT (works even on read-only-styled edits)
img_str = str(IMAGE_PATH)
ctypes.windll.user32.SendMessageW(file_edit.handle, WM_SETTEXT, 0, img_str)
time.sleep(0.1)
actual = file_edit.window_text()
print(f"File field set to: {actual!r}")
if IMAGE_PATH.name not in actual and str(IMAGE_PATH) not in actual:
    print("WARNING: field text doesn't match — trying set_edit_text()")
    file_edit.set_edit_text(img_str)
    print(f"  After set_edit_text: {file_edit.window_text()!r}")

# ── Set FLASH address to 0x00000000 ───────────────────────────────────────────

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
print("\nHex edit fields:")
for i, (y, c) in enumerate(hex_edits):
    print(f"  [{i}] y={y}  {c.window_text()!r}")

# FLASH addr field is typically index [3] (after MEMORY len, FLASH len, FLASH addr)
# Set it to 0 to be safe
if len(hex_edits) >= 4:
    hex_edits[3][1].set_edit_text("00000000")
    print(f"FLASH addr -> {hex_edits[3][1].window_text()!r}")

# ── Click FLASH 写入 ───────────────────────────────────────────────────────────

print("\nSending BM_CLICK to FLASH 写入 (no mouse)...")
ctypes.windll.user32.SendMessageW(write_btn.handle, BM_CLICK, 0, 0)

# ── Monitor status ─────────────────────────────────────────────────────────────

print("Monitoring status (up to 10 min)...")
deadline = time.monotonic() + 600
last_status = ""
while time.monotonic() < deadline:
    time.sleep(1)
    for c in dlg.children():
        try:
            t = c.window_text().strip()
            if not t or t == last_status:
                continue
            # Status label changes during flash
            if any(
                kw in t.lower()
                for kw in (
                    "writing",
                    "flashing",
                    "success",
                    "ok",
                    "error",
                    "fail",
                    "complete",
                    "finish",
                    "%",
                    "写入",
                    "成功",
                    "失败",
                    "完成",
                )
            ):
                print(f"  {t!r}")
                last_status = t
                if any(
                    kw in t.lower()
                    for kw in ("success", "ok", "complete", "finish", "成功", "完成")
                ):
                    print("\nDone! Flash appears successful.")
                    sys.exit(0)
                if any(kw in t.lower() for kw in ("error", "fail", "失败")):
                    print("\nERROR: Flash failed.")
                    sys.exit(1)
        except Exception:
            pass

print("Timed out — check PhoenixMC status manually.")
