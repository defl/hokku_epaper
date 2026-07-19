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
# NOTE: `[ -f ... ] && cmd` alone breaks under this script's `set -e` when
# the test is false (a bare failing test aborts the whole heredoc) — use
# the same `cmd || true` pattern as hokku-server above instead. Confirmed
# the hard way: an earlier version of this line silently killed the whole
# build with no error output, because /etc/systemd/system/ only holds
# override/enable symlinks, not the original unit file (that's in
# /lib/systemd/system/), so the -f test was always false here.
rm -f /etc/ssh/sshd_config.d/rename_user.conf /etc/profile.d/userconfig.sh 2>/dev/null
systemctl disable userconfig.service 2>/dev/null || true
EOF
