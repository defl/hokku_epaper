#!/bin/bash -e
# Install pre-baked pip wheels and the hokku .deb packages.
#
# Wheels are installed BEFORE dpkg so that postinst's "if ! python3 -c 'import X'"
# guards see the packages as already present and skip their pip installs — meaning
# first boot requires no internet access.

# pi-gen cd's into the script's directory before running it, so relative paths work.
WHEELS_SRC="files/wheels"
DEBS_SRC="files"

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

if [ -d "${STAGE}/wheels" ] && ls "${STAGE}/wheels"/*.whl >/dev/null 2>&1; then
    echo "[hokku-stage] Installing pre-baked pip wheels..."
    pip3 install --break-system-packages --no-index \
        --find-links "${STAGE}/wheels" \
        "${STAGE}/wheels/"*.whl || echo "[hokku-stage] Some wheels failed — postinst will retry"
else
    echo "[hokku-stage] No wheels directory found, skipping pre-install"
fi

echo "[hokku-stage] Installing hokku-installer..."
dpkg -i --force-depends "${STAGE}"/hokku-installer_*.deb

echo "[hokku-stage] Installing hokku-server..."
dpkg -i --force-depends "${STAGE}"/hokku-server_*.deb

apt-get install -f -y
EOF

rm -rf "${ROOTFS_DIR}/tmp/hokku-stage"
