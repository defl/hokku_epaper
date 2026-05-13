"""Bonjour / mDNS advertisement for the Hokku HTTP service.

Advertises ``_http._tcp.local.`` so that browsers and devices on the LAN
can reach the server at ``<hostname>.local`` without knowing its IP.
The service appears as ``Hokku._http._tcp.local.`` with a
``path=/hokku/ui`` TXT record.
"""
from __future__ import annotations

import socket
import sys
from typing import Any

from zeroconf import ServiceInfo, Zeroconf


def _get_local_ip() -> str:
    """Return the primary LAN IPv4 address without sending any packets."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def start_mdns(port: int, hostname: str) -> Any:
    """Register the Hokku HTTP service via mDNS.

    Advertises as ``Hokku._http._tcp.local.`` with A-record
    ``<hostname>.local.`` pointing to the local LAN IP.

    *hostname* is the label before ``.local`` (e.g. ``"hokku-server"``).
    Pass an empty string to skip registration (caller's responsibility).

    Returns the ``Zeroconf`` instance (keep the reference alive for the life
    of the process).  Logs and returns ``None`` on unexpected failure.
    """
    try:
        local_ip = _get_local_ip()
        info = ServiceInfo(
            "_http._tcp.local.",
            "Hokku._http._tcp.local.",
            addresses=[socket.inet_aton(local_ip)],
            port=port,
            properties={"path": "/hokku/ui"},
            server=f"{hostname}.local.",
        )
        zc = Zeroconf()
        zc.register_service(info)
        print(f"  mDNS: advertised as {hostname}.local ({local_ip}:{port})")
        return zc
    except Exception as exc:
        print(f"  mDNS: registration failed — {exc}", file=sys.stderr)
        return None


def stop_mdns(zc: Any) -> None:
    """Unregister all services and close the Zeroconf instance.

    Safe to call with ``None`` (no-op).
    """
    if zc is None:
        return
    try:
        zc.unregister_all_services()
        zc.close()
        print("  mDNS: advertisement stopped")
    except Exception as exc:
        print(f"  mDNS: error while stopping — {exc}", file=sys.stderr)
