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
