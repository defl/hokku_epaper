"""Tests for hokku webserver."""
import json
import os
import tempfile
import threading
from datetime import datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image
import pytest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import webserver


# ── Config loading tests ───────────────────────────────────────────

class TestConfigLoading:
    def test_default_config(self):
        config = webserver.DEFAULT_CONFIG
        assert "timezone" in config
        assert "refresh_image_at_time" in config
        assert "upload_dir" in config
        assert "cache_dir" in config
        assert "port" in config
        assert "poll_interval_seconds" in config

    def test_load_config_from_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"timezone": "Europe/London", "port": 9090, "poll_interval_seconds": 30}, f)
            temp_path = f.name
        try:
            with patch.dict(os.environ, {"HOKKU_CONFIG": temp_path}):
                config = webserver._load_config()
            assert config["timezone"] == "Europe/London"
            assert config["port"] == 9090
            assert config["poll_interval_seconds"] == 30
        finally:
            os.unlink(temp_path)

    def test_load_config_env_var(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"timezone": "Asia/Tokyo"}, f)
            temp_path = f.name
        try:
            with patch.dict(os.environ, {"HOKKU_CONFIG": temp_path}):
                config = webserver._load_config()
            assert config["timezone"] == "Asia/Tokyo"
        finally:
            os.unlink(temp_path)

    def test_load_config_missing_file(self):
        with patch.dict(os.environ, {"HOKKU_CONFIG": "/nonexistent/config.json"}, clear=False):
            config = webserver._load_config()
        assert config["port"] == webserver.DEFAULT_CONFIG["port"]


# ── Sleep calculation tests ────────────────────────────────────────

class TestSleepCalculation:
    def test_next_time_today(self):
        config = {"timezone": "UTC", "refresh_image_at_time": ["0600", "1200", "1800"]}
        sleep = webserver._calculate_sleep_seconds(config)
        assert sleep >= 60

    def test_empty_times_fallback(self):
        config = {"timezone": "UTC", "refresh_image_at_time": []}
        sleep = webserver._calculate_sleep_seconds(config)
        assert sleep == 21600

    def test_minimum_sleep(self):
        config = {"timezone": "UTC", "refresh_image_at_time": ["0000", "0001", "0002"]}
        sleep = webserver._calculate_sleep_seconds(config)
        assert sleep >= 60

    def test_hhmm_parsing(self):
        config = {"timezone": "UTC", "refresh_image_at_time": ["0930", "1845"]}
        sleep = webserver._calculate_sleep_seconds(config)
        assert isinstance(sleep, int)
        assert sleep >= 60


# ── Duration formatting tests ──────────────────────────────────────

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


# ── Fair distribution tests ────────────────────────────────────────

class TestFairDistribution:
    def _make_pool(self, names):
        return {f"/images/{n}": {"hash": "abc"} for n in names}

    def _make_entry(self, show_index=0, total_show_count=0, total_show_minutes=0.0):
        return {"show_index": show_index, "last_request": None,
                "total_show_count": total_show_count, "total_show_minutes": total_show_minutes}

    def test_picks_lowest_show_index(self):
        pool = self._make_pool(["a.jpg", "b.jpg", "c.jpg"])
        db = {"serve_data": {
            "a.jpg": self._make_entry(show_index=3),
            "b.jpg": self._make_entry(show_index=1),
            "c.jpg": self._make_entry(show_index=5),
        }}
        # Reset _last_served to avoid time tracking issues
        webserver._last_served = {"key": None, "name": None, "served_at": None}
        key = webserver._pick_next_image(pool, db)
        assert Path(key).name == "b.jpg"
        assert db["serve_data"]["b.jpg"]["show_index"] == 2

    def test_new_image_leveling(self):
        pool = self._make_pool(["a.jpg", "b.jpg", "new.jpg"])
        db = {"serve_data": {
            "a.jpg": self._make_entry(show_index=5),
            "b.jpg": self._make_entry(show_index=3),
        }}
        webserver._last_served = {"key": None, "name": None, "served_at": None}
        key = webserver._pick_next_image(pool, db)
        assert Path(key).name == "new.jpg"
        assert db["serve_data"]["a.jpg"]["show_index"] == 1
        assert db["serve_data"]["b.jpg"]["show_index"] == 1
        assert db["serve_data"]["new.jpg"]["show_index"] == 1  # 0 + 1 after serving

    def test_removes_deleted_images(self):
        pool = self._make_pool(["a.jpg"])
        db = {"serve_data": {
            "a.jpg": self._make_entry(show_index=2),
            "deleted.jpg": self._make_entry(show_index=10),
        }}
        webserver._last_served = {"key": None, "name": None, "served_at": None}
        webserver._pick_next_image(pool, db)
        assert "deleted.jpg" not in db["serve_data"]

    def test_empty_pool(self):
        db = {"serve_data": {}}
        key = webserver._pick_next_image({}, db)
        assert key is None

    def test_random_tiebreak(self):
        pool = self._make_pool(["a.jpg", "b.jpg", "c.jpg"])
        seen = set()
        for _ in range(50):
            db = {"serve_data": {
                "a.jpg": self._make_entry(),
                "b.jpg": self._make_entry(),
                "c.jpg": self._make_entry(),
            }}
            webserver._last_served = {"key": None, "name": None, "served_at": None}
            key = webserver._pick_next_image(pool, db)
            seen.add(Path(key).name)
        assert len(seen) >= 2

    def test_negative_show_index(self):
        """show_index can be negative (e.g. from show-next button)."""
        pool = self._make_pool(["a.jpg", "b.jpg"])
        db = {"serve_data": {
            "a.jpg": self._make_entry(show_index=-1),
            "b.jpg": self._make_entry(show_index=3),
        }}
        webserver._last_served = {"key": None, "name": None, "served_at": None}
        key = webserver._pick_next_image(pool, db)
        assert Path(key).name == "a.jpg"

    def test_total_show_count_increments(self):
        pool = self._make_pool(["a.jpg"])
        db = {"serve_data": {"a.jpg": self._make_entry(total_show_count=5)}}
        webserver._last_served = {"key": None, "name": None, "served_at": None}
        webserver._pick_next_image(pool, db)
        assert db["serve_data"]["a.jpg"]["total_show_count"] == 6

    def test_updates_last_request(self):
        pool = self._make_pool(["a.jpg"])
        db = {"serve_data": {"a.jpg": self._make_entry()}}
        webserver._last_served = {"key": None, "name": None, "served_at": None}
        before = datetime.now().isoformat(timespec="seconds")
        webserver._pick_next_image(pool, db)
        after = datetime.now().isoformat(timespec="seconds")
        ts = db["serve_data"]["a.jpg"]["last_request"]
        assert ts is not None
        assert before <= ts <= after


# ── Database persistence tests ─────────────────────────────────────

