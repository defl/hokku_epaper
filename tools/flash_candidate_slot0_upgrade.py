#!/usr/bin/env python3
"""Flash an A/B candidate into SLOT 0, entering BROM via the `upgrade` console path.

Reuses the PROVEN-RELIABLE BROM entry from _dump_bigme_f7.enter_brom(): find the
CH340 by VID/PID, send `upgrade\\n` to the running firmware's console (the OEM boot
console accepts it -> SYS_UPDATE flag -> watchdog-reset into BROM), then BROM-sync;
retry in a persistent loop (survives re-enumeration and sleep). Once synced it drives
the exact same SAFE slot-0 write as flash_candidate_slot0.py (bootloader + slot 1
preserved; cfg flip is the last write).

Use this instead of the 0x55 power-cycle catch: it does not depend on catching a
sub-second mask-BROM window across a full CH340 power-cycle.

Usage:
  python tools/flash_candidate_slot0_upgrade.py <image.img> [--reboot]
  -> if the device is asleep, press the button once to wake it; entry is automatic.
  Ctrl-C to abort.
"""

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "python"))

from _dump_bigme_f7 import enter_brom
from hokku.common.xr872.slot0 import flash_slot0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--reboot", action="store_true", help="sys_reboot after flashing")
    ap.add_argument("--timeout", type=float, default=300.0, help="BROM-entry deadline (s)")
    args = ap.parse_args()

    img = open(args.image, "rb").read()
    print(f"upgrade-entry flash SLOT 0 <- {args.image} ({len(img)} B)")

    result = enter_brom(overall_timeout=args.timeout)
    if result is None:
        print("ABORT: timed out entering BROM (keep the device awake / press the button).")
        return 1
    f, port = result
    print(f"BROM ready on {port}. Running safe slot-0 write...")

    try:
        # flash_slot0 exits(1) via die() on any safety-check failure, leaving
        # slot 1 (OEM) bootable — no brick.
        flash_slot0(f, img, reboot=args.reboot)
    finally:
        f.close()
    print("\nDONE. Long-press to cold-boot into slot 0, then watch UART.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
