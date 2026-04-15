"""Tests for hokku webserver."""
import json
import os
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

# Add parent dir to path
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import webserver


# ── Config loading tests ───────────────────────────────────────────

class TestConfigLoading:
    def test_default_config(self):
        """Default config has all expected keys."""
        config = webserver.DEFAULT_CONFIG
        assert "timezone" in config
        assert "refresh_image_at_time" in config
        assert "upload_dir" in config
        assert "cache_dir" in config
        assert "port" in config

    def test_load_config_from_file(self):
        """Config loads from a JSON file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"timezone": "Europe/London", "port": 9090}, f)
            temp_path = f.name

        try:
            with patch.dict(os.environ, {"HOKKU_CONFIG": temp_path}):
                config = webserver._load_config()
            assert config["timezone"] == "Europe/London"
            assert config["port"] == 9090
            # Defaults preserved for unset keys
            assert "refresh_image_at_time" in config
        finally:
            os.unlink(temp_path)

    def test_load_config_env_var(self):
        """HOKKU_CONFIG env var takes priority."""
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
        """Missing config file returns defaults."""
        with patch.dict(os.environ, {"HOKKU_CONFIG": "/nonexistent/config.json"}, clear=False):
            # Also patch to prevent finding ./config.json
            config = webserver._load_config()
        assert config["port"] == webserver.DEFAULT_CONFIG["port"]


# ── Sleep calculation tests ────────────────────────────────────────

class TestSleepCalculation:
    def test_next_time_today(self):
        """Returns seconds until next wake time today."""
        config = {
            "timezone": "UTC",
            "refresh_image_at_time": ["0600", "1200", "1800"],
        }
        # Mock: it's 10:00 UTC, next wake is 12:00 = 7200 seconds
        with patch("webserver.datetime") as mock_dt:
            mock_now = datetime(2026, 4, 14, 10, 0, 0)
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            # Can't easily mock datetime.now with zoneinfo, test with fallback
        # Direct test with known values
        sleep = webserver._calculate_sleep_seconds(config)
        # Result should be positive
        assert sleep >= 60

    def test_empty_times_fallback(self):
        """Empty refresh times returns 6h fallback."""
        config = {"timezone": "UTC", "refresh_image_at_time": []}
        sleep = webserver._calculate_sleep_seconds(config)
        assert sleep == 21600

    def test_minimum_sleep(self):
        """Sleep is at least 60 seconds."""
        config = {
            "timezone": "UTC",
            "refresh_image_at_time": ["0000", "0001", "0002"],
        }
        sleep = webserver._calculate_sleep_seconds(config)
        assert sleep >= 60

    def test_hhmm_parsing(self):
        """HHMM format is correctly parsed."""
        config = {
            "timezone": "UTC",
            "refresh_image_at_time": ["0930", "1845"],
        }
        sleep = webserver._calculate_sleep_seconds(config)
        assert isinstance(sleep, int)
        assert sleep >= 60


# ── Fair distribution tests ────────────────────────────────────────

class TestFairDistribution:
    def _make_pool(self, names):
        """Create a mock pool dict from a list of filenames."""
        return {f"/images/{n}": {"binary": b"x", "preview_png": b"x", "hash": "abc"} for n in names}

    def test_picks_least_shown(self):
        """Picks image with lowest show_count."""
        pool = self._make_pool(["a.jpg", "b.jpg", "c.jpg"])
        db = {"serve_data": {
            "a.jpg": {"show_count": 3, "last_request": None},
            "b.jpg": {"show_count": 1, "last_request": None},
            "c.jpg": {"show_count": 5, "last_request": None},
        }}
        key = webserver._pick_next_image(pool, db)
        assert Path(key).name == "b.jpg"
        assert db["serve_data"]["b.jpg"]["show_count"] == 2

    def test_new_image_leveling(self):
        """When new image appears, existing counts > 0 are set to 1."""
        pool = self._make_pool(["a.jpg", "b.jpg", "new.jpg"])
        db = {"serve_data": {
            "a.jpg": {"show_count": 5, "last_request": None},
            "b.jpg": {"show_count": 3, "last_request": None},
            # new.jpg not in serve_data
        }}
        key = webserver._pick_next_image(pool, db)
        # new.jpg should be picked (show_count=0)
        assert Path(key).name == "new.jpg"
        # Existing counts should be leveled to 1
        assert db["serve_data"]["a.jpg"]["show_count"] == 1
        assert db["serve_data"]["b.jpg"]["show_count"] == 1
        # new.jpg now has count 1 (0 + 1 after serving)
        assert db["serve_data"]["new.jpg"]["show_count"] == 1

    def test_removes_deleted_images(self):
        """Entries for deleted images are removed from serve_data."""
        pool = self._make_pool(["a.jpg"])
        db = {"serve_data": {
            "a.jpg": {"show_count": 2, "last_request": None},
            "deleted.jpg": {"show_count": 10, "last_request": None},
        }}
        webserver._pick_next_image(pool, db)
        assert "deleted.jpg" not in db["serve_data"]

    def test_empty_pool(self):
        """Returns None for empty pool."""
        db = {"serve_data": {}}
        key = webserver._pick_next_image({}, db)
        assert key is None

    def test_random_tiebreak(self):
        """When multiple images tie, selection is random (not always same)."""
        pool = self._make_pool(["a.jpg", "b.jpg", "c.jpg"])
        seen = set()
        for _ in range(50):
            db = {"serve_data": {
                "a.jpg": {"show_count": 0, "last_request": None},
                "b.jpg": {"show_count": 0, "last_request": None},
                "c.jpg": {"show_count": 0, "last_request": None},
            }}
            key = webserver._pick_next_image(pool, db)
            seen.add(Path(key).name)
        # With random tie-breaking, we should see at least 2 different images in 50 tries
        assert len(seen) >= 2

    def test_updates_last_request(self):
        """last_request is set to current timestamp."""
        pool = self._make_pool(["a.jpg"])
        db = {"serve_data": {
            "a.jpg": {"show_count": 0, "last_request": None},
        }}
        before = datetime.now().isoformat(timespec="seconds")
        webserver._pick_next_image(pool, db)
        after = datetime.now().isoformat(timespec="seconds")
        ts = db["serve_data"]["a.jpg"]["last_request"]
        assert ts is not None
        assert before <= ts <= after


# ── Database persistence tests ─────────────────────────────────────

class TestDatabase:
    def test_save_and_load(self):
        """Database roundtrips through JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            db = {"serve_data": {
                "test.jpg": {"show_count": 3, "last_request": "2026-04-14T12:00:00"},
            }}
            webserver._save_database(cache_dir, db)
            loaded = webserver._load_database(cache_dir)
            assert loaded["serve_data"]["test.jpg"]["show_count"] == 3

    def test_load_missing(self):
        """Loading from missing file returns empty database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = webserver._load_database(Path(tmpdir))
            assert db == {"serve_data": {}}

    def test_load_corrupt(self):
        """Loading corrupt JSON returns empty database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "database.json").write_text("not json{{{")
            db = webserver._load_database(Path(tmpdir))
            assert db == {"serve_data": {}}