class TestDatabase:
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            db = {"serve_data": {
                "test.jpg": {"show_index": 3, "last_request": "2026-04-14T12:00:00",
                              "total_show_count": 10, "total_show_minutes": 500.0},
            }}
            webserver._save_database(cache_dir, db)
            loaded = webserver._load_database(cache_dir)
            assert loaded["serve_data"]["test.jpg"]["show_index"] == 3
            assert loaded["serve_data"]["test.jpg"]["total_show_count"] == 10
            assert loaded["serve_data"]["test.jpg"]["total_show_minutes"] == 500.0

    def test_load_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = webserver._load_database(Path(tmpdir))
            assert db == {"serve_data": {}}

    def test_load_corrupt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "database.json").write_text("not json{{{")
            db = webserver._load_database(Path(tmpdir))
            assert db == {"serve_data": {}}

    def test_migrate_show_count_to_show_index(self):
        """Old databases with show_count get migrated to show_index."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = {"serve_data": {"img.jpg": {"show_count": 7, "last_request": None}}}
            (Path(tmpdir) / "database.json").write_text(json.dumps(db))
            loaded = webserver._load_database(Path(tmpdir))
            assert "show_index" in loaded["serve_data"]["img.jpg"]
            assert loaded["serve_data"]["img.jpg"]["show_index"] == 7
            assert "show_count" not in loaded["serve_data"]["img.jpg"]


# ── Flask endpoint tests ──────────────────────────────────────────

class TestFlaskEndpoints:
    @pytest.fixture
    def client(self):
        webserver.app.config["TESTING"] = True
        with webserver.app.test_client() as client:
            yield client

    def test_hokku_no_images(self, client):
        with patch.object(webserver, "_pool", {}), \
             patch.object(webserver, "_converting_count", 0):
            resp = client.get("/hokku/screen/")
            assert resp.status_code == 404

    def test_hokku_converting(self, client):
        with patch.object(webserver, "_pool", {}), \
             patch.object(webserver, "_converting_count", 1):
            resp = client.get("/hokku/screen/")
            assert resp.status_code == 503

    def test_api_status_endpoint(self, client):
        with patch.object(webserver, "_pool", {}), \
             patch.object(webserver, "_database", {"serve_data": {}}), \
             patch.object(webserver, "_config", webserver.DEFAULT_CONFIG), \
             patch.object(webserver, "_converting_count", 0), \
             patch.object(webserver, "_converting_name", None), \
             patch.object(webserver, "_last_served",
                         {"key": None, "name": None, "served_at": None}):
            resp = client.get("/hokku/api/status")
            assert resp.status_code == 200
            data = resp.get_json()
            assert "server_time" in data
            assert "config" in data
            assert "screens" in data

    def test_screen_tracking(self, client):
        """X-Screen-Name header is tracked in database."""
        pool = {"/images/a.jpg": {"hash": "abc"}}
        db = {"serve_data": {"a.jpg": {"show_index": 0, "last_request": None,
              "total_show_count": 0, "total_show_minutes": 0.0}}}
        with patch.object(webserver, "_pool", pool), \
             patch.object(webserver, "_database", db), \
             patch.object(webserver, "_config", webserver.DEFAULT_CONFIG), \
             patch.object(webserver, "_converting_count", 0), \
             patch.object(webserver, "_last_served",
                         {"key": None, "name": None, "served_at": None}), \
             patch("webserver._read_cached_binary", return_value=b"x" * 960000), \
             patch("webserver._save_database"):
            resp = client.get("/hokku/screen/", headers={"X-Screen-Name": "Living Room"})
            assert resp.status_code == 200
            assert "screens" in db
            assert "Living Room" in db["screens"]
            assert db["screens"]["Living Room"]["request_count"] == 1

    def test_api_show_next(self, client):
        db = {"serve_data": {
            "a.jpg": {"show_index": 3, "last_request": None, "total_show_count": 0, "total_show_minutes": 0.0},
            "b.jpg": {"show_index": 1, "last_request": None, "total_show_count": 0, "total_show_minutes": 0.0},
        }}
        with patch.object(webserver, "_database", db), \
             patch.object(webserver, "_config", webserver.DEFAULT_CONFIG), \
             patch("webserver._save_database"):
            resp = client.post("/hokku/api/show_next/a.jpg")
            assert resp.status_code == 200
            assert db["serve_data"]["a.jpg"]["show_index"] == 0  # min(3,1) - 1 = 0

    def test_web_gui_loads(self, client):
        resp = client.get("/hokku/ui")
        assert resp.status_code == 200
        assert b"Hokku" in resp.data


# ── Response-header contracts on /hokku/screen/ ───────────────────
#
# Firmware relies on these headers to schedule the next wake and to
# measure sleep accuracy. Regressions here silently break the frame's
# scheduling behaviour, so lock them in.

class TestScreenHeaders:
    @pytest.fixture
    def client(self):
        webserver.app.config["TESTING"] = True
        with webserver.app.test_client() as client:
            yield client

    def _serve_success_context(self):
        """Context that makes a normal 200 response possible."""
        pool = {"/images/a.jpg": {"hash": "abc"}}
        db = {"serve_data": {"a.jpg": {"show_index": 0, "last_request": None,
              "total_show_count": 0, "total_show_minutes": 0.0}}}
        return pool, db

    def test_success_has_sleep_seconds_and_server_time_epoch(self, client):
        pool, db = self._serve_success_context()
        with patch.object(webserver, "_pool", pool), \
             patch.object(webserver, "_database", db), \
             patch.object(webserver, "_config", webserver.DEFAULT_CONFIG), \
             patch.object(webserver, "_converting_count", 0), \
             patch.object(webserver, "_last_served",
                         {"key": None, "name": None, "served_at": None}), \
             patch("webserver._read_cached_binary", return_value=b"x" * 960000), \
             patch("webserver._save_database"):
            resp = client.get("/hokku/screen/", headers={"X-Screen-Name": "A"})
            assert resp.status_code == 200
            assert "X-Sleep-Seconds" in resp.headers
            assert int(resp.headers["X-Sleep-Seconds"]) > 0
            assert "X-Server-Time-Epoch" in resp.headers
            # Server epoch should be "now" within a reasonable window
            epoch = int(resp.headers["X-Server-Time-Epoch"])
            assert abs(epoch - int(datetime.now().timestamp())) < 30

    def test_busy_503_still_has_sleep_seconds(self, client):
        """Firmware falls back to its 3h default if X-Sleep-Seconds is
        missing on a busy response. Must always be present."""
        with patch.object(webserver, "_pool", {}), \
             patch.object(webserver, "_database", {"serve_data": {}}), \
             patch.object(webserver, "_config", webserver.DEFAULT_CONFIG), \
             patch.object(webserver, "_converting_count", 1), \
             patch("webserver._save_database"):
            resp = client.get("/hokku/screen/", headers={"X-Screen-Name": "A"})
            assert resp.status_code == 503
            assert "X-Sleep-Seconds" in resp.headers
            assert int(resp.headers["X-Sleep-Seconds"]) > 0

    def test_empty_pool_404_still_has_sleep_seconds(self, client):
        with patch.object(webserver, "_pool", {}), \
             patch.object(webserver, "_database", {"serve_data": {}}), \
             patch.object(webserver, "_config", webserver.DEFAULT_CONFIG), \
             patch.object(webserver, "_converting_count", 0), \
             patch("webserver._save_database"):
            resp = client.get("/hokku/screen/", headers={"X-Screen-Name": "A"})
            assert resp.status_code == 404
            assert "X-Sleep-Seconds" in resp.headers
            assert int(resp.headers["X-Sleep-Seconds"]) > 0

    def test_cached_binary_missing_503_still_has_sleep_seconds(self, client):
        """If _read_cached_binary returns None after the pool lookup (cache
        purged between lock-release and read), firmware needs a retry hint."""
        pool, db = self._serve_success_context()
        with patch.object(webserver, "_pool", pool), \
             patch.object(webserver, "_database", db), \
             patch.object(webserver, "_config", webserver.DEFAULT_CONFIG), \
             patch.object(webserver, "_converting_count", 0), \
             patch.object(webserver, "_last_served",
                         {"key": None, "name": None, "served_at": None}), \
             patch("webserver._read_cached_binary", return_value=None), \
             patch("webserver._save_database"):
            resp = client.get("/hokku/screen/", headers={"X-Screen-Name": "A"})
            assert resp.status_code == 503
            assert "X-Sleep-Seconds" in resp.headers


# ── Battery voltage reporting ─────────────────────────────────────
#
# Firmware sends X-Battery-mV with every request; the server captures
# it, computes a percentage against Li-ion anchors, and exposes both
# in /hokku/api/status so the web UI can show "is my battery OK".

class TestBatteryReporting:
    def test_parse_valid_mv(self):
        assert webserver._parse_battery_header("4100") == 4100
        assert webserver._parse_battery_header(" 3800 ") == 3800  # whitespace tolerant
        assert webserver._parse_battery_header("3400") == 3400

    def test_parse_rejects_garbage(self):
        assert webserver._parse_battery_header(None) is None
        assert webserver._parse_battery_header("") is None
        assert webserver._parse_battery_header("nope") is None

    def test_parse_rejects_out_of_range(self):
        """Uninitialised / broken ADC readings shouldn't poison the database."""
        assert webserver._parse_battery_header("0") is None
        assert webserver._parse_battery_header("500") is None  # impossibly low
        assert webserver._parse_battery_header("10000") is None  # impossibly high

    def test_percent_at_anchors(self):
        assert webserver._battery_percent(webserver.BATTERY_MV_FULL) == 100
        assert webserver._battery_percent(webserver.BATTERY_MV_EMPTY) == 0

    def test_percent_linear_midpoint(self):
        # 3400 → 0%, 4100 → 100%. Midpoint 3750 → 50%
        assert webserver._battery_percent(3750) == 50

    def test_percent_clamped_above_full(self):
        """A frame on a higher-than-expected charger (or mis-cal) shouldn't
        show 110%. Clamp to 100."""
        assert webserver._battery_percent(4200) == 100

    def test_percent_clamped_below_empty(self):
        """Below our 0% anchor (3400), clamp to 0 instead of going negative."""
        assert webserver._battery_percent(3200) == 0

    def test_percent_handles_none(self):
        assert webserver._battery_percent(None) is None
        assert webserver._battery_percent(0) is None

    def test_record_screen_call_stores_battery(self):
        db = {"serve_data": {}}
        with patch.object(webserver, "_database", db):
            webserver._record_screen_call("Foyer", "1.2.3.4", 3600, battery_mv=3950)
        entry = db["screens"]["Foyer"]
        assert entry["battery_mv"] == 3950
        assert entry["battery_percent"] == webserver._battery_percent(3950)
        assert entry["battery_seen_at"] is not None

    def test_record_screen_call_without_battery_omits_fields(self):
        """Older firmware that doesn't send the header shouldn't write any
        stale/misleading battery field."""
        db = {"serve_data": {}}
        with patch.object(webserver, "_database", db):
            webserver._record_screen_call("Old", "1.2.3.4", 3600)
        entry = db["screens"]["Old"]
        assert "battery_mv" not in entry
        assert "battery_percent" not in entry

    def test_serve_binary_captures_battery_header(self):
        """End-to-end: X-Battery-mV on the request ends up in the database."""
        webserver.app.config["TESTING"] = True
        pool = {"/images/a.jpg": {"hash": "abc"}}
        db = {"serve_data": {"a.jpg": {"show_index": 0, "last_request": None,
              "total_show_count": 0, "total_show_minutes": 0.0}}}
        with webserver.app.test_client() as client, \
             patch.object(webserver, "_pool", pool), \
             patch.object(webserver, "_database", db), \
             patch.object(webserver, "_config", webserver.DEFAULT_CONFIG), \
             patch.object(webserver, "_converting_count", 0), \
             patch.object(webserver, "_last_served",
                         {"key": None, "name": None, "served_at": None}), \
             patch("webserver._read_cached_binary", return_value=b"x" * 960000), \
             patch("webserver._save_database"):
            resp = client.get("/hokku/screen/", headers={
                "X-Screen-Name": "Foyer",
                "X-Battery-mV": "3912",
            })
            assert resp.status_code == 200
            assert db["screens"]["Foyer"]["battery_mv"] == 3912
            assert db["screens"]["Foyer"]["battery_percent"] is not None

    def test_serve_binary_ignores_bogus_battery_header(self):
        """Garbage header doesn't crash the request."""
        webserver.app.config["TESTING"] = True
        with webserver.app.test_client() as client, \
             patch.object(webserver, "_pool", {}), \
             patch.object(webserver, "_database", {"serve_data": {}}), \
             patch.object(webserver, "_config", webserver.DEFAULT_CONFIG), \
             patch.object(webserver, "_converting_count", 0), \
             patch("webserver._save_database"):
            resp = client.get("/hokku/screen/", headers={
                "X-Screen-Name": "Foyer",
                "X-Battery-mV": "not a number",
            })
            # Still a valid (busy) response; no 500
            assert resp.status_code in (404, 503)


