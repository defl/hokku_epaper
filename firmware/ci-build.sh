#!/bin/bash
# Build firmware and produce a merged single-file release binary.
# Run inside an ESP-IDF environment (idf.py + esptool.py must be on PATH).
set -e

idf.py reconfigure build

VERSION=$(python3 -c "
with open('build/hokku_epaper.bin', 'rb') as f:
    f.seek(0x30)
    print(f.read(32).split(b'\x00')[0].decode())
")

if [ -z "$VERSION" ]; then
    echo "ERROR: could not read version from build/hokku_epaper.bin"
    exit 1
fi
echo "Version: $VERSION"

mkdir -p release
esptool.py --chip esp32s3 merge_bin \
    --output release/hokku-firmware_${VERSION}.bin \
    0x0     build/bootloader/bootloader.bin \
    0x8000  build/partition_table/partition-table.bin \
    0x10000 build/hokku_epaper.bin

echo "Merged: release/hokku-firmware_${VERSION}.bin"
