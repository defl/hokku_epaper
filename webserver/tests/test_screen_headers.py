"""Unit tests for screen_headers: battery_percent, parse_battery_header, parse_frame_state."""
from __future__ import annotations

import pytest

from hokku_server.screen_headers import (
    BATTERY_MV_EMPTY,
    BATTERY_MV_FULL,
    battery_percent,
    parse_battery_header,
    parse_frame_state,
)


# ── battery_percent ───────────────────────────────────────────────────────────

def test_battery_percent_at_empty():
    assert battery_percent(BATTERY_MV_EMPTY) == 0


def test_battery_percent_at_full():
    assert battery_percent(BATTERY_MV_FULL) == 100


def test_battery_percent_midpoint():
    mid = (BATTERY_MV_EMPTY + BATTERY_MV_FULL) // 2
    assert battery_percent(mid) == pytest.approx(50, abs=1)


def test_battery_percent_below_empty_clamps_to_zero():
    assert battery_percent(BATTERY_MV_EMPTY - 100) == 0


def test_battery_percent_above_full_clamps_to_100():
    assert battery_percent(BATTERY_MV_FULL + 100) == 100


def test_battery_percent_none_returns_none():
    assert battery_percent(None) is None


def test_battery_percent_zero_returns_none():
    assert battery_percent(0) is None


def test_battery_percent_negative_returns_none():
    assert battery_percent(-1) is None


def test_battery_percent_typical_values():
    assert 0 <= battery_percent(3700) <= 100
    assert 0 <= battery_percent(3900) <= 100


# ── parse_battery_header ──────────────────────────────────────────────────────

def test_parse_battery_header_valid():
    assert parse_battery_header("3800") == 3800


def test_parse_battery_header_with_whitespace():
    assert parse_battery_header("  4000  ") == 4000


def test_parse_battery_header_none_returns_none():
    assert parse_battery_header(None) is None


def test_parse_battery_header_empty_returns_none():
    assert parse_battery_header("") is None


def test_parse_battery_header_non_numeric_returns_none():
    assert parse_battery_header("not-a-number") is None


def test_parse_battery_header_below_min_returns_none():
    assert parse_battery_header("1999") is None


def test_parse_battery_header_above_max_returns_none():
    assert parse_battery_header("5001") is None


def test_parse_battery_header_at_low_boundary():
    assert parse_battery_header("2000") == 2000


def test_parse_battery_header_at_high_boundary():
    assert parse_battery_header("5000") == 5000


def test_parse_battery_header_float_string_returns_none():
    # Only integer strings accepted.
    assert parse_battery_header("3800.5") is None


# ── parse_frame_state ─────────────────────────────────────────────────────────

def test_parse_frame_state_valid_dict():
    raw = '{"mode": "USB_AWAKE", "uptime": 123}'
    result = parse_frame_state(raw)
    assert result == {"mode": "USB_AWAKE", "uptime": 123}


def test_parse_frame_state_none_returns_none():
    assert parse_frame_state(None) is None


def test_parse_frame_state_empty_string_returns_none():
    assert parse_frame_state("") is None


def test_parse_frame_state_invalid_json_returns_none():
    assert parse_frame_state("{not valid json}") is None


def test_parse_frame_state_json_array_returns_none():
    """Top-level JSON array is not a dict — must be rejected."""
    assert parse_frame_state('["a", "b"]') is None


def test_parse_frame_state_json_string_returns_none():
    assert parse_frame_state('"just a string"') is None


def test_parse_frame_state_json_number_returns_none():
    assert parse_frame_state("42") is None


def test_parse_frame_state_empty_object():
    result = parse_frame_state("{}")
    assert result == {}


def test_parse_frame_state_nested_dict():
    raw = '{"outer": {"inner": 1}}'
    result = parse_frame_state(raw)
    assert result["outer"] == {"inner": 1}