# ── X-Frame-State JSON header ─────────────────────────────────────
#
# Replaces X-Battery-mV in current firmware. Server parses, stores
# whole dict so new firmware keys surface in the UI automatically,
# preserves top-level battery fields for the existing Battery column.

class TestFrameState:
    def test_parse_valid_json(self):
        state = webserver._parse_frame_state(
            '{"fw":"20260418","boot":3,"wake":"timer","bat_mv":4050,"chg":"charging"}')
        assert state["fw"] == "20260418"
        assert state["boot"] == 3
        assert state["wake"] == "timer"
        assert state["bat_mv"] == 4050
        assert state["chg"] == "charging"

    def test_parse_empty_returns_none(self):
        assert webserver._parse_frame_state(None) is None
        assert webserver._parse_frame_state("") is None

    def test_parse_malformed_json_returns_none(self):
        """Garbage from a misbehaving frame must not crash serve_binary."""
        assert webserver._parse_frame_state("{broken") is None
        assert webserver._parse_frame_state("not json at all") is None

    def test_parse_non_object_returns_none(self):
        """JSON that's valid but not an object (array, number, string)
        shouldn't be stored as-is."""
        assert webserver._parse_frame_state("[1,2,3]") is None
        assert webserver._parse_frame_state("42") is None
        assert webserver._parse_frame_state('"hello"') is None

    def test_record_stores_whole_state_dict(self):
        """Forward-compat: any keys firmware adds later show up in the DB
        without a server-side change."""
        db = {"serve_data": {}}
        state = {"fw": "abc", "boot": 7, "wake": "button", "new_future_key": "v2"}
        with patch.object(webserver, "_database", db):
            webserver._record_screen_call("Foo", "1.2.3.4", 60, frame_state=state)
        stored = db["screens"]["Foo"]["state"]
        assert stored["fw"] == "abc"
        assert stored["boot"] == 7
        assert stored["new_future_key"] == "v2"
        assert "seen_at" in stored

    def test_record_pulls_battery_from_state(self):
        """When X-Battery-mV is absent but bat_mv is in X-Frame-State, the
        top-level battery_mv/battery_percent still populate."""
        db = {"serve_data": {}}
        state = {"fw": "abc", "bat_mv": 3950}
        with patch.object(webserver, "_database", db):
            webserver._record_screen_call("Foo", "1.2.3.4", 60,
                                          battery_mv=None, frame_state=state)
        entry = db["screens"]["Foo"]
        assert entry["battery_mv"] == 3950
        assert entry["battery_percent"] == webserver._battery_percent(3950)

    def test_record_state_includes_clk_drift(self):
        """Server computes clk_drift_s = frame's clk_est - server's now."""
        db = {"serve_data": {}}
        import time as _time
        frame_clk = int(_time.time()) + 15  # frame thinks it's 15s in our future
        state = {"fw": "abc", "clk_est": frame_clk}
        with patch.object(webserver, "_database", db):
            webserver._record_screen_call("Foo", "1.2.3.4", 60, frame_state=state)
        drift = db["screens"]["Foo"]["state"]["clk_drift_s"]
        assert 10 <= drift <= 20  # some tolerance for test timing

    def test_record_state_omits_drift_without_clk_est(self):
        db = {"serve_data": {}}
        state = {"fw": "abc"}  # no clk_est
        with patch.object(webserver, "_database", db):
            webserver._record_screen_call("Foo", "1.2.3.4", 60, frame_state=state)
        assert "clk_drift_s" not in db["screens"]["Foo"]["state"]

    def test_serve_binary_captures_frame_state(self):
        """End-to-end: X-Frame-State on the request ends up in the DB."""
        webserver.app.config["TESTING"] = True
        pool = {"/images/a.jpg": {"hash": "abc"}}
        db = {"serve_data": {"a.jpg": {"show_index": 0, "last_request": None,
              "total_show_count": 0, "total_show_minutes": 0.0}}}
        header_json = ('{"fw":"20260418012020Z","boot":3,"wake":"timer",'
                       '"caller":"wake","bat_mv":4100,"chg":"charging",'
                       '"last_sleep":"deep_sleep","rssi":-57,"heap_kb":240,'
                       '"spurious":0,"cfg_ver":1,"clk_est":1776500000}')
        with webserver.app.test_client() as client, \
             patch.object(webserver, "_pool", pool), \
             patch.object(webserver, "_database", db), \
             patch.object(webserver, "_config", webserver.DEFAULT_CONFIG), \
             patch.object(webserver, "_converting_count", 0), \
             patch.object(webserver, "_last_served",
                         {"key": None, "name": None, "served_at": None}), \
             patch("webserver._read_cached_binary", return_value=b"x" * 960000), \
             patch("webserver._save_database"):
            resp = client.get("/hokku/screen/", headers={
                "X-Screen-Name": "Foyer",
                "X-Frame-State": header_json,
            })
            assert resp.status_code == 200
            state = db["screens"]["Foyer"]["state"]
            assert state["fw"] == "20260418012020Z"
            assert state["wake"] == "timer"
            assert state["rssi"] == -57
            assert db["screens"]["Foyer"]["battery_mv"] == 4100

    def test_serve_binary_tolerates_bogus_frame_state(self):
        """A frame sending garbage JSON must not 500."""
        webserver.app.config["TESTING"] = True
        with webserver.app.test_client() as client, \
             patch.object(webserver, "_pool", {}), \
             patch.object(webserver, "_database", {"serve_data": {}}), \
             patch.object(webserver, "_config", webserver.DEFAULT_CONFIG), \
             patch.object(webserver, "_converting_count", 0), \
             patch("webserver._save_database"):
            resp = client.get("/hokku/screen/", headers={
                "X-Screen-Name": "Foyer",
                "X-Frame-State": "not json {{{",
            })
            assert resp.status_code in (404, 503)


