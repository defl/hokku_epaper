#!/bin/sh
# Reset hokku to installer mode. Run via SSH or from the hokku-server admin UI.
# Deletes the setup sentinel and reboots — the installer AP will come back up.

SENTINEL=/var/lib/hokku-installer/setup_complete

if [ "$(id -u)" -ne 0 ]; then
    echo "Must be run as root" >&2
    exit 1
fi

echo "Removing setup sentinel and rebooting into installer mode..."
rm -f "$SENTINEL"

# Setup mode gets the USB serial console back (hokku mode leaves the port in
# host mode so it can flash a frame). Best-effort — never block the revert.
/usr/lib/hokku-installer/usb-mode.sh peripheral || \
    echo "WARNING: could not restore the USB serial console" >&2

systemctl reboot