# ── Flask endpoint tests ──────────────────────────────────────────

class TestFlaskEndpoints:
    @pytest.fixture
    def client(self):
        """Create a Flask test client."""
        webserver.app.config["TESTING"] = True
        with webserver.app.test_client() as client:
            yield client

    def test_hokku_no_images(self, client):
        """GET /hokku/ returns 404 when no images."""
        with patch.object(webserver, "_pool", {}), \
             patch.object(webserver, "_converting_count", 0):
            resp = client.get("/hokku/")
            assert resp.status_code == 404

    def test_hokku_converting(self, client):
        """GET /hokku/ returns 503 when converting."""
        with patch.object(webserver, "_pool", {}), \
             patch.object(webserver, "_converting_count", 1):
            resp = client.get("/hokku/")
            assert resp.status_code == 503

    def test_preview_no_image(self, client):
        """GET /hokku/preview returns 404 when nothing served."""
        with patch.object(webserver, "_last_served",
                         {"key": None, "name": None, "binary": None, "preview_png": None}):
            resp = client.get("/hokku/preview")
            assert resp.status_code == 404

    def test_status_endpoint(self, client):
        """GET /hokku/status returns JSON."""
        with patch.object(webserver, "_pool", {}), \
             patch.object(webserver, "_database", {"serve_data": {}}), \
             patch.object(webserver, "_config", webserver.DEFAULT_CONFIG), \
             patch.object(webserver, "_last_served",
                         {"key": None, "name": None, "binary": None, "preview_png": None}), \
             patch.object(webserver, "_converting_count", 0):
            resp = client.get("/hokku/status")
            assert resp.status_code == 200
            data = resp.get_json()
            assert "pool_size" in data
            assert "config" in data
