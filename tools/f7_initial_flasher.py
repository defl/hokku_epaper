#!/usr/bin/env python3
"""Initial USB flash of a fresh/stock Bigme F7 to Hokku firmware.

A stock unit can't answer `upgrade` (that's our firmware's command), so entry is via
the XR872 mask-BROM. The catch works IN PURE PYTHON if triggered correctly: hammer
0x55 on an already-open CH340 port while the user does a **USB replug + power press**
(the replug brings the port up first — no re-enumeration gap; a plain long-press
drops the port and misses the window). No vendor tool needed.

Modes:

  pure-python (DEFAULT) — catch the BROM via the replug+press, then flash_slot.
      Writes ONLY slot0 (our app-chain) + the A/B cfg; NEVER erases the bootloader
      or slot1. Portable (works on a Pi). No PhoenixMC.

  --phoenixmc — fallback: the vendor GUI tool catches the BROM (Windows-only), then
      is killed to free the port and the in-BROM device is handed to flash_slot.

  --full <oem_dump> — fallback: PhoenixMC full-erase + write of a composed 4 MB
      image (OEM base + our app in slot0 + cfg -> seq0). Proven, but a full erase
      momentarily blanks the bootloader.

WiFi/server config are NOT flashed — provision them over the console afterward
(`wifi <ssid> <pw>`, `cfg server <url>`, `cfg name <n>`, `cfg save`), then the unit
takes all future updates over-the-air.

Usage:
  python tools/f7_initial_flasher.py [--port COM7] [--image xr_system.img]
  -> when prompted: UNPLUG + REPLUG USB, then PRESS the power button (repeat).
"""

from __future__ import annotations

import argparse
import contextlib
import pathlib
import subprocess
import sys
import time

import serial

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "python"))

from hokku.common.xr872.catch import hammer_sync, open_stable  # noqa: E402
from hokku.common.xr872.flasher import XR872Flasher  # noqa: E402
from hokku.common.xr872.slots import BL_SIZE, OTA_ADDR, build_fdcm, flash_slot  # noqa: E402

FLASH_SIZE = 0x400000
SLOT0_APP = BL_SIZE  # 0x8000
OTA_SECTOR_END = OTA_ADDR + 0x1000  # 0x181000
DEFAULT_IMAGE = _HERE.parents[0] / "firmware/bigme_f7" / "image" / "xr872" / "xr_system.img"

PROVISION_HELP = (
    "\nPOWER-CYCLE to boot (unplug/replug USB, or long-press) — sys_reboot leaves the\n"
    "chip in BROM, and the e-paper stays on its old image until WiFi is set. Then\n"
    "provision over the UART console (115200):\n"
    "  wifi <ssid> <password>\n"
    "  cfg save    (server URL + screen name default from the compiled-in config)\n"
    "It then associates, fetches an image, and takes all future updates over-the-air."
)


def _catch_python(image: pathlib.Path, port: str) -> int:
    """Fully pure-Python bootstrap — no vendor tool, works on any platform.

    Catches the XR872 mask-BROM by hammering 0x55 on an already-open CH340 port
    while the user does a USB **replug + power press** (the replug brings the port
    up first, so there's no re-enumeration gap to miss — a plain long-press drops
    the port and fails). Then hands the in-BROM device to the proven flash_slot."""
    img = image.read_bytes()
    if img[:4] != b"AWIH":
        raise SystemExit("image is not an AWIH xr_system.img")
    print(f"PURE-PYTHON initial flash  image={image} ({len(img):,} B)  port={port}")
    print(">>> No vendor tool needed. To enter the BROM:")
    print(">>>   1. UNPLUG the USB cable")
    print(">>>   2. REPLUG it")
    print(">>>   3. PRESS the power button (short)")
    print(">>>   ...repeat the replug+press until it syncs.\n")

    deadline = time.monotonic() + 300
    last = 0.0
    while time.monotonic() < deadline:
        try:
            ser = open_stable(port)
        except (serial.SerialException, OSError):
            if time.monotonic() - last > 3:
                print("  waiting for port... replug USB + press the power button")
                last = time.monotonic()
            continue
        try:
            if hammer_sync(ser, duration=2.0):
                print("\n  *** BROM SYNC CAUGHT — flashing ***")
                f = XR872Flasher(ser=ser, verbose=False)
                if not f.sync(attempts=30, timeout_per=0.1):
                    print("  command sync failed — keep trying (replug + press)")
                    continue
                # sys_reboot leaves the XR872 in BROM, so reboot=False and let the
                # user power-cycle (a clean power-on is what actually boots the app).
                flash_slot(f, img, slot=0, reboot=False, allow_active_slot=True)
                print("\nDONE (pure-Python)." + PROVISION_HELP)
                return 0
        finally:
            with contextlib.suppress(Exception):
                ser.close()
    raise SystemExit("no BROM catch in the window — retry the replug+press, or use --phoenixmc")


