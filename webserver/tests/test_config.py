"""AppConfig: load/save roundtrip, defaults, cache_slug."""
import json
from pathlib import Path

import pytest

from webserver.config import AppConfig
from webserver.presets import DEFAULT_PRESET, PRESET_IMAGE_CONFIGS


def test_defaults():
    cfg = AppConfig()
    assert cfg.orientation == "landscape"
    assert cfg.port == 8080
    assert cfg.image == PRESET_IMAGE_CONFIGS[DEFAULT_PRESET]


def test_cache_slug_changes_with_orientation():
    base = AppConfig()
    rotated = AppConfig(orientation="portrait")
    assert base.cache_slug() != rotated.cache_slug()


def test_cache_slug_invariant_to_port():
    base = AppConfig(port=8080)
    other = AppConfig(port=9999)
    assert base.cache_slug() == other.cache_slug()


def test_save_load_roundtrip(tmp_path: Path):
    cfg = AppConfig(
        upload_dir=str(tmp_path / "uploads"),
        cache_dir=str(tmp_path / "cache"),
        orientation="portrait",
        port=9000,
    )
    p = tmp_path / "config.json"
    cfg.save(p)
    loaded = AppConfig.load(p)
    assert loaded == cfg


def test_load_missing_exits(tmp_path: Path):
    with pytest.raises(SystemExit):
        AppConfig.load(tmp_path / "nope.json")


def test_load_invalid_json_exits(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(SystemExit):
        AppConfig.load(p)


def test_image_field_with_partial_blob_rejected(tmp_path: Path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"image": {"dither": {}}}))
    with pytest.raises(SystemExit):
        AppConfig.load(p)
