"""ServeScheduler: rotation fairness, stats, telemetry, persistence."""
from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

from hokku_server.app_config import AppConfig
from hokku_server.image_manager_single import SingleThreadedImageManager
from hokku_server.orientation import Orientation
from hokku_server.screen_config import ScreenConfig
from hokku_server.serve_scheduler import ServeScheduler


def _setup(app_config: AppConfig, make_test_image, names: list[str]) -> tuple[SingleThreadedImageManager, ServeScheduler]:
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
    mgr, sched = _setup(app_config, make_test_image, ["a.png"])
    assert sched.pick_next(Orientation.NEUTRAL) == "a.png"


def test_fair_rotation(app_config: AppConfig, make_test_image):
    mgr, sched = _setup(app_config, make_test_image, ["a.png", "b.png", "c.png"])
    counts = {"a.png": 0, "b.png": 0, "c.png": 0}
    for _ in range(9):
        n = sched.pick_next(Orientation.NEUTRAL)
        assert n is not None
        sched.mark_served(n)
        counts[n] += 1
    assert counts == {"a.png": 3, "b.png": 3, "c.png": 3}


def test_stats_after_serves(app_config: AppConfig, make_test_image):
    mgr, sched = _setup(app_config, make_test_image, ["a.png"])
    n = sched.pick_next(Orientation.NEUTRAL)
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
    assert sched2.stats_for("a.png").total_show_count == 1
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
    mgr, sched = _setup_with_sizes(app_config, make_test_image, [
        ("land.png", (40, 30)),
        ("port.png", (30, 40)),
    ])
    result = sched.pick_next(Orientation.LANDSCAPE)
    assert result == "land.png"


def test_pick_next_orientation_filter_portrait(app_config: AppConfig, make_test_image):
    mgr, sched = _setup_with_sizes(app_config, make_test_image, [
        ("land.png", (40, 30)),
        ("port.png", (30, 40)),
    ])
    result = sched.pick_next(Orientation.PORTRAIT)
    assert result == "port.png"


def test_pick_next_neutral_includes_all(app_config: AppConfig, make_test_image):
    mgr, sched = _setup_with_sizes(app_config, make_test_image, [
        ("land.png", (40, 30)),
        ("port.png", (30, 40)),
    ])
    # NEUTRAL = no filter, so the first pick is whichever has lowest show_index (alphabetical tie-break)
    result = sched.pick_next(Orientation.NEUTRAL)
    assert result in ("land.png", "port.png")


def test_pick_next_neutral_image_eligible_under_any_filter(app_config: AppConfig, make_test_image):
    # 30×30 = square = NEUTRAL
    mgr, sched = _setup_with_sizes(app_config, make_test_image, [
        ("square.png", (30, 30)),
    ])
    assert sched.pick_next(Orientation.LANDSCAPE) == "square.png"
    assert sched.pick_next(Orientation.PORTRAIT) == "square.png"
    assert sched.pick_next(Orientation.NEUTRAL) == "square.png"


def test_pick_next_orientation_filter_no_match_returns_none(app_config: AppConfig, make_test_image):
    # Only portrait images; requesting landscape yields None
    mgr, sched = _setup_with_sizes(app_config, make_test_image, [
        ("port.png", (30, 40)),
    ])
    assert sched.pick_next(Orientation.LANDSCAPE) is None


def test_peek_next_no_side_effects(app_config: AppConfig, make_test_image):
    mgr, sched = _setup(app_config, make_test_image, ["a.png"])
    before = sched._next_for.copy()
    _ = sched.peek_next(Orientation.NEUTRAL)
    assert sched._next_for == before
    s = sched.stats_for("a.png")
    assert s is None or s.total_show_count == 0


def test_set_screen_config_preserves_all_fields(app_config: AppConfig):
    mgr = SingleThreadedImageManager(app_config)
    sched = ServeScheduler(mgr)

    cfg1 = ScreenConfig(orientation_override=Orientation.LANDSCAPE, filter_by_orientation=True)
    sched.set_screen_config("s1", cfg1)

    # Update only orientation_override — filter_by_orientation must survive
    current = sched.get_screen_config("s1")
    sched.set_screen_config("s1", replace(current, orientation_override=Orientation.PORTRAIT))
    updated = sched.get_screen_config("s1")
    assert updated.orientation_override == Orientation.PORTRAIT
    assert updated.filter_by_orientation is True


def test_precompute_recomputes_all_orientations_on_mark_served(app_config: AppConfig, make_test_image):
    mgr, sched = _setup_with_sizes(app_config, make_test_image, [
        ("land.png", (40, 30)),
        ("port.png", (30, 40)),
    ])
    # After mark_served, _next_for should update for all orientations
    sched.mark_served("land.png")
    assert sched._next_for[Orientation.LANDSCAPE] is not None or sched._next_for[Orientation.NEUTRAL] == "port.png"
    assert sched._next_for[Orientation.PORTRAIT] == "port.png"


def test_precompute_all_locked_landscape_only(app_config: AppConfig, make_test_image):
    # Only landscape images: NEUTRAL and LANDSCAPE point to same image, PORTRAIT is None
    mgr, sched = _setup_with_sizes(app_config, make_test_image, [
        ("land.png", (40, 30)),
    ])
    assert sched._next_for[Orientation.NEUTRAL] == "land.png"
    assert sched._next_for[Orientation.LANDSCAPE] == "land.png"
    assert sched._next_for[Orientation.PORTRAIT] is None


def test_precompute_all_locked_mixed(app_config: AppConfig, make_test_image):
    mgr, sched = _setup_with_sizes(app_config, make_test_image, [
        ("land.png", (40, 30)),
        ("port.png", (30, 40)),
    ])
    # Both orientations have a candidate; NEUTRAL picks alphabetically first (both show_index=0)
    assert sched._next_for[Orientation.LANDSCAPE] == "land.png"
    assert sched._next_for[Orientation.PORTRAIT] == "port.png"
    assert sched._next_for[Orientation.NEUTRAL] in ("land.png", "port.png")


def test_precompute_all_locked_neutral_images_appear_in_all_slots(app_config: AppConfig, make_test_image):
    # Square image (NEUTRAL) should be picked for all three orientation slots
    mgr, sched = _setup_with_sizes(app_config, make_test_image, [
        ("square.png", (30, 30)),
    ])
    assert sched._next_for[Orientation.NEUTRAL] == "square.png"
    assert sched._next_for[Orientation.LANDSCAPE] == "square.png"
    assert sched._next_for[Orientation.PORTRAIT] == "square.png"