# ── Screen-call accounting ────────────────────────────────────────

class TestRecordScreenCall:
    def test_first_call_creates_entry(self):
        db = {"serve_data": {}}
        with patch.object(webserver, "_database", db):
            webserver._record_screen_call("Foyer", "192.168.1.5", 3600,
                                          served_name="a.jpg")
        entry = db["screens"]["Foyer"]
        assert entry["ip"] == "192.168.1.5"
        assert entry["request_count"] == 1
        assert entry["last_seen"] is not None
        assert entry["last_sleep_seconds"] == 3600
        assert entry["next_update_at"] is not None
        assert entry["last_served"] == "a.jpg"

    def test_repeat_call_increments_count_and_updates_next_update(self):
        db = {"serve_data": {}}
        with patch.object(webserver, "_database", db):
            webserver._record_screen_call("Foyer", "192.168.1.5", 60)
            first_next = db["screens"]["Foyer"]["next_update_at"]
            import time as _time
            _time.sleep(1.1)  # ensure the second ISO timestamp differs
            webserver._record_screen_call("Foyer", "192.168.1.5", 120)
        entry = db["screens"]["Foyer"]
        assert entry["request_count"] == 2
        assert entry["last_sleep_seconds"] == 120
        assert entry["next_update_at"] != first_next

    def test_ip_updated_on_new_network(self):
        """Laptop-style screen with DHCP that hops IPs should be tracked at
        its most recent address, not the first one seen."""
        db = {"serve_data": {}}
        with patch.object(webserver, "_database", db):
            webserver._record_screen_call("Bed", "10.0.0.5", 600)
            webserver._record_screen_call("Bed", "10.0.0.77", 600)
        assert db["screens"]["Bed"]["ip"] == "10.0.0.77"

    def test_served_name_optional(self):
        """_record_screen_call is also used on the busy path where no image
        is served yet — must not crash and must not set last_served."""
        db = {"serve_data": {}}
        with patch.object(webserver, "_database", db):
            webserver._record_screen_call("A", "1.2.3.4", 60)
        assert "last_served" not in db["screens"]["A"]


# ── Busy-retry sleep hint ─────────────────────────────────────────

