"""ServeScheduler: rotation fairness, stats, telemetry, persistence."""
from __future__ import annotations

import time
from pathlib import Path

from hokku_server.app_config import AppConfig
from hokku_server.image_manager_single import SingleThreadedImageManager
from hokku_server.serve_scheduler import ServeScheduler


def _setup(app_config: AppConfig, make_test_image, names: list[str]) -> tuple[SingleThreadedImageManager, ServeScheduler]:
    upload = Path(app_config.upload_dir)
    for n in names:
        make_test_image(upload / n)
    mgr = SingleThreadedImageManager(app_config)
    mgr.sync()
    return mgr, ServeScheduler(mgr)


def test_pick_next_empty(app_config: AppConfig):
    mgr = SingleThreadedImageManager(app_config)
    sched = ServeScheduler(mgr)
    assert sched.pick_next() is None


def test_pick_next_single(app_config: AppConfig, make_test_image):
    mgr, sched = _setup(app_config, make_test_image, ["a.png"])
    assert sched.pick_next() == "a.png"


def test_fair_rotation(app_config: AppConfig, make_test_image):
    mgr, sched = _setup(app_config, make_test_image, ["a.png", "b.png", "c.png"])
    counts = {"a.png": 0, "b.png": 0, "c.png": 0}
    for _ in range(9):
        n = sched.pick_next()
        assert n is not None
        sched.mark_served(n)
        counts[n] += 1
    assert counts == {"a.png": 3, "b.png": 3, "c.png": 3}


def test_stats_after_serves(app_config: AppConfig, make_test_image):
    mgr, sched = _setup(app_config, make_test_image, ["a.png"])
    n = sched.pick_next()
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
    sched.pick_next()
    assert "a.png" not in sched.stats()
