"""Flash Hokku custom firmware to Bigme F7 via PhoenixMC.

Usage:
    python tools/xr872at_bigme_f7_flasher.py

Device must be in BROM mode (PhoenixMC will prompt if not connected).
After flashing, provision WiFi via UART:
    net sta config <ssid> <password>
    net sta enable
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _private import res
from bigme_f7_restore_and_verify import (
    _flash_image,
    already_connected_dialog,
    phase_launch,
    phase_wait_brom,
)

sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]

ROOT = pathlib.Path(__file__).parents[1]
PHOENIXMC_DIR = res("bigme_flash_tool_dir")
OUR_IMAGE = ROOT / "firmware/bigme_f7/image/xr872/xr_system.img"


def main():
    if not OUR_IMAGE.exists():
        print(f"ERROR: firmware image not found: {OUR_IMAGE}")
        sys.exit(1)
    if not res("bigme_flash_tool_exe").exists():
        print(f"ERROR: flash tool not found in {PHOENIXMC_DIR}")
        sys.exit(1)

    print("Hokku Bigme F7 — flash custom firmware via PhoenixMC")
    print(f"Image: {OUR_IMAGE}  ({OUR_IMAGE.stat().st_size:,} bytes)")
    print()

    dlg = already_connected_dialog()
    if dlg:
        print("PhoenixMC already connected.")
    else:
        dlg = phase_launch()
        phase_wait_brom(dlg)

    _flash_image(dlg, OUR_IMAGE, "hokku", erase=True)
    print()
    print("Flash complete. Power-cycle the device, then provision WiFi via UART:")
    print("  net sta config <ssid> <password>")
    print("  net sta enable")


if __name__ == "__main__":
    main()
