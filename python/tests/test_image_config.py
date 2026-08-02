"""ImageConfig: round-trip and cache_slug stability."""

from __future__ import annotations

from dataclasses import asdict, fields, replace
from typing import get_args

import pytest

from hokku.webserver.dither_config import DitherConfig, LutName
from hokku.webserver.dither_streaming import _validate
from hokku.webserver.image_config import (
    ImageConfig,
    ImageConfigError,
    _image_config_from_dict,
    image_config_from_dict_strict,
    parse_crop_to_fill_threshold,
)
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


# ── strict parser (API input) ─────────────────────────────────────────────────
#
# The strict parser is the mirror image of the lenient one above: the lenient
# path exists so a stored config from an older version keeps loading, while this
# one exists so a request cannot quietly mean something other than it says.


def test_strict_accepts_a_complete_config():
    cfg = _default_image_config()
    assert image_config_from_dict_strict(asdict(cfg)) == cfg


def test_strict_rejects_a_missing_field_and_names_it():
    d = asdict(_default_image_config())
    d.pop("prepare_gamma")
    with pytest.raises(ImageConfigError) as exc:
        image_config_from_dict_strict(d)
    assert exc.value.errors == ["image_config.prepare_gamma: required"]


def test_strict_rejects_an_unknown_field():
    """A typo'd knob must fail, not silently keep the default.

    This is the whole reason the strict parser exists: the lenient parser reads
    only the fields it knows, so `prepare_gama` would be dropped on the floor
    and the request would answer 200 having changed nothing.
    """
    d = asdict(_default_image_config())
    d["prepare_gama"] = 0.9
    with pytest.raises(ImageConfigError) as exc:
        image_config_from_dict_strict(d)
    assert exc.value.errors == ["image_config.prepare_gama: unknown field"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("adaptive_saturate_space", "lab"),
        ("drc_l_space", "oklab2"),
        ("drc_chroma_space", ""),
    ],
)
def test_strict_rejects_bad_enum_values(field: str, value: str):
    d = asdict(_default_image_config())
    d[field] = value
    with pytest.raises(ImageConfigError, match=field):
        image_config_from_dict_strict(d)


@pytest.mark.parametrize(("field", "value"), [("algorithm", "sierra"), ("lut_name", "ciecam")])
def test_strict_rejects_bad_dither_enum_values(field: str, value: str):
    """Caught here rather than inside a render worker.

    dither_streaming._validate() does reject these, but only once the image is
    already being converted — the user sees a failed conversion instead of a
    rejected setting.
    """
    d = asdict(_default_image_config())
    d["dither"][field] = value
    with pytest.raises(ImageConfigError, match=f"dither.{field}"):
        image_config_from_dict_strict(d)


def test_strict_rejects_int_for_bool():
    """bool is a subclass of int, so `serpentine: 1` would otherwise sail through."""
    d = asdict(_default_image_config())
    d["dither"]["serpentine"] = 1
    with pytest.raises(ImageConfigError, match="serpentine"):
        image_config_from_dict_strict(d)


def test_strict_rejects_bool_for_number():
    d = asdict(_default_image_config())
    d["prepare_gamma"] = True
    with pytest.raises(ImageConfigError, match="prepare_gamma"):
        image_config_from_dict_strict(d)


def test_strict_accepts_int_for_float_field():
    """JSON has one number type — an integral gamma must not be rejected."""
    d = asdict(_default_image_config())
    d["prepare_gamma"] = 1
    assert image_config_from_dict_strict(d).prepare_gamma == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prepare_gamma", 0.0),
        ("prepare_midtone", -1.0),
        ("prepare_contrast", 0.0),
        ("prepare_autocontrast_cutoff", 50.0),
        ("prepare_autocontrast_cutoff", -0.1),
        ("clahe_clip_limit", -0.5),
        ("clahe_keepout_feather", 1.5),
        ("dither_noise", -1.0),
        ("prepare_usm_amount", -10),
    ],
)
def test_strict_rejects_out_of_range_values(field: str, value: float):
    d = asdict(_default_image_config())
    d[field] = value
    with pytest.raises(ImageConfigError, match=field):
        image_config_from_dict_strict(d)


def test_strict_rejects_out_of_range_hue_cutoff():
    d = asdict(_default_image_config())
    d["dither"]["hue_cutoff_deg"] = 400.0
    with pytest.raises(ImageConfigError, match="hue_cutoff_deg"):
        image_config_from_dict_strict(d)


def test_strict_reports_every_problem_at_once():
    """The UI lists the errors, so one round trip must find them all."""
    d = asdict(_default_image_config())
    d.pop("prepare_gamma")
    d["nonsense"] = 1
    d["prepare_contrast"] = -1.0
    d["dither"]["lut_name"] = "nope"

    with pytest.raises(ImageConfigError) as exc:
        image_config_from_dict_strict(d)

    assert len(exc.value.errors) == 4


@pytest.mark.parametrize("blob", [None, "not a dict", 42, []])
def test_strict_rejects_non_objects(blob):
    with pytest.raises(ImageConfigError, match="must be an object"):
        image_config_from_dict_strict(blob)


def test_strict_rejects_non_object_dither():
    d = asdict(_default_image_config())
    d["dither"] = "floyd_steinberg"
    with pytest.raises(ImageConfigError, match="dither: must be an object"):
        image_config_from_dict_strict(d)


def test_strict_result_survives_the_lenient_round_trip():
    """The render worker re-parses leniently; that must not alter the config.

    render_worker.render_one() receives asdict(image_config) and rebuilds it
    with _image_config_from_dict, so a strict-validated override has to come
    back out of that path bit-identical or the rendered image would not match
    the settings the cache slug was computed from.
    """
    cfg = image_config_from_dict_strict(asdict(_default_image_config()))
    assert _image_config_from_dict(asdict(cfg)) == cfg


def test_strict_field_coverage_matches_the_dataclass():
    """Guards the reflection: every field is reachable, none silently skipped."""
    d = asdict(_default_image_config())
    assert set(d) == {f.name for f in fields(ImageConfig)}
    assert set(d["dither"]) == {f.name for f in fields(DitherConfig)}


def test_validate_lut_list_matches_the_literal():
    """dither_streaming._validate used to repeat the LUT names by hand.

    A LUT added to LutName but forgotten there was rejected at render time, so
    the two must now be the same list by construction.
    """
    for lut in get_args(LutName):
        _validate(replace(_default_dither(), lut_name=lut))
    with pytest.raises(ValueError, match="Unknown lut_name"):
        _validate(replace(_default_dither(), lut_name="not_a_lut"))  # type: ignore[arg-type]


# ── crop-to-fill threshold ────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [0, 0.0, 0.25, 1, 1.0])
def test_parse_crop_threshold_accepts_the_unit_interval(value):
    assert parse_crop_to_fill_threshold(value) == pytest.approx(float(value))


@pytest.mark.parametrize("value", [-0.1, 1.1, "0.5", None, True])
def test_parse_crop_threshold_rejects_everything_else(value):
    with pytest.raises(ImageConfigError):
        parse_crop_to_fill_threshold(value)
