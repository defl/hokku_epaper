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
    --workdir /workspace/webserver \
    debian:bookworm \
    bash -c "
        set -e
        apt-get update -qq
        apt-get install -y --no-install-recommends \
            build-essential debhelper dh-python python3 python3-setuptools pybuild-plugin-pyproject
        chmod a-x debian/install debian/control debian/changelog debian/hokku-server.service
        dpkg-buildpackage -us -uc -b
    "

# dpkg-buildpackage drops artifacts one level above webserver/ (= /workspace = repo root).
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
