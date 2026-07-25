#!/bin/bash -e
# Pi Zero 2 W optimisations.

# GPU memory: minimum (16 MB) — server is headless. disable-bt turns off the
# unused Bluetooth radio (smaller attack surface, one less background daemon).
#
# dwc2/dr_mode=otg makes the Zero 2 W's single USB data port DUAL-ROLE, which
# it must be to serve two jobs on one port:
#   • peripheral role — when plugged into a PC with a normal cable, it is the
#     USB gadget serial console (/dev/ttyGS0, see below and the cmdline stanza).
#   • host role — when a screen is attached through a micro-USB→USB-A OTG
#     adapter, the port becomes a host so "Flash a screen" can drive it
#     (the screen enumerates as /dev/ttyUSB0). Power the Pi from the PWR port
#     so the data port is free to host.
# The role is chosen by the cable/adapter (OTG ID pin), so there is nothing to
# switch in software. dr_mode=peripheral would have locked it to console-only,
# leaving no way to flash a screen from the appliance itself.
# See docs/os_pi_usb_console.md for the console; the hardening notes in this
# stage cover the rest.
for cfg in \
    "${ROOTFS_DIR}/boot/firmware/config.txt" \
    "${ROOTFS_DIR}/boot/config.txt"; do
    if [ -f "$cfg" ]; then
        echo "gpu_mem=16" >> "$cfg"
        echo "dtoverlay=dwc2,dr_mode=otg" >> "$cfg"
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

# First-boot root-filesystem expansion. pi-gen images carry no auto-resize, so
# without this the rootfs stays at its built ~3 GB on any card and fills up
# (which, once full, makes uploads and conversions fail). A oneshot service
# grows the partition + ext4 online on first boot, then stamps itself done.
#
# CRITICAL — it runs LATE and NON-BLOCKING. An earlier version ran
# Before=sysinit.target and hung the boot solid: growpart/partx re-read the
# partition table (BLKRRPART) of the still-settling *mounted* root device, which
# blocked in uninterruptible sleep, and because sysinit.target waited on it the
# appliance never came up. So: default dependencies (runs after the system is
# settled — the same state where the online resize works by hand), and NOTHING
# is ordered after this unit, so even a stuck resize can never stall the boot or
# the setup AP. Each step is timeout-guarded as a further backstop. growpart
# (cloud-guest-utils) exits 1 with "NOCHANGE" when already full; all steps
# tolerate failure and the stamp still lands.
mkdir -p "${ROOTFS_DIR}/usr/local/sbin"
cat > "${ROOTFS_DIR}/usr/local/sbin/hokku-resize-rootfs" <<'RESIZEEOF'
#!/bin/sh
set -e
STAMP=/var/lib/hokku/.rootfs-resized
[ -e "$STAMP" ] && exit 0

root_src=$(findmnt -no SOURCE /) || exit 0
case "$root_src" in
    /dev/mmcblk*p[0-9]*) disk=${root_src%p[0-9]*}; part=${root_src##*p} ;;
    /dev/sd*[0-9])       disk=$(echo "$root_src" | sed -E 's/[0-9]+$//'); part=$(echo "$root_src" | sed -E 's/.*[^0-9]//') ;;
    *) echo "hokku-resize: unrecognised root device '$root_src', skipping" >&2; exit 0 ;;
esac

timeout 60  growpart "$disk" "$part" || true   # exits 1 (NOCHANGE) when already full
timeout 30  partx -u "$disk" || true           # kernel re-reads the grown last partition
timeout 240 resize2fs "$root_src" || true      # online-grow the mounted ext4

mkdir -p /var/lib/hokku
touch "$STAMP"
RESIZEEOF

cat > "${ROOTFS_DIR}/etc/systemd/system/hokku-resize-rootfs.service" <<'RESIZESVC'
[Unit]
Description=Grow the Hokku appliance rootfs to fill the SD card (first boot)
After=local-fs.target
ConditionPathExists=!/var/lib/hokku/.rootfs-resized

