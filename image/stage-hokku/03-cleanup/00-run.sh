#!/bin/bash -e
# Remove build-only configs before image export.
rm -f "${ROOTFS_DIR}/etc/apt/apt.conf.d/00-build-insecure"
echo "[stage-hokku/03-cleanup] Removed build-only insecure apt config"
