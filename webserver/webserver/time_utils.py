"""Sleep scheduling and human-readable duration formatting.

Renamed from `time.py` to avoid shadowing stdlib `time`.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from webserver.app_config import AppConfig


DEBUG_FAST_REFRESH_SECONDS = 180


def calculate_sleep_seconds(config: AppConfig) -> int:
    """Seconds until the next configured refresh time (system local TZ).

    In debug-fast-refresh mode the schedule is bypassed and a flat 180s is
    used instead. With no times configured, defaults to 6h.
    """
    if config.debug_fast_refresh:
        return DEBUG_FAST_REFRESH_SECONDS

    now = datetime.now().astimezone()  # system tz
    times = config.refresh_image_at_time
    if not times:
        return 21600

    wake_times: list[tuple[int, int]] = []
    for t in times:
        s = str(t).zfill(4)
        wake_times.append((int(s[:2]), int(s[2:])))
    wake_times.sort()

    for h, m in wake_times:
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate > now:
            return max(60, int((candidate - now).total_seconds()))

    # All today's slots are past — use tomorrow's first slot.
    h, m = wake_times[0]
    tomorrow = now + timedelta(days=1)
    candidate = tomorrow.replace(hour=h, minute=m, second=0, microsecond=0)
    return max(60, int((candidate - now).total_seconds()))


def format_duration_human(minutes: float) -> str:
    if minutes < 0:
        return "0m"
    if minutes < 60:
        return f"{int(minutes)}m"
    hours = minutes / 60
    if hours < 24:
        h = int(hours)
        m = int(minutes % 60)
        return f"{h}h {m}m" if m > 0 else f"{h}h"
    days = hours / 24
    if days < 30:
        d = int(days)
        h = int(hours % 24)
        return f"{d}d {h}h" if h > 0 else f"{d}d"
    if days < 365:
        mo = int(days / 30)
        d = int(days % 30)
        return f"{mo}mo {d}d" if d > 0 else f"{mo}mo"
    years = int(days / 365)
    mo = int((days % 365) / 30)
    return f"{years}y {mo}mo" if mo > 0 else f"{years}y"
