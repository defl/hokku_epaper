"""ServeScheduler: rotation fairness, stats, telemetry, persistence."""

from __future__ import annotations

import json
import random
from dataclasses import replace
from pathlib import Path

from hokku.webserver.app_config import AppConfig
from hokku.webserver.image_manager_single import SingleThreadedImageManager
from hokku.webserver.orientation import Orientation
from hokku.webserver.screen_config import ScreenConfig
from hokku.webserver.serve_scheduler import ServeScheduler


def _setup(
    app_config: AppConfig, make_test_image, names: list[str]
) -> tuple[SingleThreadedImageManager, ServeScheduler]:
    upload = Path(app_config.upload_dir)
    for n in names:
        make_test_image(upload / n)
    mgr = SingleThreadedImageManager(app_config)
    mgr.sync()
    return mgr, ServeScheduler(mgr)


def _setup_with_sizes(
    app_config: AppConfig,
    make_test_image,
    name_sizes: list[tuple[str, tuple[int, int]]],
) -> tuple[SingleThreadedImageManager, ServeScheduler]:
    """Like _setup but lets the caller specify per-image dimensions."""
    upload = Path(app_config.upload_dir)
    for name, size in name_sizes:
        make_test_image(upload / name, size=size)
    mgr = SingleThreadedImageManager(app_config)
    mgr.sync()
    return mgr, ServeScheduler(mgr)


def test_pick_next_empty(app_config: AppConfig):
    mgr = SingleThreadedImageManager(app_config)
    sched = ServeScheduler(mgr)
    assert sched.pick_next(Orientation.NEUTRAL) is None


def test_pick_next_single(app_config: AppConfig, make_test_image):
    _, sched = _setup(app_config, make_test_image, ["a.png"])
    assert sched.pick_next(Orientation.NEUTRAL) == "a.png"


def test_fair_rotation(app_config: AppConfig, make_test_image):
    _, sched = _setup(app_config, make_test_image, ["a.png", "b.png", "c.png"])
    counts = {"a.png": 0, "b.png": 0, "c.png": 0}
    for _ in range(9):
        n = sched.pick_next(Orientation.NEUTRAL)
        assert n is not None
        sched.mark_served(n)
        counts[n] += 1
    assert counts == {"a.png": 3, "b.png": 3, "c.png": 3}


def test_tie_break_draws_from_full_tied_set(app_config: AppConfig, make_test_image, monkeypatch):
    """When multiple images share the minimum show_index, the pick must be
    drawn from that whole tied set via random.choice, not deterministically
    the alphabetically-first name — otherwise every rotation reset triggered
    by a new upload (_reconcile levelling the least-shown images) replays the
    same name-sorted prefix before later names ever get a turn."""
    calls = []
    original_choice = random.choice

    def spy_choice(seq):
        calls.append(sorted(seq))
        return original_choice(seq)

    # Patch before construction: ServeScheduler.__init__ already runs one
    # _precompute_all_locked pass itself, so patching after _setup() would
    # miss it and the cached pick from that pass would satisfy pick_next()
    # without ever re-invoking random.choice.
    monkeypatch.setattr("hokku.webserver.serve_scheduler.random.choice", spy_choice)
    _, sched = _setup(app_config, make_test_image, ["z.png", "a.png", "m.png"])
    sched.pick_next(Orientation.NEUTRAL)
    assert calls, "random.choice should be used to break show_index ties"
    assert calls[0] == ["a.png", "m.png", "z.png"]


def test_new_upload_preserves_cycle_progress(app_config: AppConfig, make_test_image):
    """A new upload must not let images already served this cycle cut back ahead
    of the laggard. Reconcile drops the newcomer in tied with the current
    least-shown image and normalises the rest down to that minimum, so the
    image still owed a turn keeps its priority. The old behaviour flattened
    every show_index to 1 on any new image, erasing how far the cycle had run
    and letting just-served names jump the queue again on the next upload."""
    mgr, sched = _setup(app_config, make_test_image, ["a.png", "b.png", "c.png"])
    # Round 1 serves everyone once; round 2 serves a and b again, so c lags.
    for n in ["a.png", "b.png", "c.png", "a.png", "b.png"]:
        sched.mark_served(n)

    # A genuinely new image arrives mid-cycle and triggers reconcile.
    make_test_image(Path(app_config.upload_dir) / "d.png")
    mgr.sync()
    sched.pick_next(Orientation.NEUTRAL)

    idx = {}
    for n in ("a.png", "b.png", "c.png", "d.png"):
        s = sched.stats_for(n)
        assert s is not None
        idx[n] = s.show_index
    # Laggard (c) and newcomer (d) share the minimum; already-served a and b
    # stay strictly ahead and cannot cut back in.
    assert idx["c.png"] == idx["d.png"] == min(idx.values())
    assert idx["a.png"] > idx["c.png"]
    assert idx["b.png"] > idx["c.png"]


