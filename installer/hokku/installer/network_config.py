"""Write NetworkManager WiFi connection profiles and reload connections.

Writes a .nmconnection keyfile directly to /etc/NetworkManager/system-connections/
(mode 600, root-owned) and reloads NetworkManager so it picks up the new profile
without a full restart.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

_NM_CONNECTIONS = Path("/etc/NetworkManager/system-connections")
_CONNECTION_NAME = "hokku-wifi"
_CONNECTION_FILE = _NM_CONNECTIONS / f"{_CONNECTION_NAME}.nmconnection"


class StaticConfig(NamedTuple):
    ip: str  # e.g. "192.168.1.100"
    prefix: int  # e.g. 24
    gateway: str  # e.g. "192.168.1.1"
    dns: str  # e.g. "8.8.8.8,8.8.4.4"


def _nmconnection(
    ssid: str,
    password: str,
    static: StaticConfig | None,
) -> str:
    """Build an nmconnection keyfile string."""
    wifi_security = f"\n[wifi-security]\nkey-mgmt=wpa-psk\npsk={password}\n" if password else ""
    if static:
        dns_entries = ";".join(s.strip() for s in static.dns.split(",") if s.strip())
        ipv4_section = (
            "[ipv4]\n"
            f"address1={static.ip}/{static.prefix},{static.gateway}\n"
            f"dns={dns_entries};\n"
            "method=manual\n"
        )
    else:
        ipv4_section = "[ipv4]\nmethod=auto\n"

    return (
        "[connection]\n"
        f"id={_CONNECTION_NAME}\n"
        "type=wifi\n"
        "autoconnect=true\n"
        "\n"
        "[wifi]\n"
        f"ssid={ssid}\n"
        "mode=infrastructure\n"
        f"{wifi_security}"
        "\n"
        f"{ipv4_section}"
        "\n"
        "[ipv6]\n"
        "method=auto\n"
    )


def write_wifi_connection(
    ssid: str,
    password: str,
    static: StaticConfig | None = None,
) -> None:
    """Write the WiFi .nmconnection file and reload NM connections."""
    content = _nmconnection(ssid, password, static)
    _NM_CONNECTIONS.mkdir(parents=True, exist_ok=True)

    # Atomic write: temp file in the same dir, then rename.
    fd, tmp = tempfile.mkstemp(dir=_NM_CONNECTIONS, prefix=".hokku-wifi-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, _CONNECTION_FILE)
        _CONNECTION_FILE.chmod(0o600)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    logger.info("Wrote WiFi connection profile to %s", _CONNECTION_FILE)
    _reload_connections()


def _reload_connections() -> None:
    result = subprocess.run(
        ["nmcli", "connection", "reload"],  # noqa: S607
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.warning("nmcli connection reload failed: %s", result.stderr.strip())
    else:
        logger.info("NetworkManager connections reloaded")
