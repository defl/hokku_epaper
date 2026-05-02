"""Sleep scheduling and human-readable duration formatting."""
from datetime import datetime, timedelta

from webserver.config import AppConfig

DEBUG_FAST_REFRESH_SECONDS = 180


def calculate_sleep_seconds(config: AppConfig) -> int:
    """Seconds until next refresh_image_at_time (or DEBUG_FAST_REFRESH_SECONDS in debug mode)."""
    if config.debug_fast_refresh:
        return DEBUG_FAST_REFRESH_SECONDS

    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(config.timezone)
    except (ImportError, Exception):
        tz = None

    if tz:
        now = datetime.now(tz)
    else:
        now = datetime.now()

    times = config.refresh_image_at_time
    if not times:
        return 21600

    wake_times = []
    for t in times:
        t = str(t).zfill(4)
        h, m = int(t[:2]), int(t[2:])
        wake_times.append((h, m))
    wake_times.sort()

    for h, m in wake_times:
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate > now:
            delta = (candidate - now).total_seconds()
            return max(60, int(delta))

    h, m = wake_times[0]
    tomorrow = now + timedelta(days=1)
    candidate = tomorrow.replace(hour=h, minute=m, second=0, microsecond=0)
    delta = (candidate - now).total_seconds()
    return max(60, int(delta))


def format_duration_human(minutes):
    """Format minutes into human-readable duration string."""
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
