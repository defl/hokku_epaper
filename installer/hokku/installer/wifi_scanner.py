"""Scan for nearby WiFi networks via nmcli.

Returns a list of dicts with ssid, signal (0-100), and security info.
Used by the /api/scan endpoint on the setup form.
"""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)


def scan_networks() -> list[dict]:
    """Return list of visible SSIDs sorted by signal strength (strongest first).

    Triggers a fresh scan then reads the results. Returns [] on any failure so
    the form degrades gracefully (user can type SSID manually).
    """
    # Trigger a new scan; ignore errors (may fail if adapter is in AP mode).
    subprocess.run(
        ["nmcli", "device", "wifi", "rescan"],  # noqa: S607
        capture_output=True,
        timeout=10,
    )

    r = subprocess.run(
        ["nmcli", "--terse", "--fields", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=10,
    )
    if r.returncode != 0:
        logger.warning("nmcli wifi list failed: %s", r.stderr.strip())
        return []

    seen: dict[str, dict] = {}
    for line in r.stdout.splitlines():
        # nmcli -t output: SSID:SIGNAL:SECURITY — colons in SSID are escaped as \:
        # Split on unescaped colons only.
        parts = _split_nmcli_terse(line)
        if len(parts) < 3:
            continue
        ssid = parts[0].replace("\\:", ":")
        if not ssid:
            continue  # hidden network
        try:
            signal = int(parts[1])
        except ValueError:
            signal = 0
        security = parts[2]

        # Keep only the strongest signal for each SSID.
        if ssid not in seen or signal > seen[ssid]["signal"]:
            seen[ssid] = {
                "ssid": ssid,
                "signal": signal,
                "secured": bool(security and security != "--"),
            }

    return sorted(seen.values(), key=lambda n: n["signal"], reverse=True)


def _split_nmcli_terse(line: str) -> list[str]:
    """Split a nmcli --terse line on unescaped colons."""
    parts = []
    current: list[str] = []
    i = 0
    while i < len(line):
        if line[i] == "\\" and i + 1 < len(line) and line[i + 1] == ":":
            current.append("\\:")
            i += 2
        elif line[i] == ":":
            parts.append("".join(current))
            current = []
            i += 1
        else:
            current.append(line[i])
            i += 1
    parts.append("".join(current))
    return parts
