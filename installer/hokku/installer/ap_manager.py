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

# Pin the radio to 2.4 GHz (band "bg"). This matters for the AP being *visible*
# on Apple devices, which is otherwise the setup wizard's single point of
# failure — if the phone can't see "Hokku Setup", the whole appliance is
# unreachable.
#
#   band=bg: with the band left unset, NetworkManager/wpa_supplicant picks a
#   mode and rates that iPhones scan and list poorly. Every working "NM AP +
#   iOS" recipe pins band=bg (forces hw_mode=g and the legacy-compatible
#   beacon). The Pi Zero 2 W has no 5 GHz radio anyway, so bg is the only band.
_AP_BAND = "bg"

# Channel: chosen dynamically at start-up (see _pick_channel). We can't leave it
# unset — the Pi's brcmfmac driver does NOT support automatic channel selection
# (ACS is nl80211-Atheros-only), so an unset channel just makes NM default to
# channel 1 rather than picking the quietest one. A single hardcoded channel is
# no better: a Pi Zero's weak radio sitting co-channel with a strong router/mesh
# node gets buried, and iOS drops a weak co-channel AP from its scan list
# entirely (observed directly — "Hokku Setup" co-channel with a mesh node was
# invisible on an iPhone while a laptop saw it at 100%).
#
# Key insight: everybody's router clusters on the three *non-overlapping*
# channels 1/6/11 (that's their whole point — max throughput without stepping on
# neighbours), which makes those three the most crowded. But this is a setup AP
# that serves a few KB of config wizard — we do NOT care about throughput or
# adjacent-channel interference. So we consider *every* 2.4 GHz channel and pick
# the emptiest one, which is usually an "in-between" channel (3, 8, 9…) that
# well-behaved routers deliberately avoid. Sitting alone on its channel is what
# keeps the AP out of the co-channel pile-up that iOS prunes. The overlap-
# weighted scoring below still accounts for adjacent-channel energy so we don't
# park right next to the single strongest neighbour.
#
# Candidates are 1-11 (US regulatory domain — the country baked into the image;
# 12-13 aren't permitted there). _AP_CHANNEL_DEFAULT is the fallback if the scan
# yields nothing (fresh boot before the first scan completes, or nmcli error).
_AP_CHANNEL_CANDIDATES = tuple(str(c) for c in range(1, 12))
_AP_CHANNEL_DEFAULT = "6"


def _nmcli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["nmcli", *args],  # noqa: S607
        capture_output=True,
        text=True,
    )


def _channel_overlap(distance: int) -> float:
    """Penalty weight of a neighbour `distance` channels away, for AP visibility.

    The failure we care about is *co-channel*: iOS collapses a weak AP that
    shares another AP's exact channel out of its scan list. Adjacent-channel
    overlap only raises the noise floor (a throughput problem), and this AP
    ships a few KB of setup wizard — throughput is irrelevant. So a same-channel
    neighbour is penalised ~10x+ more than any adjacent one; the small adjacent
    term exists only as a tiebreaker, to steer an otherwise-empty pick away from
    sitting right beside the single strongest neighbour. 5+ apart = no overlap.
    """
    if distance == 0:
        return 1.0
    if distance >= 5:
        return 0.0
    return 0.1 / distance  # dist 1..4 -> 0.10, 0.05, 0.033, 0.025


def _pick_channel() -> str:
    """Pick the emptiest 2.4 GHz channel (1-11) by scanning neighbours.

    Must be called while wlan0 is idle (not already an AP) — a radio that is
    beaconing can't scan, so this only works in the window between tearing the
    old AP down and bringing the new one up. Returns _AP_CHANNEL_DEFAULT if the
    scan yields nothing usable (fresh boot with a slow first scan, nmcli error).
    """
    try:
        r = subprocess.run(
            [  # noqa: S607
                "nmcli",
                "-t",
                "-f",
                "CHAN,SIGNAL",
                "device",
                "wifi",
                "list",
                "ifname",
                _AP_INTERFACE,
                "--rescan",
                "yes",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning(
            "Channel scan failed (%s); using default channel %s", exc, _AP_CHANNEL_DEFAULT
        )
        return _AP_CHANNEL_DEFAULT

    neighbours: list[tuple[int, int]] = []  # (channel, signal 0-100)
    for line in r.stdout.splitlines():
        # Terse output is "CHAN:SIGNAL"; take the first two fields only.
        parts = line.split(":")
        if len(parts) < 2:
            continue
        try:
            chan, signal = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        neighbours.append((chan, signal))

    if not neighbours:
        logger.warning(
            "Channel scan saw no neighbours; using default channel %s", _AP_CHANNEL_DEFAULT
        )
        return _AP_CHANNEL_DEFAULT

    scores = {}
    for cand in _AP_CHANNEL_CANDIDATES:
        cand_num = int(cand)
        scores[cand] = sum(
            signal * _channel_overlap(abs(chan - cand_num)) for chan, signal in neighbours
        )
    best = min(scores, key=lambda c: scores[c])
    logger.info(
        "Channel congestion scores %s from %d neighbours -> channel %s",
        {c: round(scores[c]) for c in _AP_CHANNEL_CANDIDATES},
        len(neighbours),
        best,
    )
    return best


def start_ap() -> None:
    """Create and bring up the setup access point."""
    _delete_ap_if_exists()

    # Scan now, while the radio is idle (old AP torn down, new one not up yet),
    # to pick the least-congested channel for this environment.
    channel = _pick_channel()

    logger.info(
        "Creating AP connection %r (SSID: %r) on channel %s", _AP_CON_NAME, _AP_SSID, channel
    )
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
        # 2.4 GHz + scanned-quietest channel so Apple devices can actually see
        # the AP (see the _AP_BAND / _pick_channel notes above).
        "802-11-wireless.band",
        _AP_BAND,
        "802-11-wireless.channel",
        channel,
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
