"""Parse firmware HTTP headers (battery, frame state)."""
from __future__ import annotations

import json

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


def parse_frame_state(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as e:
        print(f"  Warning: X-Frame-State not valid JSON ({e}): {str(raw)[:120]}")
        return None
    if not isinstance(data, dict):
        print(f"  Warning: X-Frame-State is not a JSON object: {str(raw)[:120]}")
        return None
    return data
