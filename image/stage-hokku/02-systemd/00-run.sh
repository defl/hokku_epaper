#!/bin/bash -e
# Configure systemd for first-boot installer flow.

on_chroot << 'EOF'
set -e

systemctl enable hokku-installer
systemctl enable hokku-wifi-watchdog
systemctl disable hokku-server || true
EOF