def test_stats_after_serves(app_config: AppConfig, make_test_image):
    _, sched = _setup(app_config, make_test_image, ["a.png"])
    n = sched.pick_next(Orientation.NEUTRAL)
    assert n is not None
    sched.mark_served(n)
    s = sched.stats_for("a.png")
    assert s is not None
    assert s.total_show_count == 1
    assert s.last_served_at is not None


def test_persistence(app_config: AppConfig, make_test_image):
    mgr, sched = _setup(app_config, make_test_image, ["a.png", "b.png"])
    sched.mark_served("a.png")
    sched.mark_served("b.png")

    # New scheduler instance over the same files
    sched2 = ServeScheduler(mgr)
    stats_a = sched2.stats_for("a.png")
    assert stats_a is not None
    assert stats_a.total_show_count == 1
    last = sched2.last_served()
    assert last is not None and last[0] == "b.png"


def test_telemetry_record(app_config: AppConfig):
    mgr = SingleThreadedImageManager(app_config)
    sched = ServeScheduler(mgr)
    sched.record_screen_call("frame-01", "192.168.1.5", 300, None, 3800, None)
    screens = sched.screens()
    assert "frame-01" in screens
    s = screens["frame-01"]
    assert s.ip == "192.168.1.5"
    assert s.request_count == 1
    assert s.battery_mv == 3800
    assert s.battery_percent is not None and 0 <= s.battery_percent <= 100


def test_cal_mean_accumulates_and_seeds(app_config: AppConfig):
    """Reported cal_ppm folds into a MAC-pinned mean; cal_seed_for returns it."""
    mgr = SingleThreadedImageManager(app_config)
    sched = ServeScheduler(mgr)
    mac = "aa:bb:cc:dd:ee:ff"
    # First report seeds the mean directly; later reports EMA toward them.
    sched.record_screen_call("frame-1", "1.1.1.1", 300, None, None, None, mac=mac, cal_ppm=10000)
    mean, n = sched.cal_seed_for("frame-1")
    assert mean == 10000 and n == 1
    for _ in range(20):
        sched.record_screen_call(
            "frame-1", "1.1.1.1", 300, None, None, None, mac=mac, cal_ppm=12000
        )
    mean, n = sched.cal_seed_for("frame-1")
    assert 10000 < mean <= 12000 and n == 21
    # Resolvable by MAC as well as by name.
    assert sched.cal_seed_for(mac=mac) == sched.cal_seed_for("frame-1")


def test_cal_seed_unknown_is_zero(app_config: AppConfig):
    mgr = SingleThreadedImageManager(app_config)
    sched = ServeScheduler(mgr)
    assert sched.cal_seed_for("nobody") == (0, 0)
    # A device that never reported cal_ppm has no mean to seed with.
    sched.record_screen_call("frame-1", "1.1.1.1", 300, None, None, None, mac="a1:a1:a1:a1:a1:a1")
    assert sched.cal_seed_for("frame-1") == (0, 0)


def test_calibration_survives_rename_via_mac(app_config: AppConfig):
    """Renaming a device (same MAC) keeps its record + calibration."""
    mgr = SingleThreadedImageManager(app_config)
    sched = ServeScheduler(mgr)
    mac = "de:ad:be:ef:00:01"
    sched.record_screen_call("old-name", "1.1.1.1", 300, None, None, None, mac=mac, cal_ppm=8000)
    sched.record_screen_call("new-name", "1.1.1.1", 300, None, None, None, mac=mac, cal_ppm=8000)
    screens = sched.screens()
    assert "new-name" in screens and "old-name" not in screens
    mean, n = sched.cal_seed_for("new-name")
    assert mean == 8000 and n == 2  # both samples kept across the rename


