"""Sleep schedule, human durations, busy-retry hint."""
from dataclasses import replace
from unittest.mock import patch

import webserver


class TestSleepCalculation:
    def test_next_time_today(self):
        config = replace(
            webserver.DEFAULT_CONFIG,
            timezone="UTC",
            refresh_image_at_time=["0600", "1200", "1800"],
        )
        sleep = webserver._calculate_sleep_seconds(config)
        assert sleep >= 60

    def test_empty_times_fallback(self):
        config = replace(webserver.DEFAULT_CONFIG, timezone="UTC", refresh_image_at_time=[])
        sleep = webserver._calculate_sleep_seconds(config)
        assert sleep == 21600

    def test_minimum_sleep(self):
        config = replace(
            webserver.DEFAULT_CONFIG,
            timezone="UTC",
            refresh_image_at_time=["0000", "0001", "0002"],
        )
        sleep = webserver._calculate_sleep_seconds(config)
        assert sleep >= 60

    def test_hhmm_parsing(self):
        config = replace(
            webserver.DEFAULT_CONFIG,
            timezone="UTC",
            refresh_image_at_time=["0930", "1845"],
        )
        sleep = webserver._calculate_sleep_seconds(config)
        assert isinstance(sleep, int)
        assert sleep >= 60


class TestFormatDuration:
    def test_minutes(self):
        assert webserver.format_duration_human(30) == "30m"

    def test_hours(self):
        assert webserver.format_duration_human(90) == "1h 30m"

    def test_hours_exact(self):
        assert webserver.format_duration_human(120) == "2h"

    def test_days(self):
        assert webserver.format_duration_human(60 * 36) == "1d 12h"

    def test_months(self):
        assert webserver.format_duration_human(60 * 24 * 45) == "1mo 15d"

    def test_years(self):
        assert webserver.format_duration_human(60 * 24 * 400) == "1y 1mo"

    def test_zero(self):
        assert webserver.format_duration_human(0) == "0m"

    def test_negative(self):
        assert webserver.format_duration_human(-5) == "0m"


class TestBusyRetrySeconds:
    def test_caps_at_five_minutes(self):
        config = replace(
            webserver.DEFAULT_CONFIG,
            timezone="UTC",
            refresh_image_at_time=["0600"],
        )
        with patch.object(webserver.flask_app, "_config", config):
            val = webserver.flask_app._busy_retry_seconds()
        assert 60 <= val <= 300

    def test_honours_next_scheduled_refresh_when_closer(self):
        config = replace(
            webserver.DEFAULT_CONFIG,
            timezone="UTC",
            refresh_image_at_time=["0600", "1200", "1800"],
        )
        with patch.object(webserver.flask_app, "_config", config):
            normal = webserver._calculate_sleep_seconds(config)
            busy = webserver.flask_app._busy_retry_seconds()
        assert busy == min(300, normal)