class TestBusyRetrySeconds:
    def test_caps_at_five_minutes(self):
        """Server-computed next refresh far in the future (e.g. 6h away)
        must still return at most 300s so the screen comes back soon
        when conversion finishes."""
        # 300s cap is min(300, delta-to-next-refresh). Use a far-future schedule.
        config = {"timezone": "UTC", "refresh_image_at_time": ["0600"]}
        with patch.object(webserver, "_config", config):
            val = webserver._busy_retry_seconds()
        assert 60 <= val <= 300

    def test_honours_next_scheduled_refresh_when_closer(self):
        """If the next scheduled refresh is in 90s, we return something near
        90s (not 300s) so the screen doesn't overshoot its schedule."""
        # Hack: set refresh_at a minute in the future relative to now.
        # Use a schedule that's only a minute or two away via the UTC now.
        # Easier: verify val never exceeds _calculate_sleep_seconds.
        config = {"timezone": "UTC", "refresh_image_at_time": ["0600", "1200", "1800"]}
        with patch.object(webserver, "_config", config):
            normal = webserver._calculate_sleep_seconds(config)
            busy = webserver._busy_retry_seconds()
        assert busy == min(300, normal)


# ── Upload endpoint ───────────────────────────────────────────────
#
# Added in v2.1 — replaces the "drop a file into the upload dir over
# Samba" workflow with a drag-and-drop POST. Needs to sanitize names,
# reject unsupported extensions, and avoid clobbering existing files.

class TestUpload:
    @pytest.fixture
    def client_with_upload_dir(self, tmp_path):
        # Source files live in a sibling dir so they don't pre-populate
        # the upload dir and trigger spurious collision-suffixing.
        webserver.app.config["TESTING"] = True
        upload_dir = tmp_path / "upload"; upload_dir.mkdir()
        src_dir = tmp_path / "src"; src_dir.mkdir()
        cfg = {**webserver.DEFAULT_CONFIG,
               "upload_dir": str(upload_dir),
               "cache_dir": str(tmp_path / "cache")}
        with patch.object(webserver, "_config", cfg), \
             patch("webserver._sync_pool"), \
             webserver.app.test_client() as client:
            yield client, upload_dir, src_dir

    def _make_jpeg(self, path):
        Image.new("RGB", (32, 32), (10, 20, 30)).save(path, "JPEG")
        return path.read_bytes()

    def test_upload_single_jpeg_success(self, client_with_upload_dir):
        client, upload_dir, src_dir = client_with_upload_dir
        src = src_dir / "src.jpg"
        data = self._make_jpeg(src)
        resp = client.post("/hokku/api/upload",
                           data={"files": (src.open("rb"), "holiday.jpg")},
                           content_type="multipart/form-data")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["saved"] == ["holiday.jpg"]
        assert (upload_dir / "holiday.jpg").read_bytes() == data

    def test_upload_rejects_unsupported_extension(self, client_with_upload_dir):
        client, upload_dir, _ = client_with_upload_dir
        resp = client.post("/hokku/api/upload",
                           data={"files": (BytesIO(b"not an image"), "virus.exe")},
                           content_type="multipart/form-data")
        assert resp.status_code == 200  # 200 with skipped[], not 4xx
        body = resp.get_json()
        assert body["saved"] == []
        assert len(body["skipped"]) == 1
        assert "unsupported type" in body["skipped"][0]["reason"]
        assert not (upload_dir / "virus.exe").exists()

    def test_upload_collision_suffix(self, client_with_upload_dir):
        client, upload_dir, src_dir = client_with_upload_dir
        # Pre-seed a file with the same name
        existing = upload_dir / "clash.jpg"
        self._make_jpeg(existing)
        src = src_dir / "src.jpg"
        self._make_jpeg(src)
        resp = client.post("/hokku/api/upload",
                           data={"files": (src.open("rb"), "clash.jpg")},
                           content_type="multipart/form-data")
        assert resp.status_code == 200
        saved = resp.get_json()["saved"]
        assert saved == ["clash_1.jpg"]
        assert (upload_dir / "clash.jpg").exists()
        assert (upload_dir / "clash_1.jpg").exists()

    def test_upload_multiple_files(self, client_with_upload_dir):
        client, upload_dir, src_dir = client_with_upload_dir
        a = src_dir / "a.jpg"; self._make_jpeg(a)
        b = src_dir / "b.png"
        Image.new("RGB", (16, 16), (1, 2, 3)).save(b, "PNG")
        resp = client.post("/hokku/api/upload", content_type="multipart/form-data",
                           data={"files": [(a.open("rb"), "a.jpg"), (b.open("rb"), "b.png")]})
        assert resp.status_code == 200
        assert set(resp.get_json()["saved"]) == {"a.jpg", "b.png"}

    def test_upload_no_files_returns_400(self, client_with_upload_dir):
        client, _, _ = client_with_upload_dir
        resp = client.post("/hokku/api/upload", content_type="multipart/form-data", data={})
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_upload_path_traversal_rejected(self, client_with_upload_dir):
        """secure_filename should strip directory components from the uploaded
        filename — no writing outside the upload_dir."""
        client, upload_dir, src_dir = client_with_upload_dir
        src = src_dir / "src.jpg"
        self._make_jpeg(src)
        resp = client.post("/hokku/api/upload", content_type="multipart/form-data",
                           data={"files": (src.open("rb"), "../../../etc/passwd.jpg")})
        assert resp.status_code == 200
        # Whatever name it landed under, it must be inside upload_dir and not
        # match the relative-path traversal attempt.
        escaped = (upload_dir / ".." / ".." / ".." / "etc" / "passwd.jpg").resolve()
        assert not escaped.exists()

    def test_upload_filesystem_readonly_returns_json_error(self, client_with_upload_dir):
        """The whole reason v2.1.1 added OSError handling here. Simulate an
        OSError on mkdir and verify we get a JSON body (not an HTML 500 page
        that the web UI would fail to parse)."""
        client, _, src_dir = client_with_upload_dir
        src = src_dir / "src.jpg"; self._make_jpeg(src)
        from pathlib import Path as _Path
        with patch.object(_Path, "mkdir", side_effect=OSError(30, "Read-only file system")):
            resp = client.post("/hokku/api/upload", content_type="multipart/form-data",
                               data={"files": (src.open("rb"), "x.jpg")})
        assert resp.status_code == 500
        assert "error" in resp.get_json()
        # The old behaviour was a Flask HTML error page — verify JSON, not HTML
        assert resp.is_json


# ── Delete endpoint ───────────────────────────────────────────────

