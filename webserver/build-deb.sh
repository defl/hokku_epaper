#!/bin/bash
# Build the Hokku server Debian package.
# Run from the webserver/ directory. The finished .deb (plus .buildinfo /
# .changes) lands in <repo-root>/build/.
set -e

cd "$(dirname "$0")"
WEBSERVER_DIR="$(pwd)"
REPO_ROOT="$(cd .. && pwd)"
BUILDS_DIR="$REPO_ROOT/build"

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
