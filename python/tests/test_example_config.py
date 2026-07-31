"""Verify config/config.json.example (the shipped template) stays in sync with AppConfig.

If AppConfig fields are renamed, added, or their types change this test will
catch it before shipping so users aren't handed a broken example file.
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

from hokku.webserver.app_config import _CURRENT_VERSION, AppConfig
from hokku.webserver.gen_example_config import render

_EXAMPLE = (
    Path(__file__).resolve().parents[1] / "hokku" / "webserver" / "config" / "config.json.example"
)


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
        f"config.json.example version={data.get('version')!r} "
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
        f"Fields in AppConfig missing from config.json.example: {sorted(missing)}. "
        "Add them to hokku/webserver/config/config.json.example."
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
        f"Unknown keys in config.json.example (possible typos): {sorted(unknown)}. "
        "Fix the key names or remove them."
    )


def test_example_config_classifier_flags():
    """Classifier flags should be explicitly set in the example (not left to defaults)."""
    data = _load_example()
    assert "classifier_bw_detect_enabled" in data


# ── generated, not hand-maintained ────────────────────────────────────────────


def test_example_config_matches_the_generator():
    """The checked-in file must be exactly what gen_example_config produces.

    The example used to be hand-edited and drifted out of the schema, which is
    how it ended up written in a pre-oklab shape while stamped with the current
    version. Everything downstream is seeded from it, so drift means every new
    install starts from a stale config.
    """
    assert _EXAMPLE.read_text(encoding="utf-8") == render(), (
        "config.json.example is out of date. Regenerate it with:\n"
        "    python -m hokku.webserver.gen_example_config"
    )


def test_example_config_pipelines_survive_a_load():
    """Loading the example must preserve its three pipelines, not reset them.

    The regression this guards: a single field missing from a pipeline blob
    reset that pipeline to the fallback preset. Because all three reset, every
    install ran the default pipeline for B&W and face photos too — the symptom
    being "all the presets look the same".
    """
    cfg = AppConfig.from_dict(_load_example())

    assert cfg.image_config_default != cfg.image_config_bw
    assert cfg.image_config_default != cfg.image_config_face
    # And specifically, the tuning that was being lost:
    assert cfg.image_config_face.dither.algorithm == "atkinson"
    assert cfg.image_config_face.clahe_clip_limit < cfg.image_config_default.clahe_clip_limit
    assert cfg.image_config_bw.dither.lut_name == "bw"
    assert cfg.image_config_bw.adaptive_saturate_space == "off"


def test_example_config_upload_and_cache_dirs_are_nonempty():
    """upload_dir and cache_dir must be set to something in the example."""
    data = _load_example()
    assert data.get("upload_dir"), "upload_dir should be set in the example config"
    assert data.get("cache_dir"), "cache_dir should be set in the example config"