def test_calibration_reattaches_when_mac_appears(app_config: AppConfig):
    """A device first seen by name only (pre-update FW) keeps its record when a
    later check-in brings a MAC."""
    mgr = SingleThreadedImageManager(app_config)
    sched = ServeScheduler(mgr)
    sched.record_screen_call("frame-1", "1.1.1.1", 300, None, None, None)  # no MAC yet
    sched.record_screen_call("frame-1", "1.1.1.1", 300, None, None, None, mac="ab:cd:ef:01:02:03")
    assert len(sched.screens()) == 1
    assert sched.resolve(mac="ab:cd:ef:01:02:03") == sched.resolve(name="frame-1")


def test_same_name_different_mac_does_not_hijack(app_config: AppConfig):
    """Two devices sharing a name but with different MACs stay distinct records
    (calibration is never cross-contaminated)."""
    mgr = SingleThreadedImageManager(app_config)
    sched = ServeScheduler(mgr)
    sched.record_screen_call(
        "dup", "1.1.1.1", 300, None, None, None, mac="11:11:11:11:11:11", cal_ppm=5000
    )
    sched.record_screen_call(
        "dup", "1.1.1.2", 300, None, None, None, mac="22:22:22:22:22:22", cal_ppm=9000
    )
    assert sched.cal_seed_for(mac="11:11:11:11:11:11") == (5000, 1)
    assert sched.cal_seed_for(mac="22:22:22:22:22:22") == (9000, 1)


def test_legacy_name_keyed_db_migrates(app_config: AppConfig, make_test_image):
    """An old name-keyed serve_scheduler.json loads into the sid-keyed model."""
    mgr, sched = _setup(app_config, make_test_image, ["a.png"])
    sched.record_screen_call("frame-1", "1.1.1.1", 300, None, 3800, None)
    sched.set_screen_config("frame-1", ScreenConfig(orientation=Orientation.PORTRAIT))
    # Rewrite the DB in the legacy (schema 1, name-keyed) shape.
    db = Path(app_config.cache_dir) / "serve_scheduler.json"
    data = json.loads(db.read_text())
    data.pop("schema", None)
    data.pop("sid_seq", None)
    data["screens"] = {"frame-1": next(iter(data["screens"].values()))}
    data["screen_configs"] = {"frame-1": next(iter(data["screen_configs"].values()))}
    db.write_text(json.dumps(data))

    sched2 = ServeScheduler(mgr)
    screens = sched2.screens()
    assert "frame-1" in screens
    assert sched2.get_screen_config("frame-1").orientation == Orientation.PORTRAIT


def test_orphan_dropped_on_pick(app_config: AppConfig, make_test_image):
    mgr, sched = _setup(app_config, make_test_image, ["a.png", "b.png"])
    sched.mark_served("a.png")
    # Remove a.png from disk + manager
    mgr.remove("a.png")
    sched.pick_next(Orientation.NEUTRAL)
    assert "a.png" not in sched.stats()


# ── Orientation filter tests ──────────────────────────────────────────────────


def test_pick_next_orientation_filter_landscape(app_config: AppConfig, make_test_image):
    # 40×30 = landscape, 30×40 = portrait
    _, sched = _setup_with_sizes(
        app_config,
        make_test_image,
        [
            ("land.png", (40, 30)),
            ("port.png", (30, 40)),
        ],
    )
    result = sched.pick_next(Orientation.LANDSCAPE)
    assert result == "land.png"


def test_pick_next_orientation_filter_portrait(app_config: AppConfig, make_test_image):
    _, sched = _setup_with_sizes(
        app_config,
        make_test_image,
        [
            ("land.png", (40, 30)),
            ("port.png", (30, 40)),
        ],
    )
    result = sched.pick_next(Orientation.PORTRAIT)
    assert result == "port.png"


def test_pick_next_neutral_includes_all(app_config: AppConfig, make_test_image):
    _, sched = _setup_with_sizes(
        app_config,
        make_test_image,
        [
            ("land.png", (40, 30)),
            ("port.png", (30, 40)),
        ],
    )
    # NEUTRAL = no filter, so the first pick is whichever has lowest show_index (alphabetical tie-break)
    result = sched.pick_next(Orientation.NEUTRAL)
    assert result in ("land.png", "port.png")


def test_pick_next_neutral_image_eligible_under_any_filter(app_config: AppConfig, make_test_image):
    # 30×30 = square = NEUTRAL
    _, sched = _setup_with_sizes(
        app_config,
        make_test_image,
        [
            ("square.png", (30, 30)),
        ],
    )
    assert sched.pick_next(Orientation.LANDSCAPE) == "square.png"
    assert sched.pick_next(Orientation.PORTRAIT) == "square.png"
    assert sched.pick_next(Orientation.NEUTRAL) == "square.png"


