"""Tests for the camera-rig analysis helpers.

`decode_note` is the piece worth pinning. Rating notes are written about what the
judge saw on screen — "2 is too light", "left is washed out" — and positions are
shuffled per photograph precisely so the notes cannot be read at face value. If
the translation back to arm names is wrong, or silently incomplete, a round's
notes do not look broken; they look *scattered*. That happened: the spatial words
were missing at first, every note in one round said left or right, the report
printed them untranslated, and nine complaints that all landed on the same arm
read as unrelated remarks.

The guards against over-eager translation matter just as much. "all 3 have yellow
skin" is a count, and turning it into an arm name would invent a complaint about
one rendering out of a statement about every rendering.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "camcal"))

from rate_report import decode_note

THREE = [{"slot": 1, "arm": "live"}, {"slot": 2, "arm": "oklab"}, {"slot": 3, "arm": "calm"}]
TWO = [{"slot": 1, "arm": "live"}, {"slot": 2, "arm": "trim70"}]


def test_a_number_becomes_the_arm_that_sat_there():
    assert decode_note("2 is too light", THREE) == "[oklab] is too light"


def test_position_follows_the_slot_number_not_the_list_order():
    """The key is serialised in whatever order it was built, so this cannot
    lean on list position — a shuffled key must decode identically."""
    shuffled = [THREE[2], THREE[0], THREE[1]]
    assert decode_note("2 is too light", shuffled) == decode_note("2 is too light", THREE)


def test_left_and_right_become_the_outer_arms():
    """The omission that hid a whole round's story."""
    assert decode_note("left is washed out", THREE) == "[live] is washed out"
    assert decode_note("right is washed out", THREE) == "[calm] is washed out"


def test_spatial_words_are_case_insensitive():
    assert decode_note("LEFT is better", THREE) == "[live] is better"


def test_middle_resolves_only_when_there_is_a_middle():
    assert decode_note("middle is best", THREE) == "[oklab] is best"
    assert decode_note("middle is best", TWO) == "middle is best"


@pytest.mark.parametrize("note", ["all 3 have yellow skin", "these 2 are fine"])
def test_a_counted_quantity_is_not_a_position(note):
    """A counted quantity describes every rendering, not the third one."""
    assert decode_note(note, THREE) == note


@pytest.mark.parametrize("note", ["1.5 stops too dark", "30% too red"])
def test_measurements_are_left_alone(note):
    """A digit inside a decimal or a percentage is not a seat number."""
    assert decode_note(note, THREE) == note


def test_several_positions_in_one_note_all_translate():
    assert decode_note("left and right differ", THREE) == "[live] and [calm] differ"
