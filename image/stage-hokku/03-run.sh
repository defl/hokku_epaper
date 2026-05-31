#!/bin/bash -e
# Stage 3: Configure systemd for first-boot installer flow.

source /common.sh

on_chroot << 'EOF'
set -e

# Enable installer services (both run on first boot post-setup).
systemctl enable hokku-installer
systemctl enable hokku-wifi-watchdog

# hokku-server must NOT start until installer is done.
# The ConditionPathExists guards in the service file prevent it, but
# disabling ensures it stays off even if something starts it manually.
systemctl disable hokku-server || true
EOF
