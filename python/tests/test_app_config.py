"""AppConfig: load/save roundtrip, defaults, cache_slug, version + migrations."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from hokku.webserver.app_config import _CURRENT_VERSION, AppConfig, _migrate
from hokku.webserver.presets import (
    DEFAULT_BW_IMAGE_CONFIG,
    DEFAULT_FACE_IMAGE_CONFIG,
    DEFAULT_IMAGE_CONFIG,
    PRESET_IMAGE_CONFIGS,
)


def test_defaults():
    cfg = AppConfig()
    assert cfg.port == 8080
    assert cfg.version == _CURRENT_VERSION
    assert cfg.image_config_default == DEFAULT_IMAGE_CONFIG
    assert cfg.image_config_bw == DEFAULT_BW_IMAGE_CONFIG
    assert cfg.image_config_face == DEFAULT_FACE_IMAGE_CONFIG
    assert cfg.classifier_bw_detect_enabled is True
    assert not hasattr(cfg, "orientation")


def test_pipeline_defaults_are_actually_different():
    """The three pipelines must not collapse onto one another.

    They did: every install ran the colour pipeline for B&W and face photos
    because a missing field reset each of them to the fallback preset.
    """
    cfg = AppConfig()
    assert cfg.image_config_default != cfg.image_config_bw
    assert cfg.image_config_default != cfg.image_config_face
    assert cfg.image_config_bw != cfg.image_config_face


def test_face_default_is_tuned_for_skin():
    """Face pipeline: gentler local contrast, stronger/wider unsharp than default."""
    cfg = AppConfig()
    face, default = cfg.image_config_face, cfg.image_config_default
    assert face.clahe_clip_limit < default.clahe_clip_limit
    assert face.prepare_usm_amount > default.prepare_usm_amount
    assert face.prepare_usm_radius > default.prepare_usm_radius
    assert face.dither.algorithm == "atkinson"


def test_dropdown_presets_unchanged_by_pipeline_defaults():
    """The face tuning must not leak into the general-purpose Atkinson preset."""
    assert DEFAULT_FACE_IMAGE_CONFIG != PRESET_IMAGE_CONFIGS["atkinson_hue_aware"]
    assert PRESET_IMAGE_CONFIGS["atkinson_hue_aware"].clahe_clip_limit == 1.75


def test_cache_slug_invariant_to_port():
    base = AppConfig(port=8080)
    other = AppConfig(port=9999)
    assert base.cache_slug() == other.cache_slug()


def test_cache_slug_changes_with_image_config_default():
    base = AppConfig()
    other = AppConfig(image_config_default=PRESET_IMAGE_CONFIGS["floyd_steinberg_bw"])
    assert base.cache_slug() != other.cache_slug()


def test_cache_slug_changes_with_classifier_flags():
    bw_off = AppConfig(classifier_bw_detect_enabled=False)
    bw_on = AppConfig(classifier_bw_detect_enabled=True)
    assert bw_off.cache_slug() != bw_on.cache_slug()


def test_save_load_roundtrip(tmp_path: Path):
    cfg = AppConfig(
        upload_dir=str(tmp_path / "uploads"),
        cache_dir=str(tmp_path / "cache"),
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
    cfg = AppConfig.from_dict({"port": 9999})
    # No version → default v1 returned, ignoring the other fields.
    assert cfg == AppConfig()


def test_unversioned_config_load_writes_back(tmp_path: Path):
    """AppConfig.load() on an unversioned JSON file writes the default back."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"port": 9999}))
    cfg = AppConfig.load(p)
    assert cfg == AppConfig()
    # File now has a version field.
    data = json.loads(p.read_text())
    assert data["version"] == _CURRENT_VERSION


def test_image_configs_roundtrip(tmp_path: Path):
    cfg = AppConfig(
        image_config_default=PRESET_IMAGE_CONFIGS["floyd_steinberg_hue_aware"],
        image_config_bw=PRESET_IMAGE_CONFIGS["floyd_steinberg_bw"],
        classifier_bw_detect_enabled=True,
    )
    p = tmp_path / "config.json"
    cfg.save(p)
    loaded = AppConfig.load(p)
    assert loaded == cfg
    assert loaded.classifier_bw_detect_enabled is True


def test_image_field_with_partial_blob_falls_back_to_default(tmp_path: Path):
    """A corrupt image_config_default blob (partial dither) falls back to the default preset."""
    p = tmp_path / "c.json"
    p.write_text(
        json.dumps(
            {
                "version": _CURRENT_VERSION,
                "image_config_default": {"dither": {}},
            }
        )
    )
    cfg = AppConfig.load(p)
    assert cfg.image_config_default == PRESET_IMAGE_CONFIGS["floyd_steinberg_hue_aware"]


