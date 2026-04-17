#!/bin/bash
# Build the Hokku server Debian package.
# Run from the webserver/ directory.
set -e

cd "$(dirname "$0")"
echo "Building hokku-server Debian package..."
dpkg-buildpackage -us -uc -b
echo "Done. Package is in parent directory."
ls -la ../hokku-server_*.deb 2>/dev/null || echo "Warning: .deb file not found"
