#!/bin/bash -e
# Configure systemd for first-boot installer flow.

on_chroot << 'EOF'
set -e

systemctl enable hokku-installer
systemctl enable hokku-wifi-watchdog 2>/dev/null || \
    echo "WARNING: hokku-wifi-watchdog.service missing (check installer .deb packaging)"
systemctl disable hokku-server || true

# Disable raspi-config's stock first-boot "rename user" prompt. It's an
# interactive whiptail dialog needing a keyboard+monitor that will never
# come on a headless appliance — left enabled, it blocks boot forever on
# tty1 (confirmed live: the Pi sat at this dialog indefinitely) AND blocks
# every login shell via the profile.d script (blocked USB-console login
# attempts too, not just tty1 — same script fires on any interactive
# shell). Same fix tools/pi_installer.py's firstrun.sh already applies for
# its own (non-appliance-image) flow, for the identical reason.
rm -f /etc/ssh/sshd_config.d/rename_user.conf /etc/profile.d/userconfig.sh 2>/dev/null
[ -f /etc/systemd/system/userconfig.service ] && systemctl disable userconfig.service
EOF
