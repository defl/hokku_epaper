#!/bin/bash -e
# Pi Zero 2 W optimisations.

# GPU memory: minimum (16 MB) — server is headless. dwc2/dr_mode=peripheral
# enables the USB gadget serial console (see below); disable-bt turns off the
# unused Bluetooth radio (smaller attack surface, one less background daemon
# — see docs/os_pi_usb_console.md for the console, and the hardening notes
# in this stage for the rest).
for cfg in \
    "${ROOTFS_DIR}/boot/firmware/config.txt" \
    "${ROOTFS_DIR}/boot/config.txt"; do
    if [ -f "$cfg" ]; then
        echo "gpu_mem=16" >> "$cfg"
        echo "dtoverlay=dwc2,dr_mode=peripheral" >> "$cfg"
        echo "dtoverlay=disable-bt" >> "$cfg"
        break
    fi
done

# USB gadget serial console: load dwc2 + g_serial at boot so /dev/ttyGS0
# exists. Idempotent — strip any pre-existing modules-load= token first.
# See docs/os_pi_usb_console.md for the full mechanism and the traps to
# avoid (serial-getty@ vs getty@, host-side DTR gotchas).
for cmdline in \
    "${ROOTFS_DIR}/boot/firmware/cmdline.txt" \
    "${ROOTFS_DIR}/boot/cmdline.txt"; do
    if [ -f "$cmdline" ]; then
        line="$(cat "$cmdline")"
        line="$(echo "$line" | sed -E 's/(^| )modules-load=[^ ]*//')"
        echo "${line} modules-load=dwc2,g_serial" > "$cmdline"
        break
    fi
done

# journald: RAM-only, capped. Appliances run 24/7 on a consumer SD card —
# a persistent disk-backed journal is continuous write wear for no real
# benefit here (journalctl still works fine against the live instance;
# this just means log history doesn't survive a reboot).
mkdir -p "${ROOTFS_DIR}/etc/systemd/journald.conf.d"
cat > "${ROOTFS_DIR}/etc/systemd/journald.conf.d/hokku.conf" <<'JOURNALEOF'
[Journal]
Storage=volatile
RuntimeMaxUse=32M
JOURNALEOF

# unattended-upgrades: keep it (this appliance has no other patching
# mechanism), but restrict it to security-only updates on a weekly cadence
# instead of the Debian default (daily list updates, all-origins upgrades)
# — cuts disk writes and network chatter while still closing CVEs.
mkdir -p "${ROOTFS_DIR}/etc/apt/apt.conf.d"
cat > "${ROOTFS_DIR}/etc/apt/apt.conf.d/52hokku-security-only" <<'APTEOF'
Unattended-Upgrade::Origins-Pattern {
    "origin=Debian,codename=${distro_codename},label=Debian-Security";
};
APTEOF
cat > "${ROOTFS_DIR}/etc/apt/apt.conf.d/20auto-upgrades" <<'APTEOF'
APT::Periodic::Update-Package-Lists "7";
APT::Periodic::Unattended-Upgrade "7";
APTEOF

# cloud-init: disable entirely. pi-gen's stage2/04-cloud-init stage installs
# and enables it by default (aimed at generic cloud/NoCloud provisioning),
# but this appliance has its own provisioning mechanism (hokku-installer's
# captive-portal wizard) and no cloud provider — cloud-init has nothing to
# do here. Left enabled, it spends ~120s every boot retrying an OpenStack/
# EC2-style metadata service that doesn't exist (169.254.169.254), visibly
# delaying the setup AP coming up. Confirmed live, twice, on real hardware.
# /etc/cloud/cloud-init.disabled is the official, documented way to turn it
# off — cloud-init's systemd generator checks for this file and skips
# enabling any of its several units (cloud-init-local, cloud-init,
# cloud-config, cloud-final), which is more robust than disabling each
# unit individually (the generator can re-enable them at boot otherwise).
mkdir -p "${ROOTFS_DIR}/etc/cloud"
touch "${ROOTFS_DIR}/etc/cloud/cloud-init.disabled"

on_chroot << 'EOF'
set -e

if systemctl is-enabled dphys-swapfile >/dev/null 2>&1; then
    systemctl disable dphys-swapfile
fi
if [ -f /etc/dphys-swapfile ]; then
    sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=0/' /etc/dphys-swapfile
fi

if systemctl is-enabled avahi-daemon >/dev/null 2>&1; then
    systemctl disable avahi-daemon
fi

# USB gadget serial console login. Use the generic getty@.service template —
# NOT serial-getty@ttyGS0, which BindsTo=dev-ttyGS0.device and gets torn
# down because the late-loading USB gadget tty never fires a proper udev
# device-active event. (Confirmed the hard way against real hardware —
# see docs/os_pi_usb_console.md.)
systemctl enable getty@ttyGS0

# Bluetooth radio unused by this appliance — disabled at the hardware level
# above (dtoverlay=disable-bt); also stop the services so nothing lingers.
systemctl disable --now bluetooth hciuart 2>/dev/null || true

# Stock periodic services with no purpose on a locked-down, single-function
# appliance — each is a periodic disk write for zero benefit here.
for unit in man-db.timer motd-news.timer motd-news.service; do
    systemctl disable --now "$unit" 2>/dev/null || true
done

# rsyslog would double-log everything journald already captures. Not present
# by default on Bookworm, but disable defensively in case the base image
# lineage changes.
systemctl disable --now rsyslog 2>/dev/null || true

# fstrim.timer is intentionally left enabled — TRIM helps SD-card wear, it's
# not a source of it.
EOF
