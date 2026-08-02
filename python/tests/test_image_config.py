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
    complete_image_config_blob,
    image_config_from_dict_strict,
    parse_crop_to_fill_threshold,
)


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
    restored = image_config_from_dict_strict(d)
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
    restored = image_config_from_dict_strict(asdict(cfg))
    assert restored == cfg


def test_cache_slug_stable():
    cfg = _default_image_config()
    assert cfg.cache_slug() == cfg.cache_slug()
    assert cfg.cache_slug() == image_config_from_dict_strict(asdict(cfg)).cache_slug()


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


# ── complete_image_config_blob (migration helper) ─────────────────────────────
#
# Bringing an old blob up to the current shape is upgrade knowledge, so it lives
# on the migration chain and runs once, at the version bump that needs it. The
# parser itself stays strict. These tests cover what that one-time repair has to
# get right for a config written by an older build.


def test_complete_fills_an_absent_dither_block():
    base = _default_image_config()
    d = asdict(base)
    d.pop("dither")
    d["prepare_brightness"] = 1.42

    out = complete_image_config_blob(d, default=base, field_path="cfg")

    assert out["dither"] == asdict(base.dither)
    assert out["prepare_brightness"] == pytest.approx(1.42)


def test_complete_keeps_every_other_stored_value():
    """One absent field must not cost the rest of the tuning.

    This is the regression that shipped once already: a single missing field
    reset the whole pipeline to the fallback preset, so the example config — and
    any config predating a newly added field — silently lost everything.
    """
    base = _default_image_config()
    d = asdict(base)
    d.pop("prepare_brightness")
    d["prepare_gamma"] = 0.55  # deliberately non-default
    d["color_enhance"] = 1.9

    out = complete_image_config_blob(d, default=base, field_path="cfg")

    assert out["prepare_brightness"] == base.prepare_brightness  # taken from default
    assert out["prepare_gamma"] == pytest.approx(0.55)  # stored value survives
    assert out["color_enhance"] == pytest.approx(1.9)


def test_complete_uses_the_pipeline_its_given():
    """A sparse B&W blob must fall back to B&W values, not the colour pipeline's."""
    face_like = replace(_default_image_config(), clahe_clip_limit=1.25, prepare_usm_amount=130)

    out = complete_image_config_blob({"prepare_gamma": 0.7}, default=face_like, field_path="cfg")

    assert out["clahe_clip_limit"] == pytest.approx(1.25)
    assert out["prepare_usm_amount"] == 130
    assert out["prepare_gamma"] == pytest.approx(0.7)


def test_complete_translates_the_renamed_saturate_flag():
    """A rename is not a missing field — the old key still carries a real setting."""
    base = _default_image_config()
    d = asdict(base)
    d.pop("adaptive_saturate_space")

    d["use_adaptive_saturate"] = False
    assert (
        complete_image_config_blob(d, default=base, field_path="c")["adaptive_saturate_space"]
        == "off"
    )
    d["use_adaptive_saturate"] = True
    assert (
        complete_image_config_blob(d, default=base, field_path="c")["adaptive_saturate_space"]
        == "cielab"
    )


def test_complete_drops_fields_that_no_longer_exist():
    """Otherwise the strict parser would reject the migrated config."""
    base = _default_image_config()
    d = asdict(base)
    d["some_retired_knob"] = 3

    out = complete_image_config_blob(d, default=base, field_path="cfg")

    assert "some_retired_knob" not in out


def test_complete_output_parses_strictly():
    """The point of the exercise: migrate once, then parse strictly forever."""
    base = _default_image_config()
    d = asdict(base)
    d.pop("prepare_midtone")
    d.pop("clahe_keepout_feather")
    d["use_adaptive_saturate"] = True
    d.pop("adaptive_saturate_space")
    d["retired"] = "x"

    out = complete_image_config_blob(d, default=base, field_path="cfg")

    assert image_config_from_dict_strict(out).adaptive_saturate_space == "cielab"


def test_complete_none_returns_the_default():
    base = _default_image_config()
    assert complete_image_config_blob(None, default=base, field_path="cfg") == asdict(base)


def test_complete_rejects_a_non_object():
    with pytest.raises(ValueError):
        complete_image_config_blob("not a dict", default=_default_image_config(), field_path="cfg")


# ── strict parser ─────────────────────────────────────────────────────────────
#
# The only ImageConfig parser. Everything that reaches it — a stored config, an
# API request, the render worker's IPC payload — has to be complete and valid,
# so a mistake is reported once instead of being carried silently forever.


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


def test_strict_survives_the_render_worker_round_trip():
    """render_one() receives asdict(cfg) and rebuilds it on the other side.

    That has to come back bit-identical, or the rendered image would not match
    the settings its cache slug was computed from.
    """
    cfg = image_config_from_dict_strict(asdict(_default_image_config()))
    assert image_config_from_dict_strict(asdict(cfg)) == cfg


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
