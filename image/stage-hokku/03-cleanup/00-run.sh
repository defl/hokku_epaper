#!/bin/bash -e
# Keep the insecure apt config in the final image — export-image/02-set-sources
# runs apt-get update inside the image and needs it (Debian Bookworm signing keys
# are not properly seeded in the bootstrapped rootfs). Acceptable on a closed
# Pi appliance; fix in a future release.
echo "[stage-hokku/03-cleanup] Keeping insecure apt config for export-image compatibility"
