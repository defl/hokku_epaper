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

# --platform must name the tags the wheels are ACTUALLY published under.
# "linux_aarch64" is not one of them: it is the bare-Linux tag almost nobody
# uploads, and pip does not treat it as a wildcard — a manylinux wheel does not
# satisfy it. Asking for it matched nothing at all ("from versions: none") for
# every package below, and because the failure is tolerated (see the || warning)
# the build carried on and produced an image with no pre-baked binary wheels,
# quietly turning "first boot needs no internet" into the opposite. Pass the
# manylinux tags instead; repeated --platform is OR, so a wheel matching any of
# them is accepted, newest ABI first.
echo "Downloading arm64 binary wheels to $DEST ..."
pip download \
    --platform manylinux_2_28_aarch64 \
    --platform manylinux2014_aarch64 \
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
    "zeroconf>=0.38" \
    || echo "WARNING: some binary wheels could not be downloaded; postinst will retry them"

# Genuinely architecture-independent, so no --platform here — which also means
# no --only-binary, since pip only accepts --platform together with it. That
# matters for esptool, which publishes an sdist and no wheel: constraining this
# group would make esptool's failure take colour-science and the nvs generator
# down with it. zeroconf used to live here and no longer belongs — it ships
# compiled wheels now, so it moved to the binary group above, where it gets an
# aarch64 wheel instead of one matching the build host.
echo "Downloading pure-Python wheels to $DEST ..."
pip download \
    --no-deps \
    --dest "$DEST" \
    "colour-science>=0.4" \
    "esptool>=4.7" \
    "esp-idf-nvs-partition-gen" \
    || echo "WARNING: some pure-Python wheels could not be downloaded"

echo "Wheels in $DEST:"
ls -lh "$DEST"/*.whl 2>/dev/null || echo "  (none)"
