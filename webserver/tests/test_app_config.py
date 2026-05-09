"""AppConfig: load/save roundtrip, defaults, cache_slug, version + migrations."""
from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from webserver.app_config import AppConfig, _CURRENT_VERSION, _MIGRATIONS
from webserver.presets import DEFAULT_PRESET, PRESET_IMAGE_CONFIGS


def test_defaults():
    cfg = AppConfig()
    assert cfg.orientation == "landscape"
    assert cfg.port == 8080
    assert cfg.version == _CURRENT_VERSION
    assert cfg.image_config_default == PRESET_IMAGE_CONFIGS[DEFAULT_PRESET]
    assert cfg.classifier_bw_detect_enabled is True
    assert cfg.classifier_face_detect_enabled is True


def test_cache_slug_changes_with_orientation():
    base = AppConfig()
    rotated = AppConfig(orientation="portrait")
    assert base.cache_slug() != rotated.cache_slug()


def test_cache_slug_invariant_to_port():
    base = AppConfig(port=8080)
    other = AppConfig(port=9999)
    assert base.cache_slug() == other.cache_slug()


def test_cache_slug_changes_with_image_config_default():
    base = AppConfig()
    other = AppConfig(image_config_default=PRESET_IMAGE_CONFIGS["floyd_steinberg"])
    assert base.cache_slug() != other.cache_slug()


def test_cache_slug_changes_with_classifier_flags():
    both_off = AppConfig(classifier_bw_detect_enabled=False, classifier_face_detect_enabled=False)
    bw_on  = AppConfig(classifier_bw_detect_enabled=True,  classifier_face_detect_enabled=False)
    face_on = AppConfig(classifier_bw_detect_enabled=False, classifier_face_detect_enabled=True)
    both_on = AppConfig(classifier_bw_detect_enabled=True,  classifier_face_detect_enabled=True)
    assert both_off.cache_slug() != bw_on.cache_slug()
    assert both_off.cache_slug() != face_on.cache_slug()
    assert bw_on.cache_slug() != face_on.cache_slug()
    assert both_on.cache_slug() != both_off.cache_slug()


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


def test_version_written_on_save(tmp_path: Path):
    p = tmp_path / "config.json"
    AppConfig().save(p)
    data = json.loads(p.read_text())
    assert data["version"] == _CURRENT_VERSION


def test_unversioned_config_returns_default(tmp_path: Path):
    """A valid-JSON file without 'version' → from_dict() returns a fresh default."""
    cfg = AppConfig.from_dict({"orientation": "portrait", "port": 9999})
    # No version → default v1 returned, ignoring the other fields.
    assert cfg == AppConfig()


def test_unversioned_config_load_writes_back(tmp_path: Path):
    """AppConfig.load() on an unversioned JSON file writes the default back."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"orientation": "portrait"}))
    cfg = AppConfig.load(p)
    assert cfg == AppConfig()
    # File now has a version field.
    data = json.loads(p.read_text())
    assert data["version"] == _CURRENT_VERSION


def test_all_three_image_configs_roundtrip(tmp_path: Path):
    face_cfg = PRESET_IMAGE_CONFIGS["floyd_steinberg"]
    cfg = AppConfig(
        image_config_default=PRESET_IMAGE_CONFIGS["atkinson_hue_aware"],
        image_config_bw=PRESET_IMAGE_CONFIGS["floyd_steinberg"],
        image_config_face=face_cfg,
        classifier_bw_detect_enabled=True,
        classifier_face_detect_enabled=True,
    )
    p = tmp_path / "config.json"
    cfg.save(p)
    loaded = AppConfig.load(p)
    assert loaded == cfg
    assert loaded.image_config_face == face_cfg
    assert loaded.classifier_bw_detect_enabled is True
    assert loaded.classifier_face_detect_enabled is True


def test_image_field_with_partial_blob_rejected(tmp_path: Path):
    """A corrupt image_config_default blob (partial dither) must fail on parse."""
    p = tmp_path / "c.json"
    p.write_text(json.dumps({
        "version": _CURRENT_VERSION,
        "image_config_default": {"dither": {}},
    }))
    with pytest.raises(SystemExit):
        AppConfig.load(p)


def test_future_version_migration(tmp_path: Path, monkeypatch):
    """Simulate a v1→v2 migration step."""
    # Register a fake migration that sets a new field.
    def _fake_migration(data: dict) -> dict:
        data = dict(data)
        data["_migrated"] = True
        return data

    monkeypatch.setitem(_MIGRATIONS, 1, _fake_migration)
    # Temporarily bump _CURRENT_VERSION to 2.
    import webserver.app_config as _mod
    monkeypatch.setattr(_mod, "_CURRENT_VERSION", 2)
    # Reload so AppConfig picks up the new version.
    monkeypatch.setattr(AppConfig, "__dataclass_fields__", AppConfig.__dataclass_fields__)

    p = tmp_path / "config.json"
    # Write a v1 file.
    p.write_text(json.dumps({"version": 1}))

    # from_dict should walk the migration chain.
    data = json.loads(p.read_text())
    from webserver.app_config import _migrate
    migrated = _migrate(data)
    assert migrated["version"] == 2
    assert migrated["_migrated"] is True
