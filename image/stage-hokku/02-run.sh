#!/bin/bash -e
# Stage 2: Pi Zero 2 W optimisations.

source /common.sh

# GPU memory: minimum (16 MB) — server is headless.
# Pi OS Bookworm config is at /boot/firmware/config.txt.
for cfg in \
    "${ROOTFS_DIR}/boot/firmware/config.txt" \
    "${ROOTFS_DIR}/boot/config.txt"; do
    if [ -f "$cfg" ]; then
        echo "gpu_mem=16" >> "$cfg"
        break
    fi
done

on_chroot << 'EOF'
set -e

# Disable swap — Pi Zero 2 W has only 512 MB RAM, and swap on SD cards
# is slow and wears the card. The server is designed to run without swap.
if systemctl is-enabled dphys-swapfile >/dev/null 2>&1; then
    systemctl disable dphys-swapfile
fi
if [ -f /etc/dphys-swapfile ]; then
    sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=0/' /etc/dphys-swapfile
fi

# Disable avahi-daemon — hokku-server's Python zeroconf handles mDNS itself
# and conflicts with avahi on UDP 5353. The installer doesn't use avahi either.
if systemctl is-enabled avahi-daemon >/dev/null 2>&1; then
    systemctl disable avahi-daemon
fi
EOF
