#!/bin/bash -e
# Configure systemd for first-boot installer flow.

# Belt-and-suspenders bypass of raspi-config's stock first-boot "rename
# user" dialog (RPi-Distro/userconf-pi). It's interactive (needs a
# keyboard+monitor that will never come on a headless appliance) and
# hijacks whichever tty is active via `chvt` regardless of its nominal
# TTYPath=/dev/tty8 — confirmed live, twice, on real hardware: it blocks
# boot indefinitely and the display shows the dialog over HDMI. A plain
# `systemctl disable userconfig.service` was NOT sufficient by itself on
# an actual build+boot (root cause not fully confirmed — the unit's own
# WantedBy=multi-user.target symlink removal alone didn't stop it from
# running/rendering), so this combines every independent angle the
# upstream source (github.com/RPi-Distro/userconf-pi, pios/trixie branch)
# offers, any one of which should be sufficient alone:
#   1. userconf-service (the script the unit runs) checks
#      /boot/firmware/userconf(.txt) FIRST, before ever going interactive
#      — if it holds a valid "user:password" line, the whole interactive
#      branch is skipped entirely. This is the exact mechanism Raspberry
#      Pi Imager itself uses to avoid this prompt. chpasswd (not -e) is
#      used on it, so the password field is plaintext, matching this
#      appliance's already-documented default credentials.
#   2. mask (not just disable) the unit — stronger than disable, prevents
#      even an explicit `systemctl start` from ever running it.
#   3. cancel-rename (what normally runs when the dialog completes/is
#      skipped) also enables getty@tty1 as its last step — since we never
#      run it, do that ourselves so tty1 still gets a normal login getty.
mkdir -p "${ROOTFS_DIR}/boot/firmware"
echo "hokku:hokku" > "${ROOTFS_DIR}/boot/firmware/userconf.txt"

on_chroot << 'EOF'
set -e

systemctl enable hokku-installer
systemctl enable hokku-wifi-watchdog 2>/dev/null || \
    echo "WARNING: hokku-wifi-watchdog.service missing (check installer .deb packaging)"
systemctl disable hokku-server || true

rm -f /etc/ssh/sshd_config.d/rename_user.conf /etc/profile.d/userconfig.sh 2>/dev/null
systemctl mask userconfig.service 2>/dev/null || true
systemctl enable getty@tty1.service 2>/dev/null || true
EOF