class TestDeleteImage:
    @pytest.fixture
    def client_with_image(self, tmp_path):
        webserver.app.config["TESTING"] = True
        upload_dir = tmp_path / "upload"; upload_dir.mkdir()
        cache_dir = tmp_path / "cache"; cache_dir.mkdir()
        (cache_dir / "thumbs").mkdir()
        # Seed an upload + its thumbnail
        (upload_dir / "victim.jpg").write_bytes(b"fake-image")
        (cache_dir / "thumbs" / "victim_thumb.jpg").write_bytes(b"fake-thumb")
        cfg = {**webserver.DEFAULT_CONFIG,
               "upload_dir": str(upload_dir), "cache_dir": str(cache_dir)}
        with patch.object(webserver, "_config", cfg), \
             patch("webserver._sync_pool"), \
             webserver.app.test_client() as client:
            yield client, upload_dir, cache_dir

    def test_delete_removes_original_and_thumbnail(self, client_with_image):
        client, upload_dir, cache_dir = client_with_image
        resp = client.delete("/hokku/api/image/victim.jpg")
        assert resp.status_code == 200
        assert resp.get_json()["deleted"] == "victim.jpg"
        assert not (upload_dir / "victim.jpg").exists()
        assert not (cache_dir / "thumbs" / "victim_thumb.jpg").exists()

    def test_delete_missing_file_returns_404(self, client_with_image):
        client, _, _ = client_with_image
        resp = client.delete("/hokku/api/image/does-not-exist.jpg")
        assert resp.status_code == 404
        assert "error" in resp.get_json()

    def test_delete_triggers_sync(self, client_with_image):
        """After deletion the background _sync_pool should run so the web
        UI's image grid refreshes without waiting for the watcher."""
        client, _, _ = client_with_image
        with patch("webserver.threading.Thread") as mock_thread:
            resp = client.delete("/hokku/api/image/victim.jpg")
            assert resp.status_code == 200
            # Exactly one Thread(target=_sync_pool) spawned
            assert mock_thread.called
            kwargs = mock_thread.call_args.kwargs
            assert kwargs.get("target") is webserver._sync_pool

    def test_delete_preserves_other_files(self, client_with_image):
        client, upload_dir, _ = client_with_image
        (upload_dir / "keep.jpg").write_bytes(b"other")
        resp = client.delete("/hokku/api/image/victim.jpg")
        assert resp.status_code == 200
        assert (upload_dir / "keep.jpg").exists()


# ── Config + clear-cache + time endpoints ─────────────────────────

class TestConfigEndpoints:
    @pytest.fixture
    def client(self, tmp_path):
        webserver.app.config["TESTING"] = True
        cfg = {**webserver.DEFAULT_CONFIG,
               "upload_dir": str(tmp_path / "upload"),
               "cache_dir": str(tmp_path / "cache")}
        (tmp_path / "upload").mkdir()
        (tmp_path / "cache").mkdir()
        with patch.object(webserver, "_config", cfg), \
             patch("webserver._save_config"), \
             patch("webserver._sync_pool"), \
             webserver.app.test_client() as client:
            yield client, cfg

    def test_config_update_timezone(self, client):
        client_, cfg = client
        resp = client_.post("/hokku/api/config", json={"timezone": "Asia/Tokyo"})
        assert resp.status_code == 200
        assert cfg["timezone"] == "Asia/Tokyo"

    def test_config_update_refresh_times(self, client):
        client_, cfg = client
        resp = client_.post("/hokku/api/config",
                            json={"refresh_image_at_time": ["0700", "1930"]})
        assert resp.status_code == 200
        assert cfg["refresh_image_at_time"] == ["0700", "1930"]

    def test_config_update_poll_interval_minimum(self, client):
        client_, cfg = client
        # poll_interval_seconds < 1 should be rejected (silently ignored)
        client_.post("/hokku/api/config", json={"poll_interval_seconds": 0})
        assert cfg["poll_interval_seconds"] != 0

    def test_config_update_rejects_invalid_orientation(self, client):
        client_, cfg = client
        before = cfg.get("orientation", "landscape")
        client_.post("/hokku/api/config", json={"orientation": "diagonal"})
        assert cfg["orientation"] == before

    def test_config_update_empty_body_400(self, client):
        client_, _ = client
        resp = client_.post("/hokku/api/config", data="", content_type="application/json")
        assert resp.status_code == 400

    def test_config_update_orientation_change_clears_cache(self, client, tmp_path):
        """Orientation change invalidates every dithered binary. The endpoint
        should wipe the cache and re-trigger _sync_pool."""
        client_, cfg = client
        cfg["orientation"] = "landscape"
        with patch("webserver._clear_cache_files") as mock_clear:
            resp = client_.post("/hokku/api/config", json={"orientation": "portrait"})
            assert resp.status_code == 200
            mock_clear.assert_called_once()
        assert cfg["orientation"] == "portrait"

    def test_clear_cache_endpoint(self, client):
        client_, _ = client
        with patch("webserver._clear_cache_files") as mock_clear:
            resp = client_.post("/hokku/api/clear_cache")
            assert resp.status_code == 200
            mock_clear.assert_called_once()

    def test_time_endpoint(self, client):
        client_, _ = client
        resp = client_.get("/hokku/api/time")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "time" in data
        assert "timezone" in data


# ── Image-serving endpoints (thumbnail / original / dithered) ─────

class TestImageServingEndpoints:
    @pytest.fixture
    def client(self, tmp_path):
        webserver.app.config["TESTING"] = True
        upload = tmp_path / "upload"; upload.mkdir()
        cache = tmp_path / "cache"; cache.mkdir()
        cfg = {**webserver.DEFAULT_CONFIG,
               "upload_dir": str(upload), "cache_dir": str(cache)}
        with patch.object(webserver, "_config", cfg), \
             webserver.app.test_client() as client:
            yield client, upload, cache

    def test_thumbnail_404_for_missing_file(self, client):
        client_, _, _ = client
        resp = client_.get("/hokku/api/thumbnail/nope.jpg")
        assert resp.status_code == 404

    def test_original_404_for_missing_file(self, client):
        client_, _, _ = client
        resp = client_.get("/hokku/api/original/nope.jpg")
        assert resp.status_code == 404

    def test_dithered_404_for_missing_file(self, client):
        client_, _, _ = client
        # dithered requires the file to be in _pool — empty pool → 404
        with patch.object(webserver, "_pool", {}):
            resp = client_.get("/hokku/api/dithered/nope.jpg")
        assert resp.status_code == 404

    def test_original_jpeg_served_directly(self, client):
        client_, upload, _ = client
        p = upload / "pic.jpg"
        Image.new("RGB", (100, 100), (200, 100, 50)).save(p, "JPEG")
        resp = client_.get("/hokku/api/original/pic.jpg")
        assert resp.status_code == 200
        assert resp.headers["Content-Type"].startswith("image/jpeg")

    def test_original_converts_non_browser_formats_to_jpeg(self, client):
        """HEIC/TIFF aren't browser-safe — the endpoint transcodes to JPEG
        so <img src=...> works from any client."""
        client_, upload, _ = client
        p = upload / "scan.tiff"
        Image.new("RGB", (100, 100), (200, 100, 50)).save(p, "TIFF")
        resp = client_.get("/hokku/api/original/scan.tiff")
        assert resp.status_code == 200
        # Served as JPEG regardless of source format
        assert resp.headers["Content-Type"] == "image/jpeg"

    def test_thumbnail_generated_on_demand(self, client):
        client_, upload, cache = client
        p = upload / "big.jpg"
        Image.new("RGB", (2000, 1500), (10, 20, 30)).save(p, "JPEG")
        resp = client_.get("/hokku/api/thumbnail/big.jpg")
        assert resp.status_code == 200
        # Thumbnail should be ≤ 300 px on the longest side
        img = Image.open(BytesIO(resp.data))
        assert max(img.size) <= 300


