#!/bin/bash
# Build the Bigme F7 (XR872) firmware image and collect it into firmware/release/.
#
# Runs INSIDE the hokku-bigme-f7 builder container (GCC ARM + 32-bit libs), with
# the xr872_sdk mounted at /xr872_sdk and CWD = firmware/bigme_f7. Mirrors the
# ESP32 ci-build.sh. See docs/screens/bigme_f7/firmware_build.md.
set -e

: "${XR872_SDK:=/xr872_sdk}"
: "${CC_DIR:=/usr/bin}"
chmod +x "$XR872_SDK/tools/mkimage" 2>/dev/null || true

# Version is the build input (main.c); the release filename carries it.
VERSION=$(grep -oP '#define\s+FIRMWARE_VERSION\s+"\K[^"]+' main.c)
if [ -z "$VERSION" ]; then
    echo "ERROR: could not read FIRMWARE_VERSION from firmware/bigme_f7/main.c"
    exit 1
fi
echo "Version: $VERSION"

( cd gcc && make image XR872_SDK="$XR872_SDK" CC_DIR="$CC_DIR" )

# All variants' release binaries collect in the shared firmware/release/ dir,
# named hokku-<vendor>_<model>-<version>.<ext>. The F7 blob is an AWIH image (.img).
mkdir -p ../release
cp image/xr872/xr_system.img "../release/hokku-bigme_f7-${VERSION}.img"
echo "Image: firmware/release/hokku-bigme_f7-${VERSION}.img"
