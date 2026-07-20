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

# Leave hokku-server ENABLED (its postinst-created WantedBy=multi-user.target
# symlink). Its own unit file already gates startup timing via
# ConditionPathExists=|/var/lib/hokku-installer/setup_complete (an OR against
# ConditionPathExists=|!/var/lib/hokku-installer for plain apt installs) — a
# Condition only skips an *attempted* start, so if this unit were disabled
# here instead, nothing would ever pull it into multi-user.target again after
# the wizard finishes (confirmed live: the setup wizard's _apply_settings()/
# mark_setup_complete() never calls `systemctl enable hokku-server`, only
# touches the sentinel file the Condition checks). An earlier version of this
# script *did* disable it here to stop it starting before setup, which
# silently broke hokku-server permanently post-setup on real hardware.

# Set a placeholder WiFi regulatory domain at image-build time. Without
# ANY country set, the radio comes up rfkill soft-blocked — confirmed live
# on real hardware — which means the setup AP (ap_manager.start_ap()) can
# never come up, and hokku-installer crash-loops retrying it forever. A
# real country is only ever set later, by the wizard itself
# (system_config.set_wifi_country(), installer/hokku/installer/flask_app.py)
# once the user submits the setup form — but that form can only be reached
# THROUGH the setup AP, which needs the radio unblocked FIRST. This breaks
# that chicken-and-egg deadlock; the wizard overwrites it with the user's
# real country as soon as setup completes, same as the original live-only
# fix this session applied by hand to the very first debugging session
# (there, the appliance instead had a stock, previously-configured Pi OS
# image with a real value already present; this bakes an equivalent
# default into the image itself so a *never-configured* Pi doesn't deadlock).
# Not set via pi-gen's own config-level WPA_COUNTRY (see os/pi/config's
# comment — an empty value there hits a pi-gen conditional bug); raspi-config
# directly instead, matching exactly what the wizard itself calls.
raspi-config nonint do_wifi_country US 2>/dev/null || true

rm -f /etc/ssh/sshd_config.d/rename_user.conf /etc/profile.d/userconfig.sh 2>/dev/null
systemctl mask userconfig.service 2>/dev/null || true
systemctl enable getty@tty1.service 2>/dev/null || true
EOF
