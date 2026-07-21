#!/usr/bin/env python3
"""Flash an A/B candidate into SLOT 0 by catching the BROM window on a power-cycle.

Same SAFE slot-0 write as flash_candidate_slot0.py (bootloader + slot 1 preserved,
cfg flip is the last write), but a different way IN: instead of the `upgrade` console
command (which needs custom firmware already running), this keeps a tight 0x55
mask-BROM sync blast running across CH340 re-enumerations. You LONG-PRESS the device
power button to power-cycle it; the BROM window opens in the ~1 s re-enumeration
blackout and this catches its tail. Retry the long-press until it lands.

Use this when no custom firmware boots (e.g. slot 0 holds a broken candidate and
slot 1 holds the OEM, which does not answer `upgrade`). If the catch never lands,
the device simply boots the OEM again — no brick (slot 1 is always intact).

Usage:
  python tools/flash_candidate_slot0_catch.py <image.img> [--port COM7]
  -> then LONG-PRESS the power button, repeatedly (~every 8 s) until it syncs.
  Ctrl-C to abort.
"""

import argparse
import sys
import time

import serial

from flash_candidate_slot0 import flash_slot0
from xr872_flasher import SYNC_BYTE, SYNC_OK, XR872Flasher


def ts() -> str:
    return time.strftime("%H:%M:%S")


def open_stable(port: str, baud: int = 115200) -> serial.Serial:
    """Open the port without toggling DTR/RTS (no reset pulse)."""
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = 0.02
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def hammer_sync(ser: serial.Serial, duration: float) -> bool:
    """Send clean single 0x55 mask-BROM sync bytes; return True on 'OK'."""
    ser.timeout = 0.004
    ser.reset_input_buffer()
    deadline = time.monotonic() + duration
    rx = bytearray()
    while time.monotonic() < deadline:
        ser.write(bytes([SYNC_BYTE]))
        ser.flush()
        r = ser.read(8)
        if r:
            rx.extend(r)
            if SYNC_OK in rx:
                print(f"  BROM answered: {bytes(rx[-16:])!r}")
                return True
            if len(rx) > 64:
                del rx[:-16]
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--port", default="COM7")
    ap.add_argument("--reboot", action="store_true", help="sys_reboot after flashing")
    args = ap.parse_args()

    img = open(args.image, "rb").read()
    print(f"catch-flash SLOT 0 <- {args.image} ({len(img)} B) on {args.port}")
    print(">>> LONG-PRESS the device power button now, and repeat every ~8 s until it syncs. <<<")
    print("    (Ctrl-C to abort)\n")

    last_status = 0.0
    while True:
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
                    print(f"\n[{ts()}] *** BROM SYNC — establishing command channel ***")
                    f = XR872Flasher(ser=ser, verbose=False)
                    if not f.sync(attempts=30, timeout_per=0.1):
                        print("  command sync failed after trigger — keep long-pressing")
                        continue
                    # Hand the synced handle to the validated safe slot-0 writer.
                    # flash_slot0 calls sys.exit(1) via die() on any safety failure,
                    # which leaves slot 1 (OEM) bootable — no brick.
                    flash_slot0(f, img, reboot=args.reboot)
                    print(f"\n[{ts()}] DONE. Power-cycle / watch UART to see it boot.")
                    return 0
            finally:
                try:
                    ser.close()
                except Exception:
                    pass

        except serial.SerialException:
            time.sleep(0.02)  # power-cycle bounce — reconnect fast
        except KeyboardInterrupt:
            print("\nAborted.")
            return 130


if __name__ == "__main__":
    sys.exit(main())