# ── _sync_pool coalescing ─────────────────────────────────────────
#
# v2.1.0 added a rerun-pending flag so changes arriving during a
# running sync are never dropped. This test is a bit clever: we
# patch _sync_pool_inner with something slow-ish, fire a second
# _sync_pool() call mid-execution, and verify _sync_pool_inner
# ran twice.

class TestSyncPoolCoalescing:
    def test_trigger_during_sync_causes_rerun(self):
        """If _sync_pool() is called while another is running, the second
        call should mark _sync_pending and the first should loop once more."""
        call_log = []
        call_started = threading.Event()
        release_first = threading.Event()

        def slow_inner():
            call_log.append("start")
            if len(call_log) == 1:
                # First call: wait for the external trigger before returning
                call_started.set()
                release_first.wait(timeout=5)
            call_log.append("done")

        with patch("webserver._sync_pool_inner", side_effect=slow_inner), \
             patch.object(webserver, "_sync_pending", False), \
             patch.object(webserver, "_sync_lock", threading.Lock()), \
             patch.object(webserver, "_sync_state_lock", threading.Lock()):

            t1 = threading.Thread(target=webserver._sync_pool)
            t1.start()

            assert call_started.wait(timeout=5)
            # First sync is running; trigger a second call
            t2 = threading.Thread(target=webserver._sync_pool)
            t2.start()

            # Let the first one finish; the outer while-loop should re-run
            release_first.set()
            t1.join(timeout=5)
            t2.join(timeout=5)

        # Expected: start → done (first, with pending set) → start → done (rerun)
        # The second external _sync_pool call is a no-op because the lock is held.
        assert call_log.count("start") == 2
        assert call_log.count("done") == 2

    def test_no_concurrent_syncs(self):
        """Two threads calling _sync_pool() simultaneously must serialize —
        _sync_pool_inner must never run twice in parallel."""
        max_concurrent = [0]
        current = [0]
        lock = threading.Lock()

        def tracking_inner():
            with lock:
                current[0] += 1
                max_concurrent[0] = max(max_concurrent[0], current[0])
            import time as _time
            _time.sleep(0.05)
            with lock:
                current[0] -= 1

        with patch("webserver._sync_pool_inner", side_effect=tracking_inner), \
             patch.object(webserver, "_sync_pending", False), \
             patch.object(webserver, "_sync_lock", threading.Lock()), \
             patch.object(webserver, "_sync_state_lock", threading.Lock()):
            threads = [threading.Thread(target=webserver._sync_pool) for _ in range(5)]
            for t in threads: t.start()
            for t in threads: t.join(timeout=5)

        assert max_concurrent[0] == 1


# ── Thumbnail mode-conversion tests ───────────────────────────────

class TestEnsureThumbnail:
    """_ensure_thumbnail must produce a valid JPEG for every PIL mode the
    upload dir might throw at it. Real bug fixed here: PNGs with alpha
    (RGBA / LA / paletted-with-transparency) used to crash with 'cannot
    write mode RGBA as JPEG' and leave no thumbnail."""

    @pytest.fixture
    def tmp_cache(self, tmp_path):
        """Point _config at a fresh temp cache_dir so thumbnails write there."""
        cache_dir = tmp_path / "cache"
        upload_dir = tmp_path / "upload"
        upload_dir.mkdir()
        cfg = {**webserver.DEFAULT_CONFIG,
               "cache_dir": str(cache_dir),
               "upload_dir": str(upload_dir)}
        with patch.object(webserver, "_config", cfg):
            yield {"cache_dir": cache_dir, "upload_dir": upload_dir}

    def _save(self, upload_dir, name, img):
        """Save img into the upload dir; format inferred from extension."""
        path = upload_dir / name
        img.save(path)
        return path

    def _assert_valid_jpeg(self, thumb_path):
        """Thumbnail file must exist and be a decodable JPEG within bounds."""
        assert thumb_path is not None, "_ensure_thumbnail returned None"
        assert thumb_path.exists()
        assert thumb_path.suffix == ".jpg"
        thumb = Image.open(thumb_path)
        assert thumb.format == "JPEG"
        assert thumb.mode == "RGB"
        assert max(thumb.size) <= 300

    def test_rgba_png_does_not_crash(self, tmp_cache):
        """The exact bug from the field: RGBA PNGs used to raise
        OSError('cannot write mode RGBA as JPEG')."""
        img = Image.new("RGBA", (400, 200), (255, 0, 0, 128))
        path = self._save(tmp_cache["upload_dir"], "rgba.png", img)
        self._assert_valid_jpeg(webserver._ensure_thumbnail(path))

    def test_la_grayscale_with_alpha(self, tmp_cache):
        """LA mode (grayscale + alpha) — same alpha-channel issue as RGBA."""
        img = Image.new("LA", (400, 200), (128, 200))
        path = self._save(tmp_cache["upload_dir"], "la.png", img)
        self._assert_valid_jpeg(webserver._ensure_thumbnail(path))

    def test_palette_with_transparency(self, tmp_cache):
        """Paletted PNGs with a tRNS chunk are reported as mode 'P' but
        carry transparency in img.info — must be flattened too."""
        img = Image.new("P", (400, 200))
        img.putpalette([0, 0, 0, 255, 0, 0, 0, 255, 0] + [0] * (256 * 3 - 9))
        img.info["transparency"] = 0
        path = self._save(tmp_cache["upload_dir"], "palette.png", img)
        self._assert_valid_jpeg(webserver._ensure_thumbnail(path))

    def test_palette_no_transparency_converts_to_rgb(self, tmp_cache):
        """Paletted without transparency hits the elif branch (mode != RGB)."""
        img = Image.new("P", (400, 200))
        img.putpalette([0, 0, 0, 200, 100, 50] + [0] * (256 * 3 - 6))
        path = self._save(tmp_cache["upload_dir"], "p.png", img)
        self._assert_valid_jpeg(webserver._ensure_thumbnail(path))

    def test_grayscale_l_mode(self, tmp_cache):
        """Plain grayscale (L) — no alpha, but still needs RGB conversion."""
        img = Image.new("L", (400, 200), 128)
        path = self._save(tmp_cache["upload_dir"], "gray.png", img)
        self._assert_valid_jpeg(webserver._ensure_thumbnail(path))

    def test_rgb_passthrough(self, tmp_cache):
        """Plain RGB JPEG goes through neither conversion branch."""
        img = Image.new("RGB", (400, 200), (10, 20, 30))
        path = self._save(tmp_cache["upload_dir"], "rgb.jpg", img)
        self._assert_valid_jpeg(webserver._ensure_thumbnail(path))

    def test_thumbnail_reused_when_fresh(self, tmp_cache):
        """Second call must return the cached path without rewriting it."""
        img = Image.new("RGB", (400, 200), (10, 20, 30))
        path = self._save(tmp_cache["upload_dir"], "cached.jpg", img)

        thumb1 = webserver._ensure_thumbnail(path)
        mtime1 = thumb1.stat().st_mtime_ns

        thumb2 = webserver._ensure_thumbnail(path)
        assert thumb2 == thumb1
        assert thumb2.stat().st_mtime_ns == mtime1, "Fresh thumbnail should not be rewritten"

    def test_thumbnail_regenerated_when_source_newer(self, tmp_cache):
        """If the source mtime is newer than the cached thumbnail, regenerate."""
        import time as _time
        img = Image.new("RGB", (400, 200), (10, 20, 30))
        path = self._save(tmp_cache["upload_dir"], "stale.jpg", img)

        thumb1 = webserver._ensure_thumbnail(path)
        # Backdate the thumbnail so the source appears newer
        old = thumb1.stat().st_mtime - 100
        os.utime(thumb1, (old, old))

        thumb2 = webserver._ensure_thumbnail(path)
        assert thumb2.stat().st_mtime > old, "Stale thumbnail should be regenerated"


