"""Bonjour / mDNS advertisement for the Hokku HTTP service.

Advertises ``_http._tcp.local.`` so that browsers and devices on the LAN
can reach the server at the fixed address ``hokku-server.local`` without
knowing its IP.  The service appears as ``Hokku._http._tcp.local.`` with a
``path=/hokku/ui`` TXT record.
"""
from __future__ import annotations

import socket
import sys
from typing import Any

from zeroconf import ServiceInfo, Zeroconf

_MDNS_HOSTNAME = "hokku-server"  # resolves as hokku-server.local on the LAN


def _get_local_ip() -> str:
    """Return the primary LAN IPv4 address without sending any packets."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def start_mdns(port: int) -> Any:
    """Register the Hokku HTTP service via mDNS.

    Advertises as ``Hokku._http._tcp.local.`` with A-record
    ``hokku-server.local.`` pointing to the local LAN IP.

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
            server=f"{_MDNS_HOSTNAME}.local.",
        )
        zc = Zeroconf()
        zc.register_service(info)
        print(f"  mDNS: advertised as {_MDNS_HOSTNAME}.local ({local_ip}:{port})")
        return zc
    except Exception as exc:
        print(f"  mDNS: registration failed — {exc}", file=sys.stderr)
        return None
