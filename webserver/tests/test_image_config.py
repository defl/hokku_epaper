"""ImageConfig: round-trip and cache_slug stability."""
from __future__ import annotations

import pytest
from dataclasses import asdict, replace

from hokku_server.dither_config import DitherConfig
from hokku_server.image_config import ImageConfig, _image_config_from_dict
from hokku_server.presets import DEFAULT_PRESET, PRESET_IMAGE_CONFIGS


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
        prepare_sharpness=1.3,
        color_enhance=1.2,
        use_adaptive_saturate=False,
        saturate_max_enhance=1.0,
        saturate_low_chroma_thresh=5.0,
        saturate_high_chroma_thresh=15.0,
        scale_chroma=False,
        adaptive_vivid=False,
        vivid_chroma_low=5.0,
        vivid_chroma_high=15.0,
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
    assert result == PRESET_IMAGE_CONFIGS[DEFAULT_PRESET]


def test_image_config_from_dict_missing_dither_raises():
    d = asdict(_default_image_config())
    d.pop("dither")
    with pytest.raises(ValueError, match="dither"):
        _image_config_from_dict(d)


def test_image_config_from_dict_missing_field_raises():
    d = asdict(_default_image_config())
    d.pop("prepare_brightness")
    with pytest.raises(ValueError, match="prepare_brightness"):
        _image_config_from_dict(d)


def test_image_config_from_dict_not_dict_raises():
    with pytest.raises(ValueError):
        _image_config_from_dict("not a dict")
