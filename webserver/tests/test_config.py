"""Config load/save and /hokku/api/config endpoints."""
import json
import os
import tempfile
from dataclasses import asdict, replace
from unittest.mock import patch

import pytest

import webserver
from webserver.image import merge_dither_slim, merge_image


class TestConfigLoading:
    def test_default_config(self):
        config = webserver.DEFAULT_CONFIG
        assert config.timezone == "America/Chicago"
        assert config.refresh_image_at_time == ["0600", "1200", "1800"]
        assert config.upload_dir == "/images/upload"
        assert config.cache_dir == "/images/cache"
        assert config.port == 8080
        assert config.poll_interval_seconds == 10
        assert config.debug_fast_refresh is False
        assert config.image == webserver.PRESET_DITHER_ALGORITHMS["atkinson_hue_aware"]

    def test_load_config_from_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"timezone": "Europe/London", "port": 9090, "poll_interval_seconds": 30}, f)
            temp_path = f.name
        try:
            with patch.dict(os.environ, {"HOKKU_CONFIG": temp_path}):
                config = webserver.AppConfig.load_from_file()
            assert config.timezone == "Europe/London"
            assert config.port == 9090
            assert config.poll_interval_seconds == 30
        finally:
            os.unlink(temp_path)

    def test_load_config_env_var(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"timezone": "Asia/Tokyo"}, f)
            temp_path = f.name
        try:
            with patch.dict(os.environ, {"HOKKU_CONFIG": temp_path}):
                config = webserver.AppConfig.load_from_file()
            assert config.timezone == "Asia/Tokyo"
        finally:
            os.unlink(temp_path)

    def test_load_config_missing_file(self):
        with patch.dict(os.environ, {"HOKKU_CONFIG": "/nonexistent/config.json"}, clear=False):
            config = webserver.AppConfig.load_from_file()
        assert config.port == webserver.DEFAULT_CONFIG.port

    def test_load_config_requires_hokku_config_env(self):
        env = {k: v for k, v in os.environ.items() if k != "HOKKU_CONFIG"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ValueError, match="HOKKU_CONFIG"):
                webserver.AppConfig.load_from_file()

    def test_load_from_file_explicit_path_without_hokku_config_env(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"timezone": "UTC", "port": 5000}, f)
            temp_path = f.name
        try:
            env = {k: v for k, v in os.environ.items() if k != "HOKKU_CONFIG"}
            with patch.dict(os.environ, env, clear=True):
                config = webserver.AppConfig.load_from_file(temp_path)
            assert config.timezone == "UTC"
            assert config.port == 5000
        finally:
            os.unlink(temp_path)

    def test_save_and_load_round_trip(self, tmp_path):
        path = tmp_path / "hokku.json"
        original = replace(
            webserver.AppConfig(),
            timezone="Europe/Berlin",
            port=7777,
            image=replace(
                webserver.DEFAULT_CONFIG.image,
                dither=replace(webserver.DEFAULT_CONFIG.image.dither, serpentine=True),
            ),
        )
        original.save_to_file(path)
        loaded = webserver.AppConfig.load_from_file(path)
        assert loaded.timezone == "Europe/Berlin"
        assert loaded.port == 7777
        assert loaded.image.dither.serpentine is True
        assert loaded.image == original.image

    def test_save_to_file_requires_path_or_env(self):
        cfg = webserver.AppConfig()
        env = {k: v for k, v in os.environ.items() if k != "HOKKU_CONFIG"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ValueError, match="HOKKU_CONFIG"):
                cfg.save_to_file()


class TestFromDictAndMergeDither:
    def test_from_dict_partial_dither_merges_with_defaults(self):
        cfg = webserver.AppConfig.from_dict({"dither": {"serpentine": True}})
        assert cfg.image.dither.serpentine is True
        assert cfg.image.dither.algorithm == webserver.DEFAULT_CONFIG.image.dither.algorithm
        assert cfg.image.dither.lut_name == webserver.DEFAULT_CONFIG.image.dither.lut_name

    def test_from_dict_nested_dither_takes_precedence_over_legacy_algorithm(self):
        cfg = webserver.AppConfig.from_dict({
            "dither": {"serpentine": False},
            "dither_algorithm": "floyd_steinberg",
        })
        assert cfg.image.dither.serpentine is False
        assert cfg.image.dither.algorithm == webserver.DEFAULT_CONFIG.image.dither.algorithm

    def test_from_dict_legacy_dither_algorithm_and_serpentine(self):
        cfg = webserver.AppConfig.from_dict({
            "dither_algorithm": "stucki",
            "dither_serpentine": True,
        })
        stucki = webserver.PRESET_DITHER_ALGORITHMS["stucki"]
        want = replace(stucki, dither=replace(stucki.dither, serpentine=True))
        assert cfg.image == want

    def test_from_dict_full_image_round_trip_via_asdict(self):
        floyd = webserver.PRESET_DITHER_ALGORITHMS["floyd_steinberg"]
        base = replace(
            webserver.AppConfig(),
            image=replace(floyd, dither=replace(floyd.dither, serpentine=True)),
        )
        raw = asdict(base)
        cfg = webserver.AppConfig.from_dict(raw)
        assert cfg == base

    def test_merge_dither_slim_ignores_unknown_keys(self):
        base = webserver.DEFAULT_CONFIG.image.dither
        merged = merge_dither_slim(base, {"serpentine": True, "extra": 1, "not_a_field": "x"})
        assert merged.serpentine is True
        assert merged.algorithm == base.algorithm

    def test_merge_image_applies_top_level_and_nested_dither(self):
        base = webserver.DEFAULT_CONFIG.image
        merged = merge_image(base, {"color_enhance": 1.1, "dither": {"serpentine": True}})
        assert merged.color_enhance == 1.1
        assert merged.dither.serpentine is True


