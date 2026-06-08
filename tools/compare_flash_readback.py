"""Compare flash readback against xr_system.img to verify the flash write.

Usage:
    python tools/compare_flash_readback.py [readback.bin]

If no argument given, looks for flash_A_0x0_L_0x400000.bin in the PhoenixMC directory.
"""

import pathlib
import sys

PHOENIXMC_DIR = (
    pathlib.Path(__file__).parents[1] / ".private/screens/bigme_f7/tools/phoenixmc_v3.1.240901a"
)
IMAGE_PATH = pathlib.Path(__file__).parents[1] / "firmware_bigme_f7/image/xr872/xr_system.img"

if len(sys.argv) > 1:
    readback = pathlib.Path(sys.argv[1])
else:
    readback = PHOENIXMC_DIR / "flash_A_0x0_L_0x400000.bin"

if not IMAGE_PATH.exists():
    print(f"ERROR: image not found: {IMAGE_PATH}")
    sys.exit(1)
if not readback.exists():
    print(f"ERROR: readback not found: {readback}")
    sys.exit(1)

img = IMAGE_PATH.read_bytes()
dump = readback.read_bytes()

img_size = len(img)
print(f"Image:    {IMAGE_PATH}  ({img_size:,} bytes)")
print(f"Readback: {readback}  ({len(dump):,} bytes)")

if len(dump) < img_size:
    print(f"ERROR: readback is shorter than image ({len(dump):,} < {img_size:,})")
    sys.exit(1)

flash_region = dump[:img_size]

if flash_region == img:
    print(f"\nOK: flash matches image perfectly (first {img_size:,} bytes identical)")
    sys.exit(0)

# Find first difference
diffs = []
for i in range(img_size):
    if flash_region[i] != img[i]:
        diffs.append(i)
        if len(diffs) >= 20:
            break

print(f"\nMISMATCH: {len(diffs)}+ differences found")
print("First differences (offset, flash_byte, image_byte):")
for off in diffs:
    print(f"  0x{off:08X} ({off:,}):  flash=0x{flash_region[off]:02X}  img=0x{img[off]:02X}")
if len(diffs) == 20:
    print("  ... (showing first 20)")
sys.exit(1)
