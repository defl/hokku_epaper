#!/bin/bash -e
# Stage 1: Pre-install pip wheels and the hokku .deb packages.
#
# Wheels are installed BEFORE dpkg so that postinst's "if ! python3 -c 'import X'"
# guards see the packages as already present and skip their pip installs — meaning
# first boot requires no internet access.

source /common.sh

WHEELS_SRC="${STAGE_DIR}/files/wheels"
DEBS_SRC="${STAGE_DIR}/files"

# Copy .debs and wheel directory into rootfs so on_chroot can reach them.
mkdir -p "${ROOTFS_DIR}/tmp/hokku-stage"
cp "${DEBS_SRC}"/hokku-installer_*.deb "${ROOTFS_DIR}/tmp/hokku-stage/"
cp "${DEBS_SRC}"/hokku-server_*.deb    "${ROOTFS_DIR}/tmp/hokku-stage/"
if [ -d "${WHEELS_SRC}" ] && [ "$(ls -A "${WHEELS_SRC}")" ]; then
    cp -r "${WHEELS_SRC}" "${ROOTFS_DIR}/tmp/hokku-stage/wheels"
fi

on_chroot << 'EOF'
set -e

STAGE="/tmp/hokku-stage"

# Install pre-downloaded pip wheels (heavy packages that postinst would otherwise
# download on first boot). Continue on failure — postinst will retry for any that
# are missing.
if [ -d "${STAGE}/wheels" ] && ls "${STAGE}/wheels"/*.whl >/dev/null 2>&1; then
    echo "[hokku-stage] Installing pre-baked pip wheels..."
    pip3 install --break-system-packages --no-index \
        --find-links "${STAGE}/wheels" \
        "${STAGE}/wheels/"*.whl || echo "[hokku-stage] Some wheels failed — postinst will retry"
else
    echo "[hokku-stage] No wheels directory found, skipping pre-install"
fi

# Install hokku-installer first (no heavy deps, runs on first boot).
echo "[hokku-stage] Installing hokku-installer..."
dpkg -i --force-depends "${STAGE}"/hokku-installer_*.deb

# Install hokku-server. The postinst pip guards will skip installs for packages
# we already installed above.
echo "[hokku-stage] Installing hokku-server..."
dpkg -i --force-depends "${STAGE}"/hokku-server_*.deb

# Satisfy any missing Debian dependencies the --force-depends skipped.
apt-get install -f -y
EOF

# Clean up the staged files from the rootfs.
rm -rf "${ROOTFS_DIR}/tmp/hokku-stage"