def test_pick_next_orientation_filter_no_match_returns_none(app_config: AppConfig, make_test_image):
    # Only portrait images; requesting landscape yields None
    _, sched = _setup_with_sizes(
        app_config,
        make_test_image,
        [
            ("port.png", (30, 40)),
        ],
    )
    assert sched.pick_next(Orientation.LANDSCAPE) is None


def test_peek_next_no_side_effects(app_config: AppConfig, make_test_image):
    _, sched = _setup(app_config, make_test_image, ["a.png"])
    before = sched._next_for.copy()
    _ = sched.peek_next(Orientation.NEUTRAL)
    assert sched._next_for == before
    s = sched.stats_for("a.png")
    assert s is None or s.total_show_count == 0


def test_set_screen_config_preserves_all_fields(app_config: AppConfig):
    mgr = SingleThreadedImageManager(app_config)
    sched = ServeScheduler(mgr)

    cfg1 = ScreenConfig(orientation=Orientation.LANDSCAPE, filter_by_orientation=True)
    sched.set_screen_config("s1", cfg1)

    # Update only orientation — filter_by_orientation must survive
    current = sched.get_screen_config("s1")
    sched.set_screen_config("s1", replace(current, orientation=Orientation.PORTRAIT))
    updated = sched.get_screen_config("s1")
    assert updated.orientation == Orientation.PORTRAIT
    assert updated.filter_by_orientation is True


def test_unknown_screen_defaults_to_landscape(app_config: AppConfig):
    """A never-seen screen returns ScreenConfig() with the dataclass default."""
    mgr = SingleThreadedImageManager(app_config)
    sched = ServeScheduler(mgr)
    cfg = sched.get_screen_config("never-seen")
    assert cfg.orientation == Orientation.LANDSCAPE
    assert cfg.filter_by_orientation is False


def test_precompute_recomputes_all_orientations_on_mark_served(
    app_config: AppConfig, make_test_image
):
    _, sched = _setup_with_sizes(
        app_config,
        make_test_image,
        [
            ("land.png", (40, 30)),
            ("port.png", (30, 40)),
        ],
    )
    # After mark_served, _next_for should update for all orientations
    sched.mark_served("land.png")
    assert (
        sched._next_for[Orientation.LANDSCAPE] is not None
        or sched._next_for[Orientation.NEUTRAL] == "port.png"
    )
    assert sched._next_for[Orientation.PORTRAIT] == "port.png"


def test_precompute_all_locked_landscape_only(app_config: AppConfig, make_test_image):
    # Only landscape images: NEUTRAL and LANDSCAPE point to same image, PORTRAIT is None
    _, sched = _setup_with_sizes(
        app_config,
        make_test_image,
        [
            ("land.png", (40, 30)),
        ],
    )
    assert sched._next_for[Orientation.NEUTRAL] == "land.png"
    assert sched._next_for[Orientation.LANDSCAPE] == "land.png"
    assert sched._next_for[Orientation.PORTRAIT] is None


def test_precompute_all_locked_mixed(app_config: AppConfig, make_test_image):
    _, sched = _setup_with_sizes(
        app_config,
        make_test_image,
        [
            ("land.png", (40, 30)),
            ("port.png", (30, 40)),
        ],
    )
    # Both orientations have a candidate; NEUTRAL picks alphabetically first (both show_index=0)
    assert sched._next_for[Orientation.LANDSCAPE] == "land.png"
    assert sched._next_for[Orientation.PORTRAIT] == "port.png"
    assert sched._next_for[Orientation.NEUTRAL] in ("land.png", "port.png")


def test_precompute_all_locked_neutral_images_appear_in_all_slots(
    app_config: AppConfig, make_test_image
):
    # Square image (NEUTRAL) should be picked for all three orientation slots
    _, sched = _setup_with_sizes(
        app_config,
        make_test_image,
        [
            ("square.png", (30, 30)),
        ],
    )
    assert sched._next_for[Orientation.NEUTRAL] == "square.png"
    assert sched._next_for[Orientation.LANDSCAPE] == "square.png"
    assert sched._next_for[Orientation.PORTRAIT] == "square.png"
