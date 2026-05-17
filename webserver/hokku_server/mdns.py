"""Bonjour / mDNS advertisement for the Hokku HTTP service.

Advertises ``_http._tcp.local.`` so that browsers and devices on the LAN
can reach the server at ``<hostname>.local`` without knowing its IP.
The service appears as ``Hokku <hostname>._http._tcp.local.`` with a
``path=/hokku/ui`` TXT record. Using the hostname in the instance name
keeps multiple hokku servers on the same LAN from colliding during probing.
"""
from __future__ import annotations

import logging
import socket
import time
from typing import Any

from zeroconf import ServiceInfo, Zeroconf

logger = logging.getLogger(__name__)


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
    local_ip = _get_local_ip()
    # Service instance name is unique per hostname so multiple hokku servers
    # on the same LAN don't collide during mDNS probing.
    # The A record (server=) is what drives <hostname>.local resolution.
    instance = f"Hokku {hostname}._http._tcp.local."
    info = ServiceInfo(
        "_http._tcp.local.",
        instance,
        addresses=[socket.inet_aton(local_ip)],
        port=port,
        properties={"path": "/hokku/ui"},
        server=f"{hostname}.local.",
    )
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        zc = None
        try:
            zc = Zeroconf()
            zc.register_service(info)
            logger.info("Advertised as %s.local (%s:%s)", hostname, local_ip, port)
            return zc
        except Exception as exc:
            last_exc = exc
            # Always close on failure — an unclosed Zeroconf instance keeps
            # background threads alive and will respond to future probes as if
            # it owns the name, causing NonUniqueNameException on every retry.
            if zc is not None:
                try:
                    zc.close()
                except Exception:
                    pass
            if attempt < 3:
                logger.warning(
                    "Attempt %d failed (%s: %s) — retrying in 3s",
                    attempt, type(exc).__name__, exc,
                )
                time.sleep(3)

    logger.error("Registration failed — %s: %s", type(last_exc).__name__, last_exc)
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
        logger.info("Advertisement stopped")
    except Exception as exc:
        logger.warning("Error while stopping — %s", exc)