def _catch_brom_via_phoenixmc(kill_after: bool):
    """Drive PhoenixMC to catch the BROM on the user's long-press.

    Returns the connected dialog. If kill_after, PhoenixMC is terminated so the
    CH340 port is freed for a pure-Python hand-off (the device stays in BROM)."""
    from _private import res  # noqa: PLC0415
    from bigme_f7_restore_and_verify import (  # noqa: PLC0415
        already_connected_dialog,
        phase_launch,
        phase_wait_brom,
    )

    # bigme_f7_restore_and_verify replaces sys.stdout with a block-buffered wrapper
    # on import — restore line buffering so progress is visible live.
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

    print("\n>>> Launching PhoenixMC. To enter the BROM: UNPLUG + REPLUG the USB,")
    print(">>> then PRESS the power button — repeat until it catches.\n")
    dlg = already_connected_dialog() or phase_launch()
    if dlg is None:
        raise SystemExit("PhoenixMC did not connect")
    phase_wait_brom(dlg)
    print(">>> BROM caught by PhoenixMC.")
    if kill_after:
        exe = res("bigme_flash_tool_exe")
        print(f">>> Killing {exe.name} to free the port (device stays in BROM)...")
        subprocess.run(["taskkill", "/F", "/IM", exe.name], capture_output=True)  # noqa: S607
        time.sleep(2.0)
    return dlg


def _surgical(image: pathlib.Path, port: str) -> int:
    img = image.read_bytes()
    if img[:4] != b"AWIH":
        raise SystemExit("image is not an AWIH xr_system.img")
    print(f"SURGICAL initial flash  image={image} ({len(img):,} B)  port={port}")

    _catch_brom_via_phoenixmc(kill_after=True)

    print(f">>> Handing off: syncing pure-Python to the in-BROM device on {port}...")
    f = None
    for attempt in range(25):
        try:
            ft = XR872Flasher(port, verbose=False)
            if ft.sync(attempts=10, timeout_per=0.2):
                f = ft
                print(f"  BROM sync OK (attempt {attempt + 1})")
                break
            ft.close()
        except Exception:
            time.sleep(0.3)
    if f is None:
        raise SystemExit(
            "could not sync to the in-BROM device — hand-off failed (it may not have "
            "stayed in BROM). Re-run with `--full <oem_dump>` for the composed full-flash."
        )
    with f:
        # flash_slot validates the live OEM bootloader header, writes only the
        # app-chain into slot0, read-back verifies, flips the cfg to seq0 LAST, and
        # leaves the bootloader + slot1 untouched. reboot=False: sys_reboot only
        # re-enters BROM here, so the user power-cycles to boot the app.
        flash_slot(f, img, slot=0, reboot=False, allow_active_slot=True)
    print("\nDONE (surgical)." + PROVISION_HELP)
    return 0


