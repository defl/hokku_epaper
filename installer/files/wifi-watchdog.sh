#!/bin/sh
# hokku-wifi-watchdog: run once after first boot post-setup.
# Waits up to 2 minutes for WiFi to connect. If it never connects, deletes
# the setup sentinel and reboots so the installer AP comes back up.
# The 90-second ExecStartPre in the .service unit fires before this script,
# giving NetworkManager time to attempt the connection first.

set -e

SENTINEL=/var/lib/hokku-installer/setup_complete
MAX_WAIT=120  # seconds (in addition to the 90s ExecStartPre sleep)
INTERVAL=10

elapsed=0
while [ "$elapsed" -lt "$MAX_WAIT" ]; do
    state=$(nmcli -t -f STATE general status 2>/dev/null | head -1 || echo "unknown")
    if [ "$state" = "connected" ]; then
        echo "hokku-wifi-watchdog: WiFi connected (state=$state) — all good"
        exit 0
    fi
    echo "hokku-wifi-watchdog: not connected yet (state=$state, elapsed=${elapsed}s)"
    sleep "$INTERVAL"
    elapsed=$((elapsed + INTERVAL))
done

echo "hokku-wifi-watchdog: WiFi never connected after $((90 + MAX_WAIT))s — reverting to installer mode"
rm -f "$SENTINEL"

# Back to setup mode means back to the USB serial console — this is the path
# that makes an unreachable appliance diagnosable over a cable again.
# Best-effort: a failure here must not stop the revert reboot.
/usr/lib/hokku-installer/usb-mode.sh peripheral || \
    echo "hokku-wifi-watchdog: WARNING: could not restore the USB serial console"

systemctl reboot
