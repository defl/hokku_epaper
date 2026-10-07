"""Unit tests for screen_headers: battery_percent, parse_battery_header, parse_frame_state."""

from __future__ import annotations

import pytest

from hokku.webserver.screen_headers import (
    BATTERY_MV_EMPTY,
    BATTERY_MV_FULL,
    OTA_MIN_BATTERY_MV,
    battery_percent,
    ota_battery_too_low,
    parse_battery_header,
    parse_cal_ppm,
    parse_frame_state,
    parse_mac_header,
    parse_screen_model,
    reported_battery_mv,
)

# ── parse_mac_header ──────────────────────────────────────────────────────────


def test_mac_normalises_to_lowercase():
    assert parse_mac_header("AA:BB:CC:DD:EE:FF") == "aa:bb:cc:dd:ee:ff"


def test_mac_passthrough_lowercase():
    assert parse_mac_header("a1:b2:c3:d4:e5:f6") == "a1:b2:c3:d4:e5:f6"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "not-a-mac",
        "aa:bb:cc:dd:ee",
        "aa:bb:cc:dd:ee:ff:00",
        "aabbccddeeff",
        "gg:bb:cc:dd:ee:ff",
        "00:00:00:00:00:00",
    ],
)
def test_mac_rejects_malformed(raw):
    assert parse_mac_header(raw) is None


# ── parse_cal_ppm ─────────────────────────────────────────────────────────────


def test_cal_ppm_valid():
    assert parse_cal_ppm(12000) == 12000
    assert parse_cal_ppm(-8000) == -8000
    assert parse_cal_ppm(0) == 0


@pytest.mark.parametrize("raw", [None, "x", True, 200000, -200000])
def test_cal_ppm_rejects_bad(raw):
    assert parse_cal_ppm(raw) is None


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
    bp_3700 = battery_percent(3700)
    bp_3900 = battery_percent(3900)
    assert bp_3700 is not None and bp_3900 is not None
    assert 0 <= bp_3700 <= 100
    assert 0 <= bp_3900 <= 100


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


# ── reported_battery_mv / ota_battery_too_low ─────────────────────────────────


def test_reported_battery_prefers_plausible_frame_state():
    assert reported_battery_mv(3900, {"bat_mv": 3700}) == 3700


def test_reported_battery_ignores_implausible_frame_state():
    assert reported_battery_mv(3900, {"bat_mv": 0}) == 3900
    assert reported_battery_mv(None, {"bat_mv": 0}) is None


def test_reported_battery_without_frame_state():
    assert reported_battery_mv(3900, None) == 3900
    assert reported_battery_mv(None, None) is None


def test_ota_battery_threshold_is_the_dashboards_empty_point():
    assert OTA_MIN_BATTERY_MV == BATTERY_MV_EMPTY


def test_ota_battery_too_low():
    assert ota_battery_too_low(2718) is True
    assert ota_battery_too_low(OTA_MIN_BATTERY_MV - 1) is True
    assert ota_battery_too_low(OTA_MIN_BATTERY_MV) is False
    assert ota_battery_too_low(4000) is False


def test_ota_battery_missing_reading_does_not_hold():
    assert ota_battery_too_low(None) is False


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
    assert result is not None
    assert result["outer"] == {"inner": 1}


# ── parse_screen_model ────────────────────────────────────────────────────────


def test_parse_screen_model_valid_huessen():
    assert parse_screen_model("huessen_epf1301") == "huessen_epf1301"


def test_parse_screen_model_valid_bigme():
    assert parse_screen_model("bigme_f7") == "bigme_f7"


def test_parse_screen_model_with_whitespace():
    assert parse_screen_model("  bigme_f7  ") == "bigme_f7"


def test_parse_screen_model_none_returns_none():
    assert parse_screen_model(None) is None


def test_parse_screen_model_empty_returns_none():
    assert parse_screen_model("") is None


def test_parse_screen_model_unknown_returns_none():
    assert parse_screen_model("nonexistent_screen") is None
