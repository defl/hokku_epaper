"""AppConfig: load/save roundtrip, defaults, cache_slug, version + migrations."""
from __future__ import annotations

import contextlib
import io
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from hokku_server.app_config import AppConfig, _CURRENT_VERSION, _MIGRATIONS, _migrate
from hokku_server.presets import DEFAULT_PRESET, PRESET_IMAGE_CONFIGS


def test_defaults():
    cfg = AppConfig()
    assert cfg.orientation == "landscape"
    assert cfg.port == 8080
    assert cfg.version == _CURRENT_VERSION
    assert cfg.image_config_default == PRESET_IMAGE_CONFIGS[DEFAULT_PRESET]
    assert cfg.classifier_bw_detect_enabled is True


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
    bw_off = AppConfig(classifier_bw_detect_enabled=False)
    bw_on = AppConfig(classifier_bw_detect_enabled=True)
    assert bw_off.cache_slug() != bw_on.cache_slug()


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


def test_load_missing_creates_default(tmp_path: Path):
    p = tmp_path / "nope.json"
    cfg = AppConfig.load(p)
    assert p.exists()
    assert isinstance(cfg, AppConfig)
    assert cfg.version == AppConfig().version


def test_load_invalid_json_exits(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with contextlib.redirect_stderr(io.StringIO()):
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


def test_image_configs_roundtrip(tmp_path: Path):
    cfg = AppConfig(
        image_config_default=PRESET_IMAGE_CONFIGS["atkinson_hue_aware"],
        image_config_bw=PRESET_IMAGE_CONFIGS["floyd_steinberg"],
        classifier_bw_detect_enabled=True,
    )
    p = tmp_path / "config.json"
    cfg.save(p)
    loaded = AppConfig.load(p)
    assert loaded == cfg
    assert loaded.classifier_bw_detect_enabled is True


def test_image_field_with_partial_blob_rejected(tmp_path: Path):
    """A corrupt image_config_default blob (partial dither) must fail on parse."""
    p = tmp_path / "c.json"
    p.write_text(json.dumps({
        "version": _CURRENT_VERSION,
        "image_config_default": {"dither": {}},
    }))
    with contextlib.redirect_stderr(io.StringIO()):
        with pytest.raises(SystemExit):
            AppConfig.load(p)


def test_v1_migrates_to_v2():
    """A v1 dict (no image_worker_thread_count) is migrated forward and gains the v2 field with default 1."""
    v1_blob = {"version": 1}  # minimal valid v1

    migrated = _migrate(v1_blob)
    assert migrated["version"] == _CURRENT_VERSION
    assert migrated["image_worker_thread_count"] == 1


def test_v2_migrates_forward():
    """A v2 dict migrates all the way to current version without error."""
    v2_blob = {"version": 2, "image_worker_thread_count": 1}

    migrated = _migrate(v2_blob)
    assert migrated["version"] == _CURRENT_VERSION
    # face_detector was added in v2→v3 then removed in v4→v5; must not survive.
    assert "face_detector" not in migrated


def test_image_worker_thread_count_roundtrips(tmp_path: Path):
    """image_worker_thread_count is written to and read from JSON."""
    cfg = AppConfig(image_worker_thread_count=3)
    p = tmp_path / "config.json"
    cfg.save(p)
    loaded = AppConfig.load(p)
    assert loaded.image_worker_thread_count == 3



def test_cache_slug_invariant_to_worker_count():
    """Worker count doesn't affect rendered output, so it must not influence the slug."""
    base = AppConfig(image_worker_thread_count=1)
    other = AppConfig(image_worker_thread_count=4)
    assert base.cache_slug() == other.cache_slug()


def test_v1_file_loads_with_default_worker_count(tmp_path: Path):
    """Load a file written as v1 (no image_worker_thread_count); should default to 1."""
    p = tmp_path / "config.json"
    # Simulate a v1 save: only include v1 fields, no image_worker_thread_count.
    v1_data = {"version": 1}
    p.write_text(json.dumps(v1_data))
    loaded = AppConfig.load(p)
    assert loaded.image_worker_thread_count == 1


def test_mdns_hostname_default():
    assert AppConfig().mdns_hostname == "hokku"


def test_mdns_hostname_empty_means_off():
    cfg = AppConfig(mdns_hostname="")
    assert cfg.mdns_hostname == ""


def test_mdns_hostname_roundtrips(tmp_path: Path):
    cfg = AppConfig(mdns_hostname="my-frame")
    p = tmp_path / "config.json"
    cfg.save(p)
    loaded = AppConfig.load(p)
    assert loaded.mdns_hostname == "my-frame"


def test_mdns_hostname_empty_roundtrips(tmp_path: Path):
    cfg = AppConfig(mdns_hostname="")
    p = tmp_path / "config.json"
    cfg.save(p)
    loaded = AppConfig.load(p)
    assert loaded.mdns_hostname == ""


def test_v3_migrates_forward():
    """A v3 dict gains mdns_hostname and loses face_detector after full migration."""
    v3_blob = {"version": 3, "image_worker_thread_count": 1, "face_detector": "yunet_opencv"}
    migrated = _migrate(v3_blob)
    assert migrated["version"] == _CURRENT_VERSION
    assert migrated["mdns_hostname"] == "hokku"
    assert "face_detector" not in migrated


def test_v3_migration_removes_old_mdns_enabled():
    """Old alpha configs with mdns_enabled bool get it removed and hostname added."""
    old_v3_blob = {"version": 3, "image_worker_thread_count": 1, "face_detector": "yunet_opencv",
                   "mdns_enabled": True}
    migrated = _migrate(old_v3_blob)
    assert "mdns_enabled" not in migrated
    assert migrated["mdns_hostname"] == "hokku"
    assert "face_detector" not in migrated


def test_v4_migrates_to_v5_removes_face_detector():
    """A v4 dict loses face_detector in the v4→v5 migration."""
    v4_blob = {"version": 4, "image_worker_thread_count": 1, "face_detector": "yunet_opencv",
               "mdns_hostname": "hokku"}
    migrated = _migrate(v4_blob)
    assert migrated["version"] == _CURRENT_VERSION
    assert "face_detector" not in migrated


def test_cache_slug_invariant_to_mdns_hostname():
    """mDNS hostname doesn't affect rendered output so it must not influence the slug."""
    assert AppConfig(mdns_hostname="hokku").cache_slug() == AppConfig(mdns_hostname="").cache_slug()
