"""Step 2: Set FLASH length to 4MB and click read in the open debug dialog.
No mouse used — all via Windows messages.

Usage:
    python tools/phoenixmc_read.py
"""

import io
import pathlib
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from pywinauto import Desktop

PHOENIXMC_DIR = (
    pathlib.Path(__file__).parents[1] / ".private/screens/bigme_f7/tools/phoenixmc_v3.1.240901a"
)


def wins():
    return [
        w for w in Desktop(backend="win32").windows() if w.class_name() not in ("tooltips_class32",)
    ]


dlg = next(
    (w for w in wins() if w.window_text() == "flash operation" and len(w.children()) > 5), None
)
if not dlg:
    print("ERROR: flash operation dialog not found")
    sys.exit(1)

# Dump all hex edit fields for reference
edits = sorted(
    [
        (c.rectangle().top, c)
        for c in dlg.children()
        if c.class_name() == "Edit"
        and len(c.window_text().strip()) == 8
        and all(ch in "0123456789abcdefABCDEF" for ch in c.window_text().strip())
    ],
    key=lambda x: x[0],
)
print("Edit fields:")
for i, (y, c) in enumerate(edits):
    print(f"  [{i}] y={y}  {c.window_text()!r}")

# FLASH length = field [2] (y~587, originally '00000200')
# FLASH addr   = field [3] (y~589, '00000000')
if len(edits) < 3:
    print("ERROR: expected at least 3 edit fields")
    sys.exit(1)

edits[2][1].set_edit_text("00400000")
edits[3][1].set_edit_text("00000000")
print(f"FLASH length -> {edits[2][1].window_text()!r}")
print(f"FLASH addr   -> {edits[3][1].window_text()!r}")

# Click FLASH 读取 — second 读取 button by Y (no mouse)
btns = sorted(
    [
        (c.rectangle().top, c)
        for c in dlg.children()
        if c.class_name() == "Button" and "读取" in c.window_text()
    ],
    key=lambda x: x[0],
)
print(f"\n{len(btns)} read buttons:")
for y, c in btns:
    print(f"  y={y}  {c.window_text()!r}")

btn = btns[1][1] if len(btns) > 1 else btns[0][1]
print(f"Sending BM_CLICK to FLASH 读取 (y={btn.rectangle().top}, no mouse)...")
btn.click()  # BM_CLICK — no mouse movement

# Monitor output file — PhoenixMC names it flash_A_0x0_L_0x<length>.bin
candidates = [
    PHOENIXMC_DIR / "flash_A_0x0_L_0x400000.bin",
    PHOENIXMC_DIR / "flash_A_0x0_L_0x200000.bin",
]
print("Waiting for dump file to grow...")
deadline = time.monotonic() + 120
last_sz = -1
while time.monotonic() < deadline:
    time.sleep(1)
    for f in candidates:
        try:
            sz = f.stat().st_size
            if sz > 0 and sz != last_sz:
                pct = sz * 100 // 0x400000
                print(f"  {f.name}: {sz:,} bytes ({pct}%)")
                last_sz = sz
            if sz >= 0x200000:
                print(f"\nDone! {f}  ({sz:,} bytes)")
                sys.exit(0)
        except FileNotFoundError:
            pass

print("Timed out.")
