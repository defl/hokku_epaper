#!/bin/bash -e
# Configure systemd for first-boot installer flow.

on_chroot << 'EOF'
set -e

systemctl enable hokku-installer
systemctl enable hokku-wifi-watchdog 2>/dev/null || \
    echo "WARNING: hokku-wifi-watchdog.service missing (check installer .deb packaging)"
systemctl disable hokku-server || true
EOF
