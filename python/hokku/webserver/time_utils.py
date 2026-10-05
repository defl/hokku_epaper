"""Sleep scheduling and human-readable duration formatting.

Renamed from `time.py` to avoid shadowing stdlib `time`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from hokku.webserver.app_config import AppConfig

DEBUG_FAST_REFRESH_SECONDS = 180

#: A slot this close ahead counts as the one being served now. Screens wake a
#: little early (oscillator drift, rounding), and without this a screen arriving
#: at 11:59:59 for the 12:00 slot was told to come back in a minute and refreshed
#: twice.
EARLY_WAKE_GRACE_SECONDS = 60


def calculate_sleep_seconds(config: AppConfig) -> int:
    """Seconds until the next configured refresh time (system local TZ).

    A slot less than :data:`EARLY_WAKE_GRACE_SECONDS` ahead is treated as the
    current one, so the answer is always the slot after it. In debug-fast-refresh
    mode the schedule is bypassed and a flat 180s is used instead. With no times
    configured, defaults to 6h.
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

    earliest = now + timedelta(seconds=EARLY_WAKE_GRACE_SECONDS)
    # Today, then tomorrow; the day after covers a lone slot that falls inside
    # the grace window just after midnight.
    for day in range(3):
        base = now + timedelta(days=day)
        for h, m in wake_times:
            candidate = base.replace(hour=h, minute=m, second=0, microsecond=0)
            if candidate > earliest:
                return int((candidate - now).total_seconds())
    raise AssertionError("unreachable: a configured slot always recurs within 2 days")


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