def _compose_bootstrap(oem_path: pathlib.Path, image: pathlib.Path) -> bytes:
    oem = bytearray(oem_path.read_bytes())
    if len(oem) != FLASH_SIZE:
        raise SystemExit(f"OEM dump is {len(oem)} B, expected {FLASH_SIZE}")
    img = image.read_bytes()
    if img[:4] != b"AWIH":
        raise SystemExit("image is not an AWIH xr_system.img")
    app = img[BL_SIZE:]
    if SLOT0_APP + len(app) > OTA_ADDR:
        raise SystemExit(f"app-chain too big: ends 0x{SLOT0_APP + len(app):x} >= 0x{OTA_ADDR:x}")
    oem[SLOT0_APP:OTA_ADDR] = b"\xff" * (OTA_ADDR - SLOT0_APP)
    oem[SLOT0_APP : SLOT0_APP + len(app)] = app
    oem[OTA_ADDR:OTA_SECTOR_END] = build_fdcm(0) + b"\xff" * (0x1000 - 512)
    return bytes(oem)  # bootloader 0x0-0x8000 kept -> ota_addr stays 0x180000


def _full(oem_dump: pathlib.Path, image: pathlib.Path, port: str) -> int:
    import ctypes  # noqa: PLC0415

    from _private import res  # noqa: PLC0415
    from bigme_f7_restore_and_verify import _flash_image, get_hex_edit_fields  # noqa: PLC0415

    ref = _compose_bootstrap(oem_dump, image)
    out = oem_dump.parent / "bootstrap_composed.bin"  # .private — has the unit's serial/config
    out.write_bytes(ref)
    print(f"FULL initial flash  composed={out} ({len(ref):,} B)  port={port}")
    print("  bootloader kept from OEM (ota_addr stays 0x180000); slot0=our app; cfg->seq0")

    dlg = _catch_brom_via_phoenixmc(kill_after=False)
    print("\n=== Erase + write composed image (4 MB) ===")
    _flash_image(dlg, out, "BOOTSTRAP", erase=True)

    print("\n=== Read back + verify ===")
    rb = res("bigme_flash_tool_readback")
    if rb.exists():
        rb.unlink()
    he = get_hex_edit_fields(dlg)
    he[2][1].set_edit_text("00400000")
    he[3][1].set_edit_text("00000000")
    read_btns = sorted(
        [
            (c.rectangle().top, c)
            for c in dlg.children()
            if c.class_name() == "Button" and "读取" in c.window_text()
        ],
        key=lambda x: x[0],
    )
    if read_btns:
        btn = read_btns[1][1] if len(read_btns) > 1 else read_btns[0][1]
        ctypes.windll.user32.SendMessageW(btn.handle, 0xF5, 0, 0)
        deadline = time.monotonic() + 420
        while time.monotonic() < deadline:
            time.sleep(2)
            if rb.exists() and rb.stat().st_size >= FLASH_SIZE:
                break
        got = rb.read_bytes() if rb.exists() else b""
        diffs = sum(1 for i in range(min(len(got), len(ref))) if got[i] != ref[i])
        print(f"VERIFY: {'OK' if (not diffs and len(got) >= FLASH_SIZE) else f'{diffs} diffs'}")
    print("\nDONE (full)." + PROVISION_HELP)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM7")
    ap.add_argument("--image", default=str(DEFAULT_IMAGE), help="our xr_system.img")
    ap.add_argument(
        "--phoenixmc",
        action="store_true",
        help="fallback: PhoenixMC catches BROM, then hand off to flash_slot (Windows-only)",
    )
    ap.add_argument(
        "--full",
        metavar="OEM_DUMP",
        help="fallback: composed full-flash via PhoenixMC (Windows-only; needs the OEM dump)",
    )
    args = ap.parse_args()
    image = pathlib.Path(args.image)
    if args.full:
        return _full(pathlib.Path(args.full), image, args.port)
    if args.phoenixmc:
        return _surgical(image, args.port)
    return _catch_python(image, args.port)  # default: fully pure-Python, no vendor tool


if __name__ == "__main__":
    sys.exit(main())
