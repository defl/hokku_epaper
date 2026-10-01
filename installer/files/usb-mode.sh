#!/bin/sh
# Set the role of the Pi's single USB data port. Takes effect on the NEXT boot.
#
#   peripheral  USB gadget serial console on /dev/ttyGS0 — setup ("config") mode
#   host        USB host, so "Flash a screen" can drive a frame — hokku mode
#
# It is ONE port and dwc2 gives it ONE role per boot. dr_mode=otg (both roles
# at once) was tried and reverted: the console enumerated unreliably on Windows
# and merely inserting an empty OTG adapter grounded the ID pin, so the port
# came up as a host at boot and USB host-init wedged the whole boot. See the
# comment in os/pi/stage-hokku/01-pi-tweaks/00-run.sh.
#
# So the role is switched at the mode transitions instead — every one of which
# already ends in a reboot, which is exactly what makes this workable:
#
#   image build          -> peripheral  (os/pi/stage-hokku/01-pi-tweaks)
#   wizard applies       -> host        (flask_app._apply_settings -> reboot)
#   reset.sh             -> peripheral  (back to setup mode -> reboot)
#   wifi-watchdog.sh     -> peripheral  (WiFi never connected -> reboot)
#
# The two revert paths matter as much as the forward one: they are what keeps
# the console the console of last resort. Any appliance that loses its network
# falls back to setup mode on its own, and the serial console comes back with
# it.
#
# Idempotent, and a no-op on a machine with no Pi boot config (dev box, CI).
# Called at image-build time inside the pi-gen chroot too, so it must not
# assume a running system.

set -e

HOKKU_BOOT_ROOT="${HOKKU_BOOT_ROOT:-}"

MODE="$1"
case "$MODE" in
    peripheral|host) ;;
    *)
        echo "usage: $0 peripheral|host" >&2
        exit 2
        ;;
esac

# Bookworm moved the boot partition to /boot/firmware; older images use /boot.
# HOKKU_BOOT_ROOT prefixes both (empty in production) so this is testable
# against a scratch directory instead of a real /boot.
find_boot_file() {
    for candidate in "${HOKKU_BOOT_ROOT}/boot/firmware/$1" "${HOKKU_BOOT_ROOT}/boot/$1"; do
        if [ -f "$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

config=$(find_boot_file config.txt) || config=""
cmdline=$(find_boot_file cmdline.txt) || cmdline=""

if [ -z "$config" ] && [ -z "$cmdline" ]; then
    echo "hokku-usb-mode: no Pi boot config found — nothing to do"
    exit 0
fi

# config.txt: exactly one dwc2 overlay line, with the role we want. Strip any
# existing one first so repeated runs can't stack up conflicting overlays.
# Appended at the end, which assumes config.txt does not close inside a
# model-specific filter section ([pi4], [cm4], …) — stock Raspberry Pi OS ends
# in [all], and the image build appends gpu_mem/disable-bt the same way.
if [ -n "$config" ]; then
    sed -i -E '/^[[:space:]]*dtoverlay=dwc2([,=].*)?$/d' "$config"
    echo "dtoverlay=dwc2,dr_mode=${MODE}" >> "$config"
    echo "hokku-usb-mode: ${config}: dtoverlay=dwc2,dr_mode=${MODE}"
fi

# cmdline.txt is a single line. dwc2 itself is loaded in both roles; g_serial
# (the gadget that creates /dev/ttyGS0) only makes sense in peripheral mode.
if [ -n "$cmdline" ]; then
    # NB: an `[ ... ] && x=y` one-liner here would exit the script under set -e
    # whenever the test is false. Keep the explicit if.
    if [ "$MODE" = "peripheral" ]; then
        modules="dwc2,g_serial"
    else
        modules="dwc2"
    fi
    line=$(sed -E 's/(^| )modules-load=[^ ]*//g' "$cmdline" | tr -d '\n')
    echo "${line} modules-load=${modules}" > "$cmdline"
    echo "hokku-usb-mode: ${cmdline}: modules-load=${modules}"
fi

# The login prompt on the gadget tty. Generic getty@.service, NOT
# serial-getty@ — see docs/os_pi_usb_console.md for why that distinction is
# load-bearing. No --now on the disable: the caller reboots straight after,
# and tearing down a console someone may be reading from is gratuitous.
if command -v systemctl >/dev/null 2>&1; then
    if [ "$MODE" = "peripheral" ]; then
        systemctl enable getty@ttyGS0 >/dev/null 2>&1 \
            || echo "hokku-usb-mode: WARNING: could not enable getty@ttyGS0" >&2
    else
        systemctl disable getty@ttyGS0 >/dev/null 2>&1 \
            || echo "hokku-usb-mode: WARNING: could not disable getty@ttyGS0" >&2
    fi
fi

echo "hokku-usb-mode: USB data port will be '${MODE}' after the next reboot"
