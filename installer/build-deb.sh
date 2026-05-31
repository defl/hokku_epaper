#!/bin/bash
# Build the hokku-installer Debian package.
# Run from the installer/ directory. Artifacts land in <repo-root>/build/.
set -e

cd "$(dirname "$0")"
REPO_ROOT="$(cd .. && pwd)"
BUILDS_DIR="$REPO_ROOT/build"

echo "Building hokku-installer Debian package..."
dpkg-buildpackage -us -uc -b

mkdir -p "$BUILDS_DIR"
shopt -s nullglob
moved=0
for f in "$REPO_ROOT"/hokku-installer_*.deb \
         "$REPO_ROOT"/hokku-installer_*.buildinfo \
         "$REPO_ROOT"/hokku-installer_*.changes; do
    mv "$f" "$BUILDS_DIR/"
    moved=1
done
shopt -u nullglob

if [ "$moved" -eq 0 ]; then
    echo "Warning: dpkg-buildpackage produced no artifacts to move."
    exit 1
fi

echo "Done. Artifacts in $BUILDS_DIR/:"
ls -la "$BUILDS_DIR"/hokku-installer_*.deb 2>/dev/null
