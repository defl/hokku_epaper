"""Unit tests for time_utils: calculate_sleep_seconds and format_duration_human."""
from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch
from datetime import datetime, timezone, timedelta

import pytest

from hokku_server.app_config import AppConfig
from hokku_server.time_utils import (
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
    with patch("hokku_server.time_utils.datetime") as mock_dt:
        mock_dt.now.return_value = now
        result = calculate_sleep_seconds(cfg)
    assert 1_700 <= result <= 1_860, f"Expected ~1800 s, got {result}"


def test_all_past_slots_wrap_to_tomorrow():
    """When all today's slots are in the past, result must exceed remaining seconds today."""
    # Set now to 23:00; the single slot is 06:00 → next occurrence is ~7 h away.
    now = _fake_now(23, 0)
    cfg = _cfg(debug_fast_refresh=False, refresh_image_at_time=["0600"])
    with patch("hokku_server.time_utils.datetime") as mock_dt:
        mock_dt.now.return_value = now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = calculate_sleep_seconds(cfg)
    # 06:00 tomorrow from 23:00 today = 7 h = 25200 s
    assert 25_000 <= result <= 25_400, f"Expected ~25200 s, got {result}"


def test_multiple_slots_picks_nearest_future():
    """With slots at 06:00, 12:00, 18:00 and now=10:00, next slot is 12:00 → ~7200 s."""
    now = _fake_now(10, 0)
    cfg = _cfg(debug_fast_refresh=False, refresh_image_at_time=["0600", "1200", "1800"])
    with patch("hokku_server.time_utils.datetime") as mock_dt:
        mock_dt.now.return_value = now
        result = calculate_sleep_seconds(cfg)
    assert 7_000 <= result <= 7_300, f"Expected ~7200 s, got {result}"


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