# ── Orientation and padding mask tests ────────────────────────────

class TestOrientation:
    """Test _prepare_canvas produces correct dimensions and masks for both orientations."""

    def _make_image(self, w, h, color=(128, 64, 32)):
        """Create a solid-color test image."""
        return Image.new("RGB", (w, h), color)

    def test_landscape_canvas_dimensions(self):
        """Landscape mode: canvas should be 1200x1600 (native format) after rotation."""
        img = self._make_image(800, 600)
        with patch.object(webserver, "_config", {**webserver.DEFAULT_CONFIG, "orientation": "landscape"}):
            canvas, mask = webserver._prepare_canvas(img)
        # PIL size is (width, height), numpy shape is (height, width)
        assert canvas.size == (webserver.FULL_W, webserver.PANEL_H)  # (1200, 1600)
        assert mask.shape == (webserver.PANEL_H, webserver.FULL_W)   # (1600, 1200)

    def test_portrait_canvas_dimensions(self):
        """Portrait mode: canvas should be 1200x1600 (native format) without rotation."""
        img = self._make_image(600, 800)
        with patch.object(webserver, "_config", {**webserver.DEFAULT_CONFIG, "orientation": "portrait"}):
            canvas, mask = webserver._prepare_canvas(img)
        assert canvas.size == (webserver.FULL_W, webserver.PANEL_H)  # (1200, 1600)
        assert mask.shape == (webserver.PANEL_H, webserver.FULL_W)   # (1600, 1200)

    def test_landscape_padding_mask_pillarbox(self):
        """Landscape: tall image gets pillarbox padding on left and right."""
        # 600x800 image is taller than 4:3, so it gets pillarboxed in 1600x1200
        img = self._make_image(600, 800)
        with patch.object(webserver, "_config", {**webserver.DEFAULT_CONFIG, "orientation": "landscape"}):
            canvas, mask = webserver._prepare_canvas(img)
        # After rotation to 1200x1600: mask should have True (padding) and False (image) regions
        assert mask.any(), "Should have some padding pixels"
        assert not mask.all(), "Should have some image pixels"

    def test_portrait_padding_mask_letterbox(self):
        """Portrait: wide image gets letterbox padding on top and bottom."""
        # 800x600 image is wider than 3:4 portrait, so it gets letterboxed in 1200x1600
        img = self._make_image(800, 600)
        with patch.object(webserver, "_config", {**webserver.DEFAULT_CONFIG, "orientation": "portrait"}):
            canvas, mask = webserver._prepare_canvas(img)
        assert mask.any(), "Should have some padding pixels"
        assert not mask.all(), "Should have some image pixels"

    def test_landscape_exact_fit_no_padding(self):
        """Landscape: 4:3 image fills the canvas exactly — no padding."""
        img = self._make_image(1600, 1200)
        with patch.object(webserver, "_config", {**webserver.DEFAULT_CONFIG, "orientation": "landscape"}):
            canvas, mask = webserver._prepare_canvas(img)
        assert not mask.any(), "Exact 4:3 landscape should have no padding"

    def test_portrait_exact_fit_no_padding(self):
        """Portrait: 3:4 image fills the canvas exactly — no padding."""
        img = self._make_image(1200, 1600)
        with patch.object(webserver, "_config", {**webserver.DEFAULT_CONFIG, "orientation": "portrait"}):
            canvas, mask = webserver._prepare_canvas(img)
        assert not mask.any(), "Exact 3:4 portrait should have no padding"

    def test_padding_forced_white_landscape(self):
        """Landscape: padding pixels in dithered output should be white (palette index 1)."""
        # Use a small non-4:3 image so there IS padding
        img = self._make_image(100, 100, color=(50, 50, 50))
        with patch.object(webserver, "_config", {**webserver.DEFAULT_CONFIG, "orientation": "landscape"}):
            canvas, mask = webserver._prepare_canvas(img)
            canvas_array = webserver._compress_dynamic_range(np.array(canvas, dtype=np.float32))
            canvas_img = Image.fromarray(canvas_array.astype(np.uint8))
            result_idx = webserver._floyd_steinberg_dither(canvas_img)
            result_idx[mask] = 1  # This is what _convert_image does
        # All padding pixels must be palette index 1 (white)
        assert (result_idx[mask] == 1).all(), "All padding pixels should be white in landscape"

    def test_padding_forced_white_portrait(self):
        """Portrait: padding pixels in dithered output should be white (palette index 1)."""
        img = self._make_image(100, 100, color=(50, 50, 50))
        with patch.object(webserver, "_config", {**webserver.DEFAULT_CONFIG, "orientation": "portrait"}):
            canvas, mask = webserver._prepare_canvas(img)
            canvas_array = webserver._compress_dynamic_range(np.array(canvas, dtype=np.float32))
            canvas_img = Image.fromarray(canvas_array.astype(np.uint8))
            result_idx = webserver._floyd_steinberg_dither(canvas_img)
            result_idx[mask] = 1
        assert (result_idx[mask] == 1).all(), "All padding pixels should be white in portrait"

    def test_pool_entries_are_metadata_only(self):
        """Pool entries should only hold metadata (hash), not the 960KB binary or preview PNG.

        Guards against regression to in-memory caching which OOM-killed the server.
        """
        # Simulate a pool entry as produced by _convert_and_store
        entry = {"hash": "abc123"}
        # Should not contain the heavy bytes
        assert "binary" not in entry
        assert "preview_png" not in entry

    def test_cache_key_differs_by_orientation(self):
        """Cache keys must differ between landscape and portrait for same file."""
        path = Path("/images/test.jpg")
        content_hash = "abcdef123456"
        with patch.object(webserver, "_config", {**webserver.DEFAULT_CONFIG, "orientation": "landscape"}):
            key_l = webserver._cache_key(path, content_hash)
        with patch.object(webserver, "_config", {**webserver.DEFAULT_CONFIG, "orientation": "portrait"}):
            key_p = webserver._cache_key(path, content_hash)
        assert key_l != key_p, "Cache keys must differ between orientations"
        assert key_l.endswith("_l")
        assert key_p.endswith("_p")
