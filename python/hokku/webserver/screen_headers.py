"""Parse firmware HTTP headers (battery, frame state)."""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

BATTERY_MV_EMPTY = 3400
BATTERY_MV_FULL = 4100


def battery_percent(mv: int | float | None) -> int | None:
    if mv is None or mv <= 0:
        return None
    pct = round((mv - BATTERY_MV_EMPTY) * 100 / (BATTERY_MV_FULL - BATTERY_MV_EMPTY))
    return max(0, min(100, pct))


def parse_battery_header(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if v < 2000 or v > 5000:
        return None
    return v


def parse_firmware_version(raw: str | None) -> str | None:
    """Parse X-Firmware-Version header — returns the stripped string or None."""
    if not raw:
        return None
    v = str(raw).strip()
    return v if v else None


def parse_screen_model(raw: str | None) -> str | None:
    """Parse the X-Screen-Model header and validate it against known models.

    Returns the model id (e.g. ``"huessen_epf1301"``) only if it is a
    registered display model; otherwise ``None`` for missing, empty, or
    unknown values.  There is no default — callers reject a ``None`` result
    so every screen must self-identify with a recognised model.
    """
    if not raw:
        return None
    # Imported lazily to avoid a heavy import chain at module load time.
    from hokku.screens.registry import DISPLAY_REGISTRY  # noqa: PLC0415

    v = str(raw).strip()
    return v if v in DISPLAY_REGISTRY else None


def parse_firmware_build(raw: str | None) -> str | None:
    """Parse X-Firmware-Build header — returns the stripped timestamp or None."""
    if not raw:
        return None
    v = str(raw).strip()
    return v if v else None


def parse_mac_header(raw: str | None) -> str | None:
    """Parse the X-Screen-Mac header into a normalised lowercase
    ``aa:bb:cc:dd:ee:ff``. Returns None for missing/malformed values. This MAC is
    the device's durable per-screen key (the name is only a mutable label)."""
    if not raw:
        return None
    v = str(raw).strip().lower()
    parts = v.split(":")
    if len(parts) != 6:
        return None
    for p in parts:
        if len(p) != 2 or any(c not in "0123456789abcdef" for c in p):
            return None
    if v == "00:00:00:00:00:00":
        return None
    return v


def parse_cal_ppm(raw) -> int | None:
    """Parse a device-reported drift correction (frame-state ``cal_ppm``) into a
    bounded int, or None if absent/implausible. The bound matches the firmware
    clamp (+/-150000 ppm) so a garbled value can't poison the server mean."""
    if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    v = int(raw)
    if v < -150000 or v > 150000:
        return None
    return v


def parse_frame_state(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as e:
        logger.warning("X-Frame-State not valid JSON (%s): %s", e, str(raw)[:120])
        return None
    if not isinstance(data, dict):
        logger.warning("X-Frame-State is not a JSON object: %s", str(raw)[:120])
        return None
    return data


def parse_config_state(raw: str | None) -> dict | None:
    """Parse the X-Config-State header a device sends during OTA: a JSON object of
    its current NVS config (wifi creds, image_url, screen_name, wifi_order,
    cfg_ver). Returns the dict, or None if absent/malformed."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as e:
        logger.warning("X-Config-State not valid JSON (%s): %s", e, str(raw)[:120])
        return None
    if not isinstance(data, dict):
        logger.warning("X-Config-State is not a JSON object: %s", str(raw)[:120])
        return None
    return data
