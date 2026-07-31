"""ImageConfig: round-trip and cache_slug stability."""

from __future__ import annotations

from dataclasses import asdict, replace

import pytest

from hokku.webserver.dither_config import DitherConfig
from hokku.webserver.image_config import ImageConfig, _image_config_from_dict
from hokku.webserver.presets import FALLBACK_PRESET, PRESET_IMAGE_CONFIGS


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
        adaptive_saturate_space="off",
        saturate_max_enhance=1.0,
        saturate_low_chroma_thresh=5.0,
        saturate_high_chroma_thresh=15.0,
        saturate_low_chroma_thresh_oklab=0.025,
        saturate_high_chroma_thresh_oklab=0.075,
        scale_chroma=False,
        adaptive_vivid=False,
        vivid_chroma_low=5.0,
        vivid_chroma_high=15.0,
        vivid_chroma_low_oklab=0.025,
        vivid_chroma_high_oklab=0.075,
        drc_l_space="cielab",
        drc_chroma_space="cielab",
        prepare_midtone=1.0,
        clahe_clip_limit=0.0,
        clahe_keepout_feather=0.0,
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
        adaptive_saturate_space="cielab",
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


def test_image_config_from_dict_missing_dither_keeps_default_dither():
    """An absent dither block keeps the default's dither — the rest survives."""
    base = _default_image_config()
    d = asdict(base)
    d.pop("dither")
    d["prepare_brightness"] = 1.42
    restored = _image_config_from_dict(d, default=base)
    assert restored.dither == base.dither
    assert restored.prepare_brightness == pytest.approx(1.42)


def test_image_config_from_dict_missing_field_keeps_only_that_field_default():
    """One absent field must not discard every other stored value.

    This is the regression that shipped: a single missing field reset the whole
    pipeline to the fallback preset, so the example config — and any config
    predating a newly added field — silently lost all of its tuning.
    """
    base = _default_image_config()
    d = asdict(base)
    d.pop("prepare_brightness")
    d["prepare_gamma"] = 0.55  # a deliberately non-default value
    d["color_enhance"] = 1.9

    restored = _image_config_from_dict(d)

    assert restored.prepare_brightness == base.prepare_brightness  # filled from default
    assert restored.prepare_gamma == pytest.approx(0.55)  # stored value survives
    assert restored.color_enhance == pytest.approx(1.9)


def test_image_config_from_dict_merges_onto_the_supplied_default():
    """Each pipeline merges onto its own default, not onto the fallback preset."""
    face_like = replace(_default_image_config(), clahe_clip_limit=1.25, prepare_usm_amount=130)
    restored = _image_config_from_dict({"prepare_gamma": 0.7}, default=face_like)
    assert restored.clahe_clip_limit == pytest.approx(1.25)
    assert restored.prepare_usm_amount == 130
    assert restored.prepare_gamma == pytest.approx(0.7)


def test_image_config_from_dict_not_dict_raises():
    with pytest.raises(ValueError):
        _image_config_from_dict("not a dict")


# ── new field leniency ────────────────────────────────────────────────────────


def test_new_fields_use_defaults_when_absent():
    """A config predating several fields keeps everything it does carry."""
    base = _default_image_config()
    d = asdict(base)
    for key in (
        "prepare_midtone",
        "clahe_clip_limit",
        "prepare_usm_radius",
        "prepare_usm_amount",
        "dither_noise",
        # The field whose omission caused the shipped example to reset.
        "clahe_keepout_feather",
    ):
        d.pop(key, None)
    d["prepare_contrast"] = 1.33

    restored = _image_config_from_dict(d, default=base)

    assert restored.prepare_contrast == pytest.approx(1.33)
    for key in ("prepare_midtone", "clahe_clip_limit", "clahe_keepout_feather"):
        assert getattr(restored, key) == getattr(base, key)


def test_renamed_use_adaptive_saturate_still_honoured():
    """A rename is not a missing field — the old key must still carry meaning."""
    d = asdict(_default_image_config())
    d.pop("adaptive_saturate_space")
    d["use_adaptive_saturate"] = False
    assert _image_config_from_dict(d).adaptive_saturate_space == "off"
    d["use_adaptive_saturate"] = True
    assert _image_config_from_dict(d).adaptive_saturate_space == "cielab"
