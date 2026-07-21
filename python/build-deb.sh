#!/bin/bash
# Build the Hokku server Debian package.
# Run from the python/ directory. The finished .deb (plus .buildinfo /
# .changes) lands in <repo-root>/build/.
set -e

cd "$(dirname "$0")"
PKG_DIR="$(pwd)"
REPO_ROOT="$(cd .. && pwd)"
BUILDS_DIR="$REPO_ROOT/build"

# Stage the bundled screen firmware into the source tree so debian/install can
# ship it to /usr/share/hokku-server/firmware/. The "Flash a screen" feature
# needs this merged image. Fail loudly if it is missing.
FW_SRC_DIR="$REPO_ROOT/firmware/release"
if ! ls "$FW_SRC_DIR"/hokku-huessen_epf1301-*.bin >/dev/null 2>&1; then
    echo "ERROR: no firmware/release/hokku-huessen_epf1301-*.bin found to bundle." >&2
    echo "Build the firmware (firmware/huessen_epf1301/build.*) or fetch the release asset first." >&2
    exit 1
fi
# Clean the staging dir so stale versions from a previous build aren't shipped.
rm -rf "$PKG_DIR/firmware/release"
mkdir -p "$PKG_DIR/firmware/release"
# Bundle every built variant: the ESP32 .bin (required) plus any Bigme F7 .img.
cp "$FW_SRC_DIR"/hokku-* "$PKG_DIR/firmware/release/"

# Stage the default placeholder image the same way — debian/install can only
# reference paths inside the source tree (python/), not the repo root.
LOGO_SRC="$REPO_ROOT/images/logo/logo_alt_white.png"
if [ ! -f "$LOGO_SRC" ]; then
    echo "ERROR: $LOGO_SRC not found (default placeholder image)." >&2
    exit 1
fi
rm -rf "$PKG_DIR/default_image"
mkdir -p "$PKG_DIR/default_image"
cp "$LOGO_SRC" "$PKG_DIR/default_image/"

echo "Building hokku-server Debian package..."
dpkg-buildpackage -us -uc -b

# dpkg-buildpackage drops the artifacts one level up from the source dir
# (i.e. directly in the repo root). Sweep them into build/.
mkdir -p "$BUILDS_DIR"
shopt -s nullglob
moved=0
for f in "$REPO_ROOT"/hokku-server_*.deb \
         "$REPO_ROOT"/hokku-server_*.buildinfo \
         "$REPO_ROOT"/hokku-server_*.changes; do
    mv "$f" "$BUILDS_DIR/"
    moved=1
done
shopt -u nullglob

if [ "$moved" -eq 0 ]; then
    echo "Warning: dpkg-buildpackage produced no artifacts to move."
    exit 1
fi

echo "Done. Artifacts in $BUILDS_DIR/:"
ls -la "$BUILDS_DIR"/hokku-server_*.deb 2>/dev/null
