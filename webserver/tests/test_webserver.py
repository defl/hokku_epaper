"""Tests for hokku webserver."""
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

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
        return {f"/images/{n}": {"binary": b"x", "preview_png": b"x", "hash": "abc"} for n in names}

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
        webserver._last_served = {"key": None, "name": None, "binary": None, "preview_png": None, "served_at": None}
        key = webserver._pick_next_image(pool, db)
        assert Path(key).name == "b.jpg"
        assert db["serve_data"]["b.jpg"]["show_index"] == 2

    def test_new_image_leveling(self):
        pool = self._make_pool(["a.jpg", "b.jpg", "new.jpg"])
        db = {"serve_data": {
            "a.jpg": self._make_entry(show_index=5),
            "b.jpg": self._make_entry(show_index=3),
        }}
        webserver._last_served = {"key": None, "name": None, "binary": None, "preview_png": None, "served_at": None}
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
        webserver._last_served = {"key": None, "name": None, "binary": None, "preview_png": None, "served_at": None}
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
            webserver._last_served = {"key": None, "name": None, "binary": None, "preview_png": None, "served_at": None}
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
        webserver._last_served = {"key": None, "name": None, "binary": None, "preview_png": None, "served_at": None}
        key = webserver._pick_next_image(pool, db)
        assert Path(key).name == "a.jpg"

    def test_total_show_count_increments(self):
        pool = self._make_pool(["a.jpg"])
        db = {"serve_data": {"a.jpg": self._make_entry(total_show_count=5)}}
        webserver._last_served = {"key": None, "name": None, "binary": None, "preview_png": None, "served_at": None}
        webserver._pick_next_image(pool, db)
        assert db["serve_data"]["a.jpg"]["total_show_count"] == 6

    def test_updates_last_request(self):
        pool = self._make_pool(["a.jpg"])
        db = {"serve_data": {"a.jpg": self._make_entry()}}
        webserver._last_served = {"key": None, "name": None, "binary": None, "preview_png": None, "served_at": None}
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
                         {"key": None, "name": None, "binary": None, "preview_png": None, "served_at": None}):
            resp = client.get("/hokku/api/status")
            assert resp.status_code == 200
            data = resp.get_json()
            assert "server_time" in data
            assert "config" in data
            assert "screens" in data

    def test_screen_tracking(self, client):
        """X-Screen-Name header is tracked in database."""
        pool = {"/images/a.jpg": {"binary": b"x" * 960000, "preview_png": b"png", "hash": "abc"}}
        db = {"serve_data": {"a.jpg": {"show_index": 0, "last_request": None,
              "total_show_count": 0, "total_show_minutes": 0.0}}}
        with patch.object(webserver, "_pool", pool), \
             patch.object(webserver, "_database", db), \
             patch.object(webserver, "_config", webserver.DEFAULT_CONFIG), \
             patch.object(webserver, "_converting_count", 0), \
             patch.object(webserver, "_last_served",
                         {"key": None, "name": None, "binary": None, "preview_png": None, "served_at": None}), \
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
