#!/bin/bash
# Build firmware and produce a merged single-file release binary.
# Run inside an ESP-IDF environment (idf.py + esptool.py must be on PATH).
set -e

idf.py reconfigure build

# Strip CR/whitespace so a CRLF-checked-out VERSION doesn't corrupt the filename.
VERSION=$(tr -d '\r\n' < VERSION)

if [ -z "$VERSION" ]; then
    echo "ERROR: firmware/seeedstudio_e1004/VERSION is empty"
    exit 1
fi
echo "Version: $VERSION"

# All variants' release binaries collect in the shared firmware/release/ dir,
# named hokku-<vendor>_<model>-<version>.<ext> (the filename carries the version).
mkdir -p ../release
esptool.py --chip esp32s3 merge_bin \
    --output ../release/hokku-seeedstudio_e1004-${VERSION}.bin \
    0x0     build/bootloader/bootloader.bin \
    0x8000  build/partition_table/partition-table.bin \
    0x10000 build/hokku_seeedstudio_e1004.bin

echo "Merged: firmware/release/hokku-seeedstudio_e1004-${VERSION}.bin"