def test_v1_migrates_to_current():
    """A v1 dict migrates all the way forward; the old image_worker_thread_count
    (added at v2) is dropped again at v9, and memory_budget_mb is present."""
    v1_blob = {"version": 1}  # minimal valid v1

    migrated = _migrate(v1_blob)
    assert migrated["version"] == _CURRENT_VERSION
    assert "image_worker_thread_count" not in migrated  # removed at v8→v9
    assert migrated["memory_budget_mb"] == 0  # added at v8→v9


def test_v2_migrates_forward():
    """A v2 dict migrates all the way to current version without error."""
    v2_blob = {"version": 2, "image_worker_thread_count": 1}

    migrated = _migrate(v2_blob)
    assert migrated["version"] == _CURRENT_VERSION
    # face_detector was added in v2→v3 then removed in v4→v5; must not survive.
    assert "face_detector" not in migrated
    # image_worker_thread_count was added at v1→v2 then removed at v8→v9.
    assert "image_worker_thread_count" not in migrated


def test_memory_budget_mb_roundtrips(tmp_path: Path):
    """memory_budget_mb is written to and read from JSON."""
    cfg = AppConfig(memory_budget_mb=512)
    p = tmp_path / "config.json"
    cfg.save(p)
    loaded = AppConfig.load(p)
    assert loaded.memory_budget_mb == 512


def test_cache_slug_invariant_to_memory_budget():
    """The memory budget doesn't affect rendered output, so it must not influence the slug."""
    base = AppConfig(memory_budget_mb=0)
    other = AppConfig(memory_budget_mb=512)
    assert base.cache_slug() == other.cache_slug()


def test_v1_file_loads_and_gains_memory_budget(tmp_path: Path):
    """Load a file written as v1; it migrates forward and gains memory_budget_mb."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"version": 1}))
    loaded = AppConfig.load(p)
    assert loaded.memory_budget_mb == 0


def test_server_threads_default_is_bounded():
    """The WSGI request-thread pool must be small and fixed — the whole point is
    to NOT spawn a thread per request (which OOM'd the Pi on process limits)."""
    n = AppConfig().server_threads
    assert isinstance(n, int)
    assert 1 <= n <= 16, f"server_threads default {n} is not a small bounded pool"


def test_server_threads_roundtrips(tmp_path: Path):
    cfg = AppConfig(server_threads=2)
    p = tmp_path / "config.json"
    cfg.save(p)
    assert AppConfig.load(p).server_threads == 2


def test_old_config_gets_default_server_threads(tmp_path: Path):
    """A config written before server_threads existed loads with the default."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"version": _CURRENT_VERSION, "port": 8080}))
    assert AppConfig.load(p).server_threads == AppConfig().server_threads


def test_cache_slug_invariant_to_server_threads():
    """Serving concurrency doesn't affect rendered output — must not change the slug."""
    assert AppConfig(server_threads=2).cache_slug() == AppConfig(server_threads=8).cache_slug()


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
    old_v3_blob = {
        "version": 3,
        "image_worker_thread_count": 1,
        "face_detector": "yunet_opencv",
        "mdns_enabled": True,
    }
    migrated = _migrate(old_v3_blob)
    assert "mdns_enabled" not in migrated
    assert migrated["mdns_hostname"] == "hokku"
    assert "face_detector" not in migrated


def test_v4_migrates_to_v5_removes_face_detector():
    """A v4 dict loses face_detector in the v4→v5 migration."""
    v4_blob = {
        "version": 4,
        "image_worker_thread_count": 1,
        "face_detector": "yunet_opencv",
        "mdns_hostname": "hokku",
    }
    migrated = _migrate(v4_blob)
    assert migrated["version"] == _CURRENT_VERSION
    assert "face_detector" not in migrated


def test_v5_migrates_to_v6_drops_orientation():
    """A v5 dict loses the global orientation field in the v5→v6 migration."""
    v5_blob = {
        "version": 5,
        "image_worker_thread_count": 1,
        "mdns_hostname": "hokku",
        "orientation": "portrait",
    }
    migrated = _migrate(v5_blob)
    assert migrated["version"] == _CURRENT_VERSION
    assert "orientation" not in migrated


def test_v5_config_load_drops_orientation(tmp_path: Path):
    """End-to-end: a v5 config.json with orientation loads as v6 without it."""
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps({"version": 5, "orientation": "portrait", "port": 9000}),
    )
    cfg = AppConfig.load(p)
    assert cfg.port == 9000
    assert not hasattr(cfg, "orientation")
    # Re-saved config has v6 and no orientation key.
    cfg.save(p)
    data = json.loads(p.read_text())
    assert data["version"] == _CURRENT_VERSION
    assert "orientation" not in data


def test_cache_slug_invariant_to_mdns_hostname():
    """mDNS hostname doesn't affect rendered output so it must not influence the slug."""
    assert AppConfig(mdns_hostname="hokku").cache_slug() == AppConfig(mdns_hostname="").cache_slug()
