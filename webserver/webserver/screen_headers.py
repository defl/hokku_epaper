"""Parse firmware HTTP headers (battery, frame state)."""
import json

BATTERY_MV_EMPTY = 3400
BATTERY_MV_FULL = 4100


def battery_percent(mv):
    if mv is None or mv <= 0:
        return None
    pct = round((mv - BATTERY_MV_EMPTY) * 100 / (BATTERY_MV_FULL - BATTERY_MV_EMPTY))
    return max(0, min(100, pct))


def parse_battery_header(raw):
    if not raw:
        return None
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if v < 2000 or v > 5000:
        return None
    return v


def parse_frame_state(raw):
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


def _battery_percent(mv):
    return battery_percent(mv)


def _parse_battery_header(raw):
    return parse_battery_header(raw)


def _parse_frame_state(raw):
    return parse_frame_state(raw)
