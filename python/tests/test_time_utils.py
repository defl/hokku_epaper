"""Unit tests for time_utils: calculate_sleep_seconds and format_duration_human."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from unittest.mock import patch

from hokku.webserver.app_config import AppConfig
from hokku.webserver.time_utils import (
    DEBUG_FAST_REFRESH_SECONDS,
    calculate_sleep_seconds,
    format_duration_human,
)

# ── helpers ───────────────────────────────────────────────────────────────────


def _cfg(**kwargs) -> AppConfig:
    return replace(AppConfig(), **kwargs)


def _fake_now(hour: int, minute: int, second: int = 0) -> datetime:
    """Return today at the given local time (fixed offset so it is stable)."""
    base = datetime.now().astimezone()
    return base.replace(hour=hour, minute=minute, second=second, microsecond=0)


# ── calculate_sleep_seconds ───────────────────────────────────────────────────


def test_debug_fast_refresh_returns_constant():
    cfg = _cfg(debug_fast_refresh=True)
    assert calculate_sleep_seconds(cfg) == DEBUG_FAST_REFRESH_SECONDS


def test_debug_fast_refresh_ignores_times():
    """debug_fast_refresh overrides even with refresh times configured."""
    cfg = _cfg(debug_fast_refresh=True, refresh_image_at_time=["0600", "1200"])
    assert calculate_sleep_seconds(cfg) == DEBUG_FAST_REFRESH_SECONDS


def test_no_refresh_times_returns_six_hours():
    cfg = _cfg(debug_fast_refresh=False, refresh_image_at_time=[])
    assert calculate_sleep_seconds(cfg) == 21600


def test_sleep_seconds_non_negative():
    cfg = _cfg(debug_fast_refresh=False, refresh_image_at_time=["0600", "1200", "1800"])
    assert calculate_sleep_seconds(cfg) > 0


def test_sleep_seconds_minimum_sixty():
    """Result must be at least 60 s (enforced by max(60, ...) in the function)."""
    cfg = _cfg(debug_fast_refresh=False, refresh_image_at_time=["0600", "1200", "1800"])
    assert calculate_sleep_seconds(cfg) >= 60


def test_sleep_at_most_one_day():
    cfg = _cfg(debug_fast_refresh=False, refresh_image_at_time=["0600"])
    assert calculate_sleep_seconds(cfg) <= 86_400


def test_future_slot_chosen_over_past():
    """With one slot in the future, result should be the seconds until that slot."""
    # Set now to 10:00 and the slot to 10:30 → expect ~1800 s.
    now = _fake_now(10, 0)
    cfg = _cfg(debug_fast_refresh=False, refresh_image_at_time=["1030"])
    with patch("hokku.webserver.time_utils.datetime") as mock_dt:
        mock_dt.now.return_value = now
        result = calculate_sleep_seconds(cfg)
    assert 1_700 <= result <= 1_860, f"Expected ~1800 s, got {result}"


def test_all_past_slots_wrap_to_tomorrow():
    """When all today's slots are in the past, result must exceed remaining seconds today."""
    # Set now to 23:00; the single slot is 06:00 → next occurrence is ~7 h away.
    now = _fake_now(23, 0)
    cfg = _cfg(debug_fast_refresh=False, refresh_image_at_time=["0600"])
    with patch("hokku.webserver.time_utils.datetime") as mock_dt:
        mock_dt.now.return_value = now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = calculate_sleep_seconds(cfg)
    # 06:00 tomorrow from 23:00 today = 7 h = 25200 s
    assert 25_000 <= result <= 25_400, f"Expected ~25200 s, got {result}"


def test_multiple_slots_picks_nearest_future():
    """With slots at 06:00, 12:00, 18:00 and now=10:00, next slot is 12:00 → ~7200 s."""
    now = _fake_now(10, 0)
    cfg = _cfg(debug_fast_refresh=False, refresh_image_at_time=["0600", "1200", "1800"])
    with patch("hokku.webserver.time_utils.datetime") as mock_dt:
        mock_dt.now.return_value = now
        result = calculate_sleep_seconds(cfg)
    assert 7_000 <= result <= 7_300, f"Expected ~7200 s, got {result}"


def _sleep_at(now: datetime, times: list[str]) -> int:
    cfg = _cfg(debug_fast_refresh=False, refresh_image_at_time=times)
    with patch("hokku.webserver.time_utils.datetime") as mock_dt:
        mock_dt.now.return_value = now
        return calculate_sleep_seconds(cfg)


def test_early_wake_counts_as_current_slot():
    """A screen waking at 11:59:59 for the 12:00 slot is sent to 18:00, not told
    to come back in a minute (which refreshed it twice)."""
    assert _sleep_at(_fake_now(11, 59, 59), ["0600", "1200", "1800"]) == 6 * 3600 + 1


def test_grace_boundary():
    """Exactly 60 s ahead is still the current slot; 61 s ahead is the next one."""
    assert _sleep_at(_fake_now(11, 59, 0), ["1200", "1800"]) == 6 * 3600 + 60
    assert _sleep_at(_fake_now(11, 58, 59), ["1200", "1800"]) == 61


def test_early_wake_before_lone_midnight_slot_goes_to_next_day():
    """A lone 00:00 slot reached 30 s early is served now; next is a day later."""
    assert _sleep_at(_fake_now(23, 59, 30), ["0000"]) == 24 * 3600 + 30


# ── format_duration_human ─────────────────────────────────────────────────────


def test_format_negative_is_zero():
    assert format_duration_human(-5) == "0m"


def test_format_zero():
    assert format_duration_human(0) == "0m"


def test_format_minutes_only():
    assert format_duration_human(45) == "45m"


def test_format_exactly_one_hour():
    assert format_duration_human(60) == "1h"


def test_format_hours_and_minutes():
    assert format_duration_human(90) == "1h 30m"


def test_format_exactly_one_day():
    assert format_duration_human(24 * 60) == "1d"


def test_format_days_and_hours():
    assert format_duration_human(36 * 60) == "1d 12h"


def test_format_exactly_one_month():
    # 30 days in minutes
    assert format_duration_human(30 * 24 * 60) == "1mo"


def test_format_months_and_days():
    # 45 days = 1 month 15 days
    assert format_duration_human(45 * 24 * 60) == "1mo 15d"


def test_format_exactly_one_year():
    # 365 days
    assert format_duration_human(365 * 24 * 60) == "1y"


def test_format_years_and_months():
    # 395 days = 1 year + ~1 month
    assert format_duration_human(395 * 24 * 60).startswith("1y")


def test_format_large_hours_no_minutes_omits_zero_minutes():
    assert format_duration_human(120) == "2h"


def test_format_large_days_no_hours_omits_zero_hours():
    assert format_duration_human(48 * 60) == "2d"
