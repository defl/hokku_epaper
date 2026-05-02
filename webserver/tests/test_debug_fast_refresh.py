"""Debug fast refresh: sleep bypass + API + /hokku/screen/ header."""
from dataclasses import replace
from unittest.mock import patch

import webserver


class TestDebugFastRefresh:
    def test_calculate_sleep_seconds_bypasses_schedule(self):
        config = replace(
            webserver.DEFAULT_CONFIG,
            timezone="UTC",
            refresh_image_at_time=["0600"],
            debug_fast_refresh=True,
        )
        assert webserver._calculate_sleep_seconds(config) == webserver.DEBUG_FAST_REFRESH_SECONDS

    def test_calculate_sleep_seconds_normal_when_disabled(self):
        config = replace(
            webserver.DEFAULT_CONFIG,
            timezone="UTC",
            refresh_image_at_time=["0600"],
            debug_fast_refresh=False,
        )
        val = webserver._calculate_sleep_seconds(config)
        assert val != webserver.DEBUG_FAST_REFRESH_SECONDS
        assert val >= 60

    def test_api_config_accepts_debug_toggle(self):
        webserver.app.config["TESTING"] = True
        cfg = replace(webserver.DEFAULT_CONFIG, debug_fast_refresh=False)
        with webserver.app.test_client() as client, \
             patch.object(webserver.flask_app, "_config", cfg), \
             patch.object(webserver.AppConfig, "save_to_file"):
            resp = client.post("/hokku/api/config",
                               json={"debug_fast_refresh": True})
            assert resp.status_code == 200
            assert cfg.debug_fast_refresh is True
            assert resp.get_json()["config"]["debug_fast_refresh"] is True

    def test_api_status_exposes_debug_flag(self):
        webserver.app.config["TESTING"] = True
        cfg = replace(webserver.DEFAULT_CONFIG, debug_fast_refresh=True)
        with webserver.app.test_client() as client, \
             patch.object(webserver.flask_app, "_config", cfg), \
             patch.object(webserver.flask_app, "_pool", {}), \
             patch.object(webserver.flask_app, "_database", {"serve_data": {}}), \
             patch.object(webserver.flask_app, "_converting_count", 0), \
             patch.object(webserver.flask_app, "_converting_name", None), \
             patch.object(webserver.flask_app, "_last_served",
                         {"key": None, "name": None, "served_at": None}):
            resp = client.get("/hokku/api/status")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["config"]["debug_fast_refresh"] is True
            assert data["config"]["debug_fast_refresh_seconds"] == webserver.DEBUG_FAST_REFRESH_SECONDS

    def test_api_status_matches_config_after_debug_post(self):
        webserver.app.config["TESTING"] = True
        cfg = replace(webserver.DEFAULT_CONFIG, debug_fast_refresh=False)
        with webserver.app.test_client() as client, \
             patch.object(webserver.flask_app, "_config", cfg), \
             patch.object(webserver.flask_app, "_pool", {}), \
             patch.object(webserver.flask_app, "_database", {"serve_data": {}}), \
             patch.object(webserver.flask_app, "_converting_count", 0), \
             patch.object(webserver.flask_app, "_converting_name", None), \
             patch.object(webserver.flask_app, "_last_served",
                         {"key": None, "name": None, "served_at": None}), \
             patch.object(webserver.AppConfig, "save_to_file"):
            r = client.post("/hokku/api/config",
                            json={"debug_fast_refresh": True})
            assert r.status_code == 200
            r = client.get("/hokku/api/status")
            assert r.status_code == 200
            assert r.get_json()["config"]["debug_fast_refresh"] is True

            r = client.post("/hokku/api/config",
                            json={"debug_fast_refresh": False})
            assert r.status_code == 200
            r = client.get("/hokku/api/status")
            assert r.status_code == 200
            assert r.get_json()["config"]["debug_fast_refresh"] is False

    def test_serve_binary_uses_debug_sleep_seconds(self):
        webserver.app.config["TESTING"] = True
        pool = {"/images/a.jpg": {"hash": "abc"}}
        db = {"serve_data": {"a.jpg": {"show_index": 0, "last_request": None,
              "total_show_count": 0, "total_show_minutes": 0.0}}}
        cfg = replace(webserver.DEFAULT_CONFIG, debug_fast_refresh=True)
        with webserver.app.test_client() as client, \
             patch.object(webserver.flask_app, "_pool", pool), \
             patch.object(webserver.flask_app, "_database", db), \
             patch.object(webserver.flask_app, "_config", cfg), \
             patch.object(webserver.flask_app, "_converting_count", 0), \
             patch.object(webserver.flask_app, "_last_served",
                         {"key": None, "name": None, "served_at": None}), \
             patch("webserver.flask_app._read_cached_binary", return_value=b"x" * 960000), \
             patch("webserver.serve_data._save_database"):
            resp = client.get("/hokku/screen/", headers={"X-Screen-Name": "A"})
            assert resp.status_code == 200
            assert int(resp.headers["X-Sleep-Seconds"]) == webserver.DEBUG_FAST_REFRESH_SECONDS
