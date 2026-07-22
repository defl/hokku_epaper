"""Manage the setup access point via NetworkManager.

Creates an open (no-password) WiFi AP named "Hokku Setup" on wlan0 with IP
192.168.11.1/24 and IP sharing (NetworkManager's built-in DHCP). A separate
dnsmasq instance handles DHCP with a custom range; NM's own DHCP/DNS is
disabled via the connection profile.

The AP profile is created fresh on installer start and deleted on teardown so
there are no stale NM profiles left after setup completes.
"""

from __future__ import annotations

import logging
import subprocess
import time

logger = logging.getLogger(__name__)

_AP_CON_NAME = "hokku-ap"
_AP_SSID = "Hokku Setup"
_AP_INTERFACE = "wlan0"
_AP_IP = "192.168.11.1/24"

# Pin the radio to 2.4 GHz (band "bg") and a fixed channel. Both matter for the
# AP being *visible* on Apple devices, which is otherwise the setup wizard's
# single point of failure — if the phone can't see "Hokku Setup", the whole
# appliance is unreachable.
#
#   band=bg: with the band left unset, NetworkManager/wpa_supplicant picks a
#   mode and rates that iPhones scan and list poorly. Every working "NM AP +
#   iOS" recipe pins band=bg (forces hw_mode=g and the legacy-compatible
#   beacon). The Pi Zero 2 W has no 5 GHz radio anyway, so bg is the only band.
#
#   channel=6: without an explicit channel NM lands on channel 1, and a Pi
#   Zero's weak radio sitting co-channel with a strong router/mesh node on the
#   same channel gets buried — Windows still decodes the beacon and lists it,
#   but iOS drops a weak co-channel AP from its scan list entirely (observed
#   directly: "Hokku Setup" on ch 1 alongside a mesh AP was invisible on an
#   iPhone while a laptop saw it at 100%). Channel 6 is the middle of the three
#   non-overlapping 2.4 GHz channels (1/6/11); no static channel is ideal in
#   every environment, but a fixed one off the NM default is far better than
#   auto-selection that reliably collides with channel 1.
_AP_BAND = "bg"
_AP_CHANNEL = "6"


def _nmcli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["nmcli", *args],  # noqa: S607
        capture_output=True,
        text=True,
    )


def start_ap() -> None:
    """Create and bring up the setup access point."""
    _delete_ap_if_exists()

    logger.info("Creating AP connection %r (SSID: %r)", _AP_CON_NAME, _AP_SSID)
    r = _nmcli(
        "connection",
        "add",
        "type",
        "wifi",
        "ifname",
        _AP_INTERFACE,
        "con-name",
        _AP_CON_NAME,
        "ssid",
        _AP_SSID,
        "mode",
        "ap",
        # 2.4 GHz + fixed channel so Apple devices can actually see the AP
        # (see the _AP_BAND / _AP_CHANNEL note above).
        "802-11-wireless.band",
        _AP_BAND,
        "802-11-wireless.channel",
        _AP_CHANNEL,
        # Use manual (not shared) so NM does NOT start its own dnsmasq instance.
        # Our dnsmasq process handles DHCP and captive DNS.
        "ipv4.method",
        "manual",
        "ipv4.addresses",
        _AP_IP,
        "ipv6.method",
        "disabled",
        "connection.autoconnect",
        "no",
    )
    if r.returncode != 0:
        raise RuntimeError(f"nmcli add AP failed: {r.stderr.strip()}")

    logger.info("Bringing up AP")
    r = _nmcli("connection", "up", _AP_CON_NAME)
    if r.returncode != 0:
        raise RuntimeError(f"nmcli up AP failed: {r.stderr.strip()}")

    # Brief pause for the interface to stabilise before dnsmasq starts.
    time.sleep(1)
    logger.info("AP %r is up on %s at %s", _AP_SSID, _AP_INTERFACE, _AP_IP)


def stop_ap() -> None:
    """Bring down and delete the setup AP connection."""
    logger.info("Stopping AP")
    r = _nmcli("connection", "down", _AP_CON_NAME)
    if r.returncode != 0:
        logger.warning("nmcli down AP: %s", r.stderr.strip())
    _delete_ap_if_exists()


def _delete_ap_if_exists() -> None:
    r = _nmcli("connection", "show", _AP_CON_NAME)
    if r.returncode == 0:
        _nmcli("connection", "delete", _AP_CON_NAME)
