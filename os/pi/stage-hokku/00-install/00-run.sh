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
dpkg -i --force-depends --force-overwrite "${STAGE}"/hokku-installer_*.deb

echo "[hokku-stage] Installing hokku-server..."
dpkg -i --force-depends --force-overwrite "${STAGE}"/hokku-server_*.deb

apt-get install -f -y

# ── Pre-warm the numba dither cache ───────────────────────────────────────
# First use of the dither JIT-compiles its kernel — minutes of pure compile on
# one weak A53 core, during which sshd/hokku-server/mDNS are all starved (this
# is what made every cold first boot look wedged). Compile it HERE instead, in
# the build chroot, into the very cache dir the service reads. numba's on-disk
# cache is keyed by the target CPU, so we pin cortex-a53 (the Pi Zero 2 W) for
# BOTH this warmup and the runtime service (drop-in below) — otherwise the Pi
# cache-misses and re-JITs anyway. Best-effort: a warmup failure must not fail
# the build (first boot just falls back to JIT-on-demand).
mkdir -p /etc/systemd/system/hokku-server.service.d
cat > /etc/systemd/system/hokku-server.service.d/10-numba-cpu.conf <<'DROPIN'
# Pin numba's codegen target so the build-time pre-warmed cache
# (os/pi/stage-hokku/00-install) is valid at runtime instead of re-compiling on
# first boot. The cache is keyed by (cpu_name, cpu_features) — and numba only
# takes cpu_features from NUMBA_CPU_FEATURES if it is SET (even to empty);
# otherwise it host-detects, which differs between the qemu build chroot and the
# real Pi and voids the cache. So pin BOTH to a fixed pair. Empty features →
# cortex-a53's LLVM defaults (incl. NEON); the appliance is always a Pi Zero 2 W.
[Service]
Environment=NUMBA_CPU_NAME=cortex-a53
Environment=NUMBA_CPU_FEATURES=
DROPIN

echo "[hokku-stage] Pre-warming the numba dither cache (cortex-a53)..."
mkdir -p /var/lib/hokku/numba_cache
# timeout: the JIT runs under qemu emulation here, so it's slow — but bounded,
# so a stuck compile can't stall the whole image build.
NUMBA_CACHE_DIR=/var/lib/hokku/numba_cache NUMBA_CPU_NAME=cortex-a53 NUMBA_CPU_FEATURES= \
    timeout 600 python3 - <<'WARM' \
    || echo "[hokku-stage] warmup failed/timed out (non-fatal — first boot will JIT on demand)"
import numpy as np
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_config import DitherConfig
from hokku.webserver.dither_streaming_numba import NumbaStreamingDither

disp = DISPLAY_REGISTRY["huessen_epf1301"]
NumbaStreamingDither(disp).dither(
    np.zeros((16, 16, 3), dtype=np.float32),
    DitherConfig(algorithm="floyd_steinberg", lut_name="euclidean",
                 serpentine=True, hue_cutoff_deg=8.0, neutral_chroma=8.0),
)
print("[hokku-stage] dither kernel compiled + cached")
WARM
# The service runs as user 'hokku'; hand it the pre-warmed cache.
chown -R hokku:hokku /var/lib/hokku/numba_cache 2>/dev/null || true
EOF

rm -rf "${ROOTFS_DIR}/tmp/hokku-stage"
