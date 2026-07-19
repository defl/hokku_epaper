#!/bin/bash
# Download arm64 pip wheels for pre-baking into the Pi image.
# Run on the CI host (x86_64) before building the image.
# Outputs wheel files to DEST (default: os/pi/stage-hokku/files/wheels/).
#
# We split downloads into two groups:
#   binary  — packages with C/Rust extensions that need the right arch
#   pure    — pure Python packages; platform tag doesn't matter

set -e

DEST="${1:-"$(cd "$(dirname "$0")" && pwd)/stage-hokku/files/wheels"}"
mkdir -p "$DEST"

echo "Downloading arm64 binary wheels to $DEST ..."
pip download \
    --platform linux_aarch64 \
    --python-version 311 \
    --only-binary :all: \
    --dest "$DEST" \
    "pillow-heif>=0.10" \
    "pillow-avif-plugin>=0.1" \
    "pillow-jxl-plugin>=0.1" \
    "opencv-python-headless>=4.7" \
    "scipy>=1.14" \
    "numba>=0.59" \
    "resvg-py>=0.3" \
    || echo "WARNING: some binary wheels could not be downloaded; postinst will retry them"

echo "Downloading pure-Python wheels to $DEST ..."
pip download \
    --no-deps \
    --dest "$DEST" \
    "colour-science>=0.4" \
    "zeroconf>=0.38" \
    "esptool>=4.7" \
    "esp-idf-nvs-partition-gen" \
    || echo "WARNING: some pure-Python wheels could not be downloaded"

echo "Wheels in $DEST:"
ls -lh "$DEST"/*.whl 2>/dev/null || echo "  (none)"
