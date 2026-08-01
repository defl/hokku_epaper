#!/bin/bash -e
# Pi Zero 2 W optimisations.

# GPU memory: minimum (16 MB) — server is headless. disable-bt turns off the
# unused Bluetooth radio (smaller attack surface, one less background daemon).
#
# The USB data port's role (dwc2 dr_mode, the g_serial gadget and its getty) is
# NOT set here — it is set in the on_chroot block below by hokku-installer's
# usb-mode.sh, the same script the wizard and the recovery paths call at
# runtime. The image ships in "peripheral": a USB gadget serial console on
# /dev/ttyGS0, which is what setup mode wants. See docs/os_pi_usb_console.md.
for cfg in \
    "${ROOTFS_DIR}/boot/firmware/config.txt" \
    "${ROOTFS_DIR}/boot/config.txt"; do
    if [ -f "$cfg" ]; then
        echo "gpu_mem=16" >> "$cfg"
        echo "dtoverlay=disable-bt" >> "$cfg"
        break
    fi
done

# journald: persistent but tightly capped. A purely RAM-only journal is easiest
# on the SD card, but it makes boot problems undiagnosable — a reboot erases the
# evidence, exactly the trap we hit debugging a first-boot that misbehaved before
# SSH was up. Keep a small persistent journal so `journalctl -b -1` can show the
# previous boot; the 100 MB cap + compression bound the write wear.
mkdir -p "${ROOTFS_DIR}/etc/systemd/journald.conf.d"
cat > "${ROOTFS_DIR}/etc/systemd/journald.conf.d/hokku.conf" <<'JOURNALEOF'
[Journal]
Storage=persistent
Compress=yes
SystemMaxUse=100M
SystemMaxFileSize=20M
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
systemctl enable zramswap.service || {
    echo "ERROR: could not enable zramswap.service (zram-tools not installed?)" >&2
    exit 1
}

# Last-resort on-SD swapfile via dphys-swapfile: 512 MB, created on first boot
# (not baked into the image, so the .img stays small). Its default swap
# priority is negative — well below zram's 100 — so it is only ever used once
# zram is exhausted, keeping SD writes to genuine emergencies.
#
# This layer did not exist on any image before now. dphys-swapfile was never in
# 00-packages and is not part of pi-gen's stage2 either, so /etc/dphys-swapfile
# was absent, the `if [ -f ]` below skipped the sizing, `systemctl enable`
# failed into `|| true`, and the build said nothing. Appliances shipped with
# zram as their ONLY swap. Both steps are now loud: if the package is missing
# the build fails here rather than producing an image quietly missing a layer
# of its memory policy.
if [ ! -f /etc/dphys-swapfile ]; then
    echo "ERROR: /etc/dphys-swapfile missing — is dphys-swapfile in 00-packages?" >&2
    exit 1
fi
sed -i 's/^#\?CONF_SWAPSIZE=.*/CONF_SWAPSIZE=512/' /etc/dphys-swapfile
grep -q '^CONF_SWAPSIZE=512$' /etc/dphys-swapfile || {
    echo "ERROR: could not set CONF_SWAPSIZE in /etc/dphys-swapfile" >&2
    exit 1
}
systemctl enable dphys-swapfile || {
    echo "ERROR: could not enable dphys-swapfile.service" >&2
    exit 1
}

# Order zram AFTER the swapfile so that, on shutdown, it is torn down BEFORE
# it — systemd stops units in reverse start order. This is the difference
# between a clean reboot and a hang: `swapoff` on zram has to pull every
# compressed page back into RAM, and on a 464 MB board that can be more than
# will fit. With the SD swapfile still active at that moment the kernel has
# somewhere to put the overflow; without it there is nowhere to go and the
# shutdown wedges with no console message and no journal (observed on real
# hardware, 2026-07-30 — the appliance froze mid-reboot after the setup wizard
# and had to be power-cycled).
mkdir -p /etc/systemd/system/zramswap.service.d
cat > /etc/systemd/system/zramswap.service.d/10-after-swapfile.conf <<'ZRAMORDER'
# Managed by the Hokku appliance image (os/pi/stage-hokku/01-pi-tweaks).
# Start after the on-SD swapfile => stop before it. Keeps the last-resort swap
# available while zram is being swapped off during shutdown.
[Unit]
After=dphys-swapfile.service
Wants=dphys-swapfile.service
ZRAMORDER

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

# USB data port: ship the image in peripheral mode — the gadget serial console
# on /dev/ttyGS0 (dwc2 overlay + g_serial + a getty). That is the state setup
# mode wants, and the image always boots into setup mode first.
#
# Delegated to hokku-installer's usb-mode.sh (installed by the .deb in the 00-
# install stage, which runs before this one) rather than written out here, so
# the build-time default and the runtime switches — the wizard flipping the port
# to host mode on the way into hokku mode, reset.sh and the WiFi watchdog
# flipping it back — are all the same code. The script carries the full
# rationale, including why dr_mode=otg was tried and rejected.
usb_mode_script=/usr/lib/hokku-installer/usb-mode.sh
if [ ! -x "$usb_mode_script" ]; then
    echo "ERROR: $usb_mode_script missing — is hokku-installer installed" >&2
    echo "       before this stage? (os/pi/stage-hokku/00-install)" >&2
    exit 1
fi
"$usb_mode_script" peripheral

# Keep PID 1 away from the gadget tty, or the appliance cannot reboot.
#
# getty@.service defaults have systemd ITSELF open /dev/ttyGS0 around service
# stop (TTYReset / TTYVHangup / TTYVTDisallocate). Opening a USB gadget tty with
# no host attached blocks in an uninterruptible open() — so PID 1 wedges, and
# because the thing that is stuck is the thing that prints progress, there is no
# "A stop job is running" message, no timeout, and nothing in the journal. The
# board sits there answering ping until the hardware watchdog fires ten minutes
# later. An appliance normally has NO cable attached, so this is the DEFAULT
# case, not an edge case: reproduced here 2026-07-31, cable out it hangs every
# time, cable in it reboots every time.
#
# Long known upstream — raspberrypi/linux#1929 and the Pi forum thread
# "Pi Zero gadget serial hangs on shutdown" (t=178917); same symptom reported on
# systemd-devel back in 2015. Note the widely copy-pasted forum snippet spells
# it TTYVDisallocate, which is not a real directive and is silently ignored;
# the correct name is TTYVTDisallocate.
#
# Applies to the unit, so it is harmless in host mode where the getty is off.
mkdir -p /etc/systemd/system/getty@ttyGS0.service.d
cat > /etc/systemd/system/getty@ttyGS0.service.d/10-no-tty-reset.conf <<'GETTYEOF'
# Managed by the Hokku appliance image (os/pi/stage-hokku/01-pi-tweaks).
# Without this the board cannot reboot with no USB host attached — see the
# comment in 01-pi-tweaks and docs/os_pi_usb_console.md.
[Service]
TTYReset=no
TTYVHangup=no
TTYVTDisallocate=no
GETTYEOF

# Bound the damage of ANY wedged shutdown, not just this one. systemd already
# arms the BCM2835 hardware watchdog while shutting down, but at its 10-minute
# default — long enough that a headless appliance looks bricked and gets its
# power pulled. A minute is plenty for a board whose clean reboot takes ~30s.
mkdir -p /etc/systemd/system.conf.d
cat > /etc/systemd/system.conf.d/10-hokku-reboot-watchdog.conf <<'WDEOF'
# Managed by the Hokku appliance image (os/pi/stage-hokku/01-pi-tweaks).
[Manager]
RebootWatchdogSec=60
WDEOF

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