[Service]
Type=oneshot
RemainAfterExit=yes
TimeoutStartSec=360
ExecStart=/usr/local/sbin/hokku-resize-rootfs

[Install]
WantedBy=multi-user.target
RESIZESVC

on_chroot << 'EOF'
set -e

# ── Memory / swap policy ──────────────────────────────────────────────
# This appliance runs on a 512 MB Pi Zero 2 W (~460 MB usable, ~140 MB free
# at idle) and decodes + dithers photos, so it lives near the memory ceiling.
# Three layers keep it from tipping over, in order of use:
#
#   1. zram — compressed RAM-backed swap, the PRIMARY overflow. Anonymous
#      pages compress ~3:1 and stay in RAM (zero SD writes), so it soaks up
#      normal spikes (the first-boot placeholder dither, several frames
#      converting at once) without ever touching the card. Highest swap
#      priority (100), so the kernel fills it first.
#   2. A small on-SD swapfile — the LAST-RESORT safety net, only reached when
#      zram is completely full. Kept at dphys-swapfile's default (negative)
#      swap priority, far below zram's 100, so under normal and even heavy
#      load it is never written — no SD wear — but it prevents an outright
#      OOM kill under extreme pressure.
#   3. vm.swappiness=100 — bias the kernel toward moving cold anonymous pages
#      into (cheap) zram rather than evicting executable pages and re-reading
#      them from the slow SD. That page-cache thrash is what wedged a first
#      boot on real hardware: with NO swap at all, a spike had nowhere to go,
#      and sshd + hokku-server were both unreachable for minutes while the Pi
#      still answered ping, cleared only by a power-cycle.

# zram (zram-tools, installed via 00-packages): device sized at 100% of RAM,
# zstd for a better ratio (fine on the quad-core A53), swap priority 100.
cat > /etc/default/zramswap <<'ZRAM'
# Managed by the Hokku appliance image (os/pi/stage-hokku/01-pi-tweaks).
ALGO=zstd
PERCENT=100
PRIORITY=100
ZRAM
systemctl enable zramswap.service 2>/dev/null || \
    echo "WARNING: zramswap.service missing (zram-tools not installed?)"

# Last-resort on-SD swapfile via dphys-swapfile: 512 MB, created on first boot
# (not baked into the image, so the .img stays small). Its default swap
# priority is negative — well below zram's 100 — so it is only ever used once
# zram is exhausted, keeping SD writes to genuine emergencies.
if [ -f /etc/dphys-swapfile ]; then
    sed -i 's/^#\?CONF_SWAPSIZE=.*/CONF_SWAPSIZE=512/' /etc/dphys-swapfile
fi
systemctl enable dphys-swapfile 2>/dev/null || true

# Prefer swapping cold anon pages to zram over dropping/re-reading page cache.
cat > /etc/sysctl.d/99-hokku-vm.conf <<'SYSCTL'
# Managed by the Hokku appliance image (os/pi/stage-hokku/01-pi-tweaks).
vm.swappiness=100
SYSCTL

if systemctl is-enabled avahi-daemon >/dev/null 2>&1; then
    systemctl disable avahi-daemon
fi

# First-boot rootfs expansion (see the unit written above this on_chroot).
chmod +x /usr/local/sbin/hokku-resize-rootfs
systemctl enable hokku-resize-rootfs.service 2>/dev/null || \
    echo "WARNING: could not enable hokku-resize-rootfs.service"

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

# Background daemons with no purpose on this headless, single-function
# appliance — each is idle RAM plus periodic wakeups for zero benefit here.
# Disabling them also frees a little of the tight 512 MB budget.
#   ModemManager — cellular / USB-modem manager; there is no modem
#   udisks2      — removable-disk + automount daemon; nothing hot-plugs
#   triggerhappy — global hotkey daemon; there are no input devices
for unit in ModemManager udisks2 triggerhappy; do
    systemctl disable --now "$unit" 2>/dev/null || true
done

# fstrim.timer is intentionally left enabled — TRIM helps SD-card wear, it's
# not a source of it.
EOF
