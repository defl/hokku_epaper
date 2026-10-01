"""Version ordering for firmware release artifacts.

``version_key`` decides which build the server hands to a screen when several
versions sit in the same directory (``firmware/release/`` is not pruned between
builds, and the downloaded-firmware overlay accumulates too). Getting the order
wrong doesn't fail loudly — it silently serves and flashes an older build — so
the ordering is pinned here rather than left to the callers' own tests.

Hardware-free; no filesystem access beyond tmp_path.
"""

from __future__ import annotations

from hokku.common.firmware_paths import version_key


def test_double_digit_component_outranks_single_digit():
    # The headline case, and the one a plain string sort gets backwards:
    # '1.2.10' < '1.2.9' as strings, because '1' < '9' at the first difference.
    assert version_key("1.2.10") > version_key("1.2.9")
    assert version_key("1.10.0") > version_key("1.9.0")
    assert version_key("10.0.0") > version_key("9.0.0")


def test_orders_a_realistic_release_series():
    versions = ["1.2.9", "1.2.10", "1.2.2", "1.3.0", "1.2.11", "2.0.0"]
    assert sorted(versions, key=version_key) == [
        "1.2.2",
        "1.2.9",
        "1.2.10",
        "1.2.11",
        "1.3.0",
        "2.0.0",
    ]


def test_prerelease_ranks_below_its_own_final_release():
    # A beta must never look newer than the release it precedes, or publishing
    # 1.2.10 final would leave the beta still winning.
    assert version_key("1.2.10-beta") < version_key("1.2.10")
    assert version_key("1.2.10-rc1") < version_key("1.2.10")
    # ...but still above the previous final release.
    assert version_key("1.2.10-beta") > version_key("1.2.9")


def test_differing_component_counts_compare_sensibly():
    assert version_key("1.3") == version_key("1.3.0")
    assert version_key("1.3.1") > version_key("1.3")


def test_unparseable_versions_rank_below_every_real_version():
    # A truncated download, a hand-renamed file, or the empty string a caller
    # passes when the filename doesn't match the release convention must never
    # outrank a genuine release.
    for junk in ("", "garbage", "1.2.x", "not-a-version", "4.0.1~dev.14"):
        assert version_key(junk) < version_key("0.0.1"), junk


def test_max_over_mixed_valid_and_junk_picks_the_real_version():
    candidates = ["hokku-x-1.2.9", "1.2.10", "", "1.2.4"]
    assert max(candidates, key=version_key) == "1.2.10"