class TestAppConfigCacheSlug:
    def test_cache_slug_changes_with_orientation_dither_and_debug(self):
        base = webserver.DEFAULT_CONFIG
        assert replace(base, orientation="portrait").cache_slug() != base.cache_slug()
        assert replace(base, debug_fast_refresh=True).cache_slug() != base.cache_slug()
        assert (
            replace(
                base,
                image=replace(
                    base.image,
                    dither=replace(base.image.dither, serpentine=True),
                ),
            ).cache_slug()
            != base.cache_slug()
        )

    def test_cache_slug_ignores_timezone(self):
        a = replace(webserver.DEFAULT_CONFIG, timezone="UTC")
        b = replace(webserver.DEFAULT_CONFIG, timezone="Europe/Oslo")
        assert a.cache_slug() == b.cache_slug()


class TestConfigEndpoints:
    @pytest.fixture
    def client(self, tmp_path):
        webserver.app.config["TESTING"] = True
        cfg = replace(
            webserver.DEFAULT_CONFIG,
            upload_dir=str(tmp_path / "upload"),
            cache_dir=str(tmp_path / "cache"),
        )
        (tmp_path / "upload").mkdir()
        (tmp_path / "cache").mkdir()
        with patch.object(webserver.flask_app, "_config", cfg), \
             patch.object(webserver.AppConfig, "save_to_file"), \
             patch("webserver.flask_app._sync_pool"), \
             webserver.app.test_client() as client:
            yield client, cfg

    def test_config_update_timezone(self, client):
        client_, cfg = client
        resp = client_.post("/hokku/api/config", json={"timezone": "Asia/Tokyo"})
        assert resp.status_code == 200
        assert cfg.timezone == "Asia/Tokyo"

    def test_config_update_refresh_times(self, client):
        client_, cfg = client
        resp = client_.post("/hokku/api/config",
                            json={"refresh_image_at_time": ["0700", "1930"]})
        assert resp.status_code == 200
        assert cfg.refresh_image_at_time == ["0700", "1930"]

    def test_config_update_poll_interval_minimum(self, client):
        client_, cfg = client
        client_.post("/hokku/api/config", json={"poll_interval_seconds": 0})
        assert cfg.poll_interval_seconds != 0

    def test_config_update_rejects_invalid_orientation(self, client):
        client_, cfg = client
        before = cfg.orientation
        client_.post("/hokku/api/config", json={"orientation": "diagonal"})
        assert cfg.orientation == before

    def test_config_update_empty_body_400(self, client):
        client_, _ = client
        resp = client_.post("/hokku/api/config", data="", content_type="application/json")
        assert resp.status_code == 400

    def test_config_update_orientation_change_clears_cache(self, client, tmp_path):
        client_, cfg = client
        cfg.orientation = "landscape"
        with patch("webserver.flask_app._clear_cache_files") as mock_clear:
            resp = client_.post("/hokku/api/config", json={"orientation": "portrait"})
            assert resp.status_code == 200
            mock_clear.assert_called_once()
        assert cfg.orientation == "portrait"

    def test_clear_cache_endpoint(self, client):
        client_, _ = client
        with patch("webserver.flask_app._clear_cache_files") as mock_clear:
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

    def test_config_update_dither_algorithm_applies_preset(self, client):
        client_, cfg = client
        resp = client_.post("/hokku/api/config", json={"dither_algorithm": "floyd_steinberg"})
        assert resp.status_code == 200
        assert cfg.image == webserver.PRESET_DITHER_ALGORITHMS["floyd_steinberg"]

    def test_config_update_dither_preset_key(self, client):
        client_, cfg = client
        resp = client_.post("/hokku/api/config", json={"dither_preset": "atkinson"})
        assert resp.status_code == 200
        assert cfg.image == webserver.PRESET_DITHER_ALGORITHMS["atkinson"]

    def test_config_update_dither_unknown_preset_400(self, client):
        client_, cfg = client
        before = cfg.image
        resp = client_.post("/hokku/api/config", json={"dither_algorithm": "not_a_real_preset"})
        assert resp.status_code == 400
        assert cfg.image == before

    def test_config_update_dither_nested_merge(self, client):
        client_, cfg = client
        resp = client_.post("/hokku/api/config", json={"dither": {"serpentine": True}})
        assert resp.status_code == 200
        assert cfg.image.dither.serpentine is True
        assert cfg.image.dither.algorithm == webserver.DEFAULT_CONFIG.image.dither.algorithm

    def test_config_update_dither_serpentine_only(self, client):
        client_, cfg = client
        assert cfg.image.dither.serpentine is False
        resp = client_.post("/hokku/api/config", json={"dither_serpentine": True})
        assert resp.status_code == 200
        assert cfg.image.dither.serpentine is True

    def test_config_update_dither_preset_with_serpentine(self, client):
        client_, cfg = client
        resp = client_.post("/hokku/api/config", json={
            "dither_algorithm": "stucki",
            "dither_serpentine": True,
        })
        assert resp.status_code == 200
        stucki = webserver.PRESET_DITHER_ALGORITHMS["stucki"]
        want = replace(stucki, dither=replace(stucki.dither, serpentine=True))
        assert cfg.image == want

    def test_config_nested_dither_wins_over_preset_in_same_request(self, client):
        client_, cfg = client
        resp = client_.post("/hokku/api/config", json={
            "dither": {"serpentine": True},
            "dither_algorithm": "floyd_steinberg",
        })
        assert resp.status_code == 200
        assert cfg.image.dither.serpentine is True
        assert cfg.image.dither.algorithm == webserver.DEFAULT_CONFIG.image.dither.algorithm
