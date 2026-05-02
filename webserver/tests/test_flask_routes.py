"""Core Flask routes: /hokku/screen/, /hokku/api/status, GUI, show-next."""
from dataclasses import replace
from datetime import datetime
from unittest.mock import patch

import pytest

import webserver


class TestFlaskEndpoints:
    @pytest.fixture
    def client(self):
        webserver.app.config["TESTING"] = True
        with webserver.app.test_client() as client:
            yield client

    def test_hokku_no_images(self, client):
        with patch.object(webserver.flask_app, "_pool", {}), \
             patch.object(webserver.flask_app, "_converting_count", 0):
            resp = client.get("/hokku/screen/")
            assert resp.status_code == 404

    def test_hokku_converting(self, client):
        with patch.object(webserver.flask_app, "_pool", {}), \
             patch.object(webserver.flask_app, "_converting_count", 1):
            resp = client.get("/hokku/screen/")
            assert resp.status_code == 503

    def test_api_status_endpoint(self, client):
        with patch.object(webserver.flask_app, "_pool", {}), \
             patch.object(webserver.flask_app, "_database", {"serve_data": {}}), \
             patch.object(webserver.flask_app, "_config", replace(webserver.DEFAULT_CONFIG)), \
             patch.object(webserver.flask_app, "_converting_count", 0), \
             patch.object(webserver.flask_app, "_converting_name", None), \
             patch.object(webserver.flask_app, "_last_served",
                         {"key": None, "name": None, "served_at": None}):
            resp = client.get("/hokku/api/status")
            assert resp.status_code == 200
            data = resp.get_json()
            assert "server_time" in data
            assert "config" in data
            assert "screens" in data

    def test_screen_tracking(self, client):
        pool = {"/images/a.jpg": {"hash": "abc"}}
        db = {"serve_data": {"a.jpg": {"show_index": 0, "last_request": None,
              "total_show_count": 0, "total_show_minutes": 0.0}}}
        with patch.object(webserver.flask_app, "_pool", pool), \
             patch.object(webserver.flask_app, "_database", db), \
             patch.object(webserver.flask_app, "_config", replace(webserver.DEFAULT_CONFIG)), \
             patch.object(webserver.flask_app, "_converting_count", 0), \
             patch.object(webserver.flask_app, "_last_served",
                         {"key": None, "name": None, "served_at": None}), \
             patch("webserver.flask_app._read_cached_binary", return_value=b"x" * 960000), \
             patch("webserver.serve_data._save_database"):
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
        with patch.object(webserver.flask_app, "_database", db), \
             patch.object(webserver.flask_app, "_config", replace(webserver.DEFAULT_CONFIG)), \
             patch("webserver.serve_data._save_database"):
            resp = client.post("/hokku/api/show_next/a.jpg")
            assert resp.status_code == 200
            assert db["serve_data"]["a.jpg"]["show_index"] == 0

    def test_web_gui_loads(self, client):
        resp = client.get("/hokku/ui")
        assert resp.status_code == 200
        assert b"Hokku" in resp.data


class TestScreenHeaders:
    @pytest.fixture
    def client(self):
        webserver.app.config["TESTING"] = True
        with webserver.app.test_client() as client:
            yield client

    def _serve_success_context(self):
        pool = {"/images/a.jpg": {"hash": "abc"}}
        db = {"serve_data": {"a.jpg": {"show_index": 0, "last_request": None,
              "total_show_count": 0, "total_show_minutes": 0.0}}}
        return pool, db

    def test_success_has_sleep_seconds_and_server_time_epoch(self, client):
        pool, db = self._serve_success_context()
        with patch.object(webserver.flask_app, "_pool", pool), \
             patch.object(webserver.flask_app, "_database", db), \
             patch.object(webserver.flask_app, "_config", replace(webserver.DEFAULT_CONFIG)), \
             patch.object(webserver.flask_app, "_converting_count", 0), \
             patch.object(webserver.flask_app, "_last_served",
                         {"key": None, "name": None, "served_at": None}), \
             patch("webserver.flask_app._read_cached_binary", return_value=b"x" * 960000), \
             patch("webserver.serve_data._save_database"):
            resp = client.get("/hokku/screen/", headers={"X-Screen-Name": "A"})
            assert resp.status_code == 200
            assert "X-Sleep-Seconds" in resp.headers
            assert int(resp.headers["X-Sleep-Seconds"]) > 0
            assert "X-Server-Time-Epoch" in resp.headers
            epoch = int(resp.headers["X-Server-Time-Epoch"])
            assert abs(epoch - int(datetime.now().timestamp())) < 30

    def test_busy_503_still_has_sleep_seconds(self, client):
        with patch.object(webserver.flask_app, "_pool", {}), \
             patch.object(webserver.flask_app, "_database", {"serve_data": {}}), \
             patch.object(webserver.flask_app, "_config", replace(webserver.DEFAULT_CONFIG)), \
             patch.object(webserver.flask_app, "_converting_count", 1), \
             patch("webserver.serve_data._save_database"):
            resp = client.get("/hokku/screen/", headers={"X-Screen-Name": "A"})
            assert resp.status_code == 503
            assert "X-Sleep-Seconds" in resp.headers
            assert int(resp.headers["X-Sleep-Seconds"]) > 0

    def test_empty_pool_404_still_has_sleep_seconds(self, client):
        with patch.object(webserver.flask_app, "_pool", {}), \
             patch.object(webserver.flask_app, "_database", {"serve_data": {}}), \
             patch.object(webserver.flask_app, "_config", replace(webserver.DEFAULT_CONFIG)), \
             patch.object(webserver.flask_app, "_converting_count", 0), \
             patch("webserver.serve_data._save_database"):
            resp = client.get("/hokku/screen/", headers={"X-Screen-Name": "A"})
            assert resp.status_code == 404
            assert "X-Sleep-Seconds" in resp.headers
            assert int(resp.headers["X-Sleep-Seconds"]) > 0

    def test_cached_binary_missing_503_still_has_sleep_seconds(self, client):
        pool, db = self._serve_success_context()
        with patch.object(webserver.flask_app, "_pool", pool), \
             patch.object(webserver.flask_app, "_database", db), \
             patch.object(webserver.flask_app, "_config", replace(webserver.DEFAULT_CONFIG)), \
             patch.object(webserver.flask_app, "_converting_count", 0), \
             patch.object(webserver.flask_app, "_last_served",
                         {"key": None, "name": None, "served_at": None}), \
             patch("webserver.flask_app._read_cached_binary", return_value=None), \
             patch("webserver.serve_data._save_database"):
            resp = client.get("/hokku/screen/", headers={"X-Screen-Name": "A"})
            assert resp.status_code == 503
            assert "X-Sleep-Seconds" in resp.headers
