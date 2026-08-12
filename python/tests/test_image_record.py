"""ImageRecord: serialisation of the per-picture overrides.

The overrides are the only user-authored data in image_manager.json — every
other field is derived from the source file and can be recomputed. These tests
pin the two properties that follow from that: they survive a round trip, and a
corrupt one costs only itself.
"""

from __future__ import annotations

from dataclasses import asdict, replace

import pytest

from hokku.webserver.image_record import ConvertStatus, ImageRecord
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS


def _record(**kwargs) -> ImageRecord:
    base = ImageRecord(
        name="a.png",
        name_hash="0123456789abcd",
        original_sha1="deadbeef",
        original_size_bytes=1234,
        original_mtime=1.0,
        added_at=2.0,
        convert_status=ConvertStatus.OK,
        convert_error=None,
        slugs={"huessen_epf1301.landscape": "slug0"},
        last_conversion_seconds=3.5,
        image_width=800,
        image_height=600,
    )
    return replace(base, **kwargs)


def test_roundtrip_without_overrides():
    rec = _record()
    assert rec.image_config is None
    assert rec.crop_to_fill_threshold is None
    assert ImageRecord.from_dict(rec.to_dict()) == rec


def test_roundtrip_with_both_overrides():
    rec = _record(
        image_config=PRESET_IMAGE_CONFIGS["atkinson_hue_aware"],
        crop_to_fill_threshold=0.25,
    )
    restored = ImageRecord.from_dict(rec.to_dict())
    assert restored == rec
    assert restored.image_config == PRESET_IMAGE_CONFIGS["atkinson_hue_aware"]
    assert restored.crop_to_fill_threshold == pytest.approx(0.25)


def test_the_two_overrides_are_independent():
    """Overriding the pipeline says nothing about the crop, and vice versa."""
    pipeline_only = _record(image_config=PRESET_IMAGE_CONFIGS["floyd_steinberg_bw"])
    crop_only = _record(crop_to_fill_threshold=0.4)

    assert ImageRecord.from_dict(pipeline_only.to_dict()).crop_to_fill_threshold is None
    assert ImageRecord.from_dict(crop_only.to_dict()).image_config is None
    assert ImageRecord.from_dict(crop_only.to_dict()).crop_to_fill_threshold == pytest.approx(0.4)


def test_a_row_predating_the_fields_still_loads():
    """No _DB_VERSION bump: a v4 row simply has no override keys."""
    d = _record().to_dict()
    del d["image_config"]
    del d["crop_to_fill_threshold"]

    restored = ImageRecord.from_dict(d)

    assert restored.image_config is None
    assert restored.crop_to_fill_threshold is None
    assert restored.image_width == 800  # the rest of the row is intact


def test_a_zero_crop_override_is_not_confused_with_absent():
    """0.0 is a real setting — always letterbox, never crop."""
    d = _record(crop_to_fill_threshold=0.0).to_dict()
    assert ImageRecord.from_dict(d).crop_to_fill_threshold == 0.0


def test_malformed_dither_override_drops_only_itself(caplog):
    """A corrupt override must not cost the whole record.

    _load_db() skips any record whose from_dict() raises, so propagating here
    would discard the image's dimensions, slugs and status too, and force a
    needless re-render of a picture whose cached output is perfectly good.
    """
    d = _record(crop_to_fill_threshold=0.3).to_dict()
    d["image_config"] = {"dither": {"algorithm": "nonsense"}}  # missing nearly every field

    restored = ImageRecord.from_dict(d)

    assert restored.image_config is None  # dropped
    assert restored.crop_to_fill_threshold == pytest.approx(0.3)  # the other one survives
    assert restored.image_width == 800  # and so does the rest of the record
    assert restored.slugs == {"huessen_epf1301.landscape": "slug0"}
    assert "Dropping malformed dither override" in caplog.text


def test_malformed_crop_override_drops_only_itself(caplog):
    d = _record(image_config=PRESET_IMAGE_CONFIGS["atkinson_hue_aware"]).to_dict()
    d["crop_to_fill_threshold"] = 7.5  # outside [0, 1]

    restored = ImageRecord.from_dict(d)

    assert restored.crop_to_fill_threshold is None
    assert restored.image_config == PRESET_IMAGE_CONFIGS["atkinson_hue_aware"]
    assert "Dropping malformed crop override" in caplog.text


def test_override_is_stored_as_plain_json():
    """to_dict() has to be JSON-ready — asdict recurses into the nested config."""
    d = _record(image_config=PRESET_IMAGE_CONFIGS["atkinson_hue_aware"]).to_dict()
    assert isinstance(d["image_config"], dict)
    assert isinstance(d["image_config"]["dither"], dict)
    assert d["image_config"]["dither"]["algorithm"] == "atkinson"


def test_asdict_on_the_record_matches_to_dict():
    rec = _record(image_config=PRESET_IMAGE_CONFIGS["atkinson_hue_aware"])
    assert asdict(rec) == rec.to_dict()
