#!/bin/bash
# Build the hokku-server Debian package inside a Debian Bookworm Docker container.
# Run from anywhere in the repo; artifacts land in <repo-root>/build/.
set -e

cd "$(dirname "$0")"
REPO_ROOT="$(cd .. && pwd)"
BUILDS_DIR="$REPO_ROOT/build"

echo "Building hokku-server Debian package via Docker..."

docker run --rm \
    --volume "$REPO_ROOT:/workspace" \
    --workdir /workspace/python \
    debian:bookworm \
    bash -c "
        set -e
        apt-get update -qq
        apt-get install -y --no-install-recommends \
            build-essential debhelper dh-python python3 python3-setuptools pybuild-plugin-pyproject
        cp -a /workspace /build
        cd /build/python
        chmod a-x debian/install debian/control debian/changelog debian/hokku-server.service
        # Stage the bundled screen firmware so debian/install can ship it.
        rm -rf firmware/release
        mkdir -p firmware/release
        cp ../firmware/release/hokku-* firmware/release/
        # Stage the default placeholder image the same way.
        rm -rf default_image
        mkdir -p default_image
        cp ../images/logo/logo_alt_white.png default_image/
        dpkg-buildpackage -us -uc -b
        cp /build/hokku-server_*.deb /build/hokku-server_*.buildinfo /build/hokku-server_*.changes /workspace/ 2>/dev/null || true
    "

# dpkg-buildpackage drops artifacts one level above python/ (= /workspace = repo root).
mkdir -p "$BUILDS_DIR"
moved=0
for f in "$REPO_ROOT"/hokku-server_*.deb \
         "$REPO_ROOT"/hokku-server_*.buildinfo \
         "$REPO_ROOT"/hokku-server_*.changes; do
    [ -f "$f" ] || continue
    mv "$f" "$BUILDS_DIR/"
    moved=1
done

if [ "$moved" -eq 0 ]; then
    echo "Error: dpkg-buildpackage produced no artifacts."
    exit 1
fi

echo "Done. Artifacts in $BUILDS_DIR/:"
ls -la "$BUILDS_DIR"/hokku-server_*.deb
