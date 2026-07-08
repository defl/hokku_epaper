#!/usr/bin/env python3
"""Non-destructive test: can we catch the XR872 mask-BROM on a STOCK unit via the
0x55 power-cycle catch (the only entry when the firmware doesn't answer `upgrade`)?

Long-press the power button to power-cycle the device; this hammers 0x55 across the
CH340 re-enumeration blackout to catch the mask-BROM sync window. On success it
confirms with GetFlashId and immediately sys_reboots back out — nothing is written,
the unit stays stock. This settles whether pure-Python fresh-unit flashing is viable.

Usage:
  python tools/_catch_test.py [--port COM7]
  -> LONG-PRESS the power button, repeat every ~8 s until it syncs (Ctrl-C to stop).
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import time

import serial

from flash_candidate_slot0_catch import hammer_sync, open_stable
from xr872_flasher import XR872Flasher


def ts() -> str:
    return time.strftime("%H:%M:%S")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM7")
    ap.add_argument("--seconds", type=int, default=120, help="give up after N seconds")
    args = ap.parse_args()

    print(f"NON-DESTRUCTIVE BROM catch test on {args.port}")
    print(">>> LONG-PRESS the power button now, and repeat every ~8 s until it syncs. <<<")
    print("    (Ctrl-C to abort; nothing is written either way)\n")

    deadline = time.monotonic() + args.seconds
    last_status = 0.0
    while time.monotonic() < deadline:
        try:
            try:
                ser = open_stable(args.port)
            except (serial.SerialException, OSError):
                if time.monotonic() - last_status > 3:
                    print(f"[{ts()}] waiting for port... LONG-PRESS the button")
                    last_status = time.monotonic()
                continue
            try:
                if time.monotonic() - last_status > 3:
                    print(f"[{ts()}] hammering 0x55 — keep long-pressing")
                    last_status = time.monotonic()
                if hammer_sync(ser, duration=2.0):
                    print(f"\n[{ts()}] *** BROM SYNC CAUGHT — establishing command channel ***")
                    f = XR872Flasher(ser=ser, verbose=False)
                    if not f.sync(attempts=30, timeout_per=0.1):
                        print("  command sync failed after trigger — keep long-pressing")
                        continue
                    fid = f.get_flash_id()
                    print(f"  GetFlashId -> {fid}")
                    print("\n  ===> CATCH WORKS on this stock unit. Rebooting back out (no write).")
                    f.sys_reboot()
                    return 0
            finally:
                with contextlib.suppress(Exception):
                    ser.close()
        except serial.SerialException:
            time.sleep(0.02)
        except KeyboardInterrupt:
            print("\nAborted.")
            return 130

    print(f"\n[{ts()}] gave up after {args.seconds}s — no BROM catch.")
    print("  ===> The 0x55 catch did NOT work on this stock unit over the CH340.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
