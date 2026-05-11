"""Verify config/config.example.json stays in sync with AppConfig.

If AppConfig fields are renamed, added, or their types change this test will
catch it before shipping so users aren't handed a broken example file.
"""
from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import pytest

from hokku_server.app_config import AppConfig, _CURRENT_VERSION

_EXAMPLE = Path(__file__).resolve().parents[2] / "webserver" / "config" / "config.example.json"


def _load_example() -> dict:
    return json.loads(_EXAMPLE.read_text("utf-8"))


# ── basic parsability ─────────────────────────────────────────────────────────

def test_example_config_exists():
    assert _EXAMPLE.exists(), f"Example config missing: {_EXAMPLE}"


def test_example_config_is_valid_json():
    data = _load_example()
    assert isinstance(data, dict)


def test_example_config_version_is_current():
    data = _load_example()
    assert data.get("version") == _CURRENT_VERSION, (
        f"config.example.json version={data.get('version')!r} "
        f"but _CURRENT_VERSION={_CURRENT_VERSION}. "
        "Bump the version in the example or update the migration chain."
    )


def test_example_config_parses_without_error():
    """AppConfig.from_dict() must accept the example config without raising."""
    data = _load_example()
    cfg = AppConfig.from_dict(data)
    assert isinstance(cfg, AppConfig)


def test_example_config_round_trips():
    """Parsed example → to_dict() → from_dict() must yield an identical AppConfig."""
    data = _load_example()
    cfg = AppConfig.from_dict(data)
    cfg2 = AppConfig.from_dict(cfg.to_dict())
    assert cfg == cfg2, "Example config does not survive a save/load round-trip"


def test_example_config_has_all_known_fields():
    """Every AppConfig field name must appear in the example JSON.

    This catches fields added to AppConfig that were forgotten in the example.
    Fields that legitimately differ (upload_dir, cache_dir) are excluded from
    the equality check but must still be present as keys.
    """
    data = _load_example()
    cfg_fields = {f.name for f in fields(AppConfig)}
    missing = cfg_fields - set(data.keys())
    assert not missing, (
        f"Fields in AppConfig missing from config.example.json: {sorted(missing)}. "
        "Add them to webserver/config/config.example.json."
    )


def test_example_config_no_unknown_fields():
    """No key in the example should be silently ignored by from_dict().

    Catches typos in field names (e.g. classifier_face_detected_enabled).
    """
    data = _load_example()
    cfg_fields = {f.name for f in fields(AppConfig)}
    # image_config_* are parsed specially and not in fields() as plain keys
    # after the nested parse — check them separately.
    structural_extras = {"image_config_default", "image_config_bw"}
    unknown = set(data.keys()) - cfg_fields - structural_extras
    assert not unknown, (
        f"Unknown keys in config.example.json (possible typos): {sorted(unknown)}. "
        "Fix the key names or remove them."
    )


def test_example_config_classifier_flags():
    """Classifier flags should be explicitly set in the example (not left to defaults)."""
    data = _load_example()
    assert "classifier_bw_detect_enabled" in data


def test_example_config_upload_and_cache_dirs_are_nonempty():
    """upload_dir and cache_dir must be set to something in the example."""
    data = _load_example()
    assert data.get("upload_dir"), "upload_dir should be set in the example config"
    assert data.get("cache_dir"), "cache_dir should be set in the example config"
