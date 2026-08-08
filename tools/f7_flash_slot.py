#!/usr/bin/env python3
"""Flash an A/B candidate into a slot, entering BROM via the `upgrade` console path.

Reuses the PROVEN-RELIABLE BROM entry from _dump_bigme_f7.enter_brom(): find the
CH340 by VID/PID, send `upgrade\\n` to the running firmware's console (the OEM boot
console accepts it -> SYS_UPDATE flag -> watchdog-reset into BROM), then BROM-sync;
retry in a persistent loop (survives re-enumeration and sleep). Once synced it drives
the same SAFE write as the catch flasher (bootloader + the other slot preserved;
cfg flip is the last write; read-back verified).

**Prefer this over the 0x55 power-cycle catch.** It does not depend on catching a
sub-second mask-BROM window, and — unlike the catch — it needs no USB replug. It
also has nothing to do with the `ota` command, so the firmware's "OTA busy" refresh
lock cannot block it, and it never touches the network.

Slot choice is a SAFETY decision: write the slot the device is NOT running from.
`--slot 1` is the update case for a unit running Hokku firmware from slot 0.
flash_slot reads the A/B cfg first and refuses the active slot on its own.

Usage:
  python tools/f7_flash_slot.py <image.img> [--slot 0|1] [--reboot]
  -> if the device is asleep, LONG-PRESS the power button to boot it; the console
     comes up for a few seconds and entry is automatic from there. Repeat if the
     first window is missed. Ctrl-C to abort.
"""

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "python"))

from _dump_bigme_f7 import enter_brom
from hokku.common.xr872.slots import flash_slot


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument(
        "--slot",
        type=int,
        default=0,
        choices=(0, 1),
        help="target slot; refuses the currently-active slot unless --allow-active-slot",
    )
    ap.add_argument(
        "--allow-active-slot",
        action="store_true",
        help="permit overwriting the active slot (bootstrap over a stock/OEM image only)",
    )
    ap.add_argument("--reboot", action="store_true", help="sys_reboot after flashing")
    ap.add_argument("--timeout", type=float, default=300.0, help="BROM-entry deadline (s)")
    args = ap.parse_args()

    img = open(args.image, "rb").read()
    print(f"upgrade-entry flash SLOT {args.slot} <- {args.image} ({len(img)} B)")

    result = enter_brom(overall_timeout=args.timeout)
    if result is None:
        print("ABORT: timed out entering BROM (keep the device awake / press the button).")
        return 1
    f, port = result
    print(f"BROM ready on {port}. Running safe slot-{args.slot} write...")

    try:
        # flash_slot exits(1) via die() on any safety-check failure — including
        # targeting the active slot — leaving the other slot bootable. No brick.
        flash_slot(
            f,
            img,
            slot=args.slot,
            reboot=args.reboot,
            allow_active_slot=args.allow_active_slot,
        )
    finally:
        f.close()
    print(f"\nDONE. Long-press to cold-boot into slot {args.slot}, then watch UART.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
