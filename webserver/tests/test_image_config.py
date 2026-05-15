"""ImageConfig: round-trip and cache_slug stability."""
from __future__ import annotations

import pytest
from dataclasses import asdict, replace

from hokku_server.dither_config import DitherConfig
from hokku_server.image_config import ImageConfig, _image_config_from_dict
from hokku_server.presets import FALLBACK_PRESET, PRESET_IMAGE_CONFIGS


def _default_dither() -> DitherConfig:
    return DitherConfig(
        algorithm="floyd_steinberg",
        lut_name="euclidean",
        serpentine=False,
        hue_cutoff_deg=95.0,
        neutral_chroma=8.0,
    )


def _default_image_config() -> ImageConfig:
    return ImageConfig(
        dither=_default_dither(),
        prepare_autocontrast_cutoff=0.5,
        prepare_gamma=0.85,
        prepare_brightness=1.0,
        prepare_contrast=1.1,
        color_enhance=1.2,
        use_adaptive_saturate=False,
        saturate_max_enhance=1.0,
        saturate_low_chroma_thresh=5.0,
        saturate_high_chroma_thresh=15.0,
        scale_chroma=False,
        adaptive_vivid=False,
        vivid_chroma_low=5.0,
        vivid_chroma_high=15.0,
        prepare_midtone=1.0,
        clahe_clip_limit=0.0,
        prepare_usm_radius=1.0,
        prepare_usm_amount=120,
        dither_noise=0.0,
    )


def test_default_roundtrip_via_asdict():
    cfg = _default_image_config()
    d = asdict(cfg)
    restored = _image_config_from_dict(d)
    assert restored == cfg


def test_non_default_roundtrip():
    cfg = replace(
        _default_image_config(),
        prepare_brightness=0.8,
        use_adaptive_saturate=True,
        saturate_max_enhance=1.5,
        dither=DitherConfig(
            algorithm="atkinson",
            lut_name="hue_aware",
            serpentine=True,
            hue_cutoff_deg=60.0,
            neutral_chroma=10.0,
        ),
    )
    restored = _image_config_from_dict(asdict(cfg))
    assert restored == cfg


def test_cache_slug_stable():
    cfg = _default_image_config()
    assert cfg.cache_slug() == cfg.cache_slug()
    assert cfg.cache_slug() == _image_config_from_dict(asdict(cfg)).cache_slug()


def test_cache_slug_changes_when_brightness_changes():
    a = _default_image_config()
    b = replace(a, prepare_brightness=0.7)
    assert a.cache_slug() != b.cache_slug()


def test_cache_slug_changes_when_dither_changes():
    a = _default_image_config()
    b = replace(a, dither=replace(a.dither, algorithm="stucki"))
    assert a.cache_slug() != b.cache_slug()


def test_cache_slug_length():
    assert len(_default_image_config().cache_slug()) == 14


def test_image_config_from_dict_none_returns_default():
    result = _image_config_from_dict(None)
    assert result == PRESET_IMAGE_CONFIGS[FALLBACK_PRESET]


def test_image_config_from_dict_missing_dither_returns_default():
    d = asdict(_default_image_config())
    d.pop("dither")
    assert _image_config_from_dict(d) == PRESET_IMAGE_CONFIGS[FALLBACK_PRESET]


def test_image_config_from_dict_missing_field_returns_default():
    d = asdict(_default_image_config())
    d.pop("prepare_brightness")
    assert _image_config_from_dict(d) == PRESET_IMAGE_CONFIGS[FALLBACK_PRESET]


def test_image_config_from_dict_not_dict_raises():
    with pytest.raises(ValueError):
        _image_config_from_dict("not a dict")


# ── new field leniency ────────────────────────────────────────────────────────

def test_new_fields_use_defaults_when_absent():
    """Missing new fields reset the whole config to the default preset."""
    d = asdict(_default_image_config())
    for key in ("prepare_midtone", "clahe_clip_limit", "prepare_usm_radius",
                "prepare_usm_amount", "dither_noise"):
        d.pop(key, None)
    assert _image_config_from_dict(d) == PRESET_IMAGE_CONFIGS[FALLBACK_PRESET]


# ── legacy prepare_sharpness backward compat ──────────────────────────────────

def test_legacy_sharpness_derives_usm_amount():
    """prepare_sharpness present but no USM keys → derive prepare_usm_amount."""
    d = asdict(_default_image_config())
    d.pop("prepare_usm_amount")
    d.pop("prepare_usm_radius")
    d["prepare_sharpness"] = 1.3
    restored = _image_config_from_dict(d)
    # max(0, int((1.3 - 1.0) * 400)) = 120
    assert restored.prepare_usm_amount == 120
    assert restored.prepare_usm_radius == pytest.approx(1.0)


def test_legacy_sharpness_identity_resets_to_default():
    """prepare_sharpness with missing USM fields resets to default preset."""
    d = asdict(_default_image_config())
    d.pop("prepare_usm_amount")
    d.pop("prepare_usm_radius")
    d["prepare_sharpness"] = 1.0
    assert _image_config_from_dict(d) == PRESET_IMAGE_CONFIGS[FALLBACK_PRESET]


def test_explicit_usm_wins_over_legacy_sharpness():
    """When both keys are present, explicit prepare_usm_amount takes precedence."""
    d = asdict(_default_image_config())
    d["prepare_sharpness"] = 2.0   # would imply 400
    d["prepare_usm_amount"] = 50   # explicit — must win
    restored = _image_config_from_dict(d)
    assert restored.prepare_usm_amount == 50
