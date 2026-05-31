#!/bin/bash -e
# Stage 4: Clean up build-only configs before the image is exported.

# Remove the insecure apt config that was added in stage0/00-aaa-fix-keys
# to allow unauthenticated repos during the pi-gen build. The final image
# must enforce normal GPG signature checking.
rm -f "${ROOTFS_DIR}/etc/apt/apt.conf.d/00-build-insecure"
echo "[stage-hokku/04] Removed build-only insecure apt config"
