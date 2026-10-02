"""Labels: normalisation, persistence on both records, and the OR filter."""

from __future__ import annotations

import pytest

from hokku.webserver.image_record import ConvertStatus, ImageRecord
from hokku.webserver.labels import LabelError, matches_labels, parse_labels
from hokku.webserver.orientation import Orientation
from hokku.webserver.screen_config import ScreenConfig


def _record(**kwargs) -> ImageRecord:
    return ImageRecord(
        name="a.png",
        name_hash="0123456789abcd",
        original_sha1="deadbeef",
        original_size_bytes=1234,
        original_mtime=1.0,
        added_at=2.0,
        convert_status=ConvertStatus.OK,
        convert_error=None,
        image_width=800,
        image_height=600,
        **kwargs,
    )


# ── parse_labels ──────────────────────────────────────────────────────────────


def test_parse_trims_collapses_dedupes_and_sorts():
    assert parse_labels(["  Kitchen ", "summer", "Kitchen", "a   b"]) == (
        "Kitchen",
        "a b",
        "summer",
    )


def test_parse_empty_list_is_no_labels():
    assert parse_labels([]) == ()


@pytest.mark.parametrize(
    "raw",
    ["kitchen", None, 3, ["a", 1], [""], ["   "], ["x" * 41], ["l"] * 33],
    ids=["str", "none", "int", "non-str item", "blank", "whitespace", "too long", "too many"],
)
def test_parse_rejects_malformed(raw):
    with pytest.raises(LabelError):
        parse_labels(raw)


def test_parse_error_names_the_field():
    with pytest.raises(LabelError, match=r"^add"):
        parse_labels("nope", field="add")


# ── matches_labels ────────────────────────────────────────────────────────────


def test_empty_filter_matches_everything():
    assert matches_labels((), frozenset())
    assert matches_labels(("a",), frozenset())


def test_filter_is_any_of():
    assert matches_labels(("kitchen", "summer"), frozenset({"summer", "winter"}))
    assert not matches_labels(("kitchen",), frozenset({"summer", "winter"}))
    assert not matches_labels((), frozenset({"summer"}))


# ── ImageRecord ───────────────────────────────────────────────────────────────


def test_record_labels_round_trip_as_a_plain_list():
    d = _record(labels=("b", "a")).to_dict()
    assert isinstance(d["labels"], tuple)  # asdict keeps the tuple; json writes a list
    restored = ImageRecord.from_dict({**d, "labels": ["b", "a"]})
    assert restored.labels == ("a", "b")


def test_record_predating_labels_loads_untagged():
    d = _record().to_dict()
    del d["labels"]
    assert ImageRecord.from_dict(d).labels == ()


def test_record_with_corrupt_labels_drops_only_them(caplog):
    d = _record(labels=("a",)).to_dict()
    d["labels"] = "not-a-list"
    with caplog.at_level("WARNING"):
        restored = ImageRecord.from_dict(d)
    assert restored.labels == ()
    assert restored.image_width == 800
    assert "malformed labels" in caplog.text


# ── ScreenConfig ──────────────────────────────────────────────────────────────


def test_screen_config_default_has_no_filter():
    assert ScreenConfig().labels == ()
    assert ScreenConfig.from_dict({"orientation": "landscape"}).labels == ()


def test_screen_config_labels_round_trip():
    cfg = ScreenConfig(orientation=Orientation.PORTRAIT, labels=("b", "a"))
    d = cfg.to_dict()
    assert d["labels"] == ["b", "a"]
    assert ScreenConfig.from_dict(d).labels == ("a", "b")


def test_screen_config_corrupt_labels_fall_back_to_whole_library():
    cfg = ScreenConfig.from_dict({"orientation": "landscape", "labels": [1, 2]})
    assert cfg.labels == ()
