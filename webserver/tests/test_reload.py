"""Tests for AppState hot-reload, Watcher state-following, and Flask integration.

Fast tests (always run):
  AppState:
    - holds initial references correctly
    - reload() swaps config, manager, scheduler atomically
    - reload() builds new manager with the new config's slug
    - reload() preserves old state when upload_dir is missing
    - reload() preserves old state when cache_dir is missing

  Watcher:
    - calls state.manager.sync() on each tick
    - follows the new manager after AppState.reload()
    - uses the new poll_interval after reload

  Flask routes (test client):
    - GET /hokku/api/config returns the current state.config
    - POST /hokku/api/config reloads in-process (restarting: false)
    - POST /hokku/api/config updates state.config and state.manager
    - POST /hokku/api/config with invalid body returns 400
    - POST /hokku/api/config with bad upload_dir returns 400, state unchanged
    - POST /hokku/api/config without config_path returns 500
"""
from __future__ import annotations

import json
import unittest.mock
from dataclasses import replace
from pathlib import Path

import pytest

from webserver.app_state import AppState
from webserver.app_config import AppConfig
from webserver.flask_app import create_app
from webserver.image_classifier import ImageClassifier
from webserver.image_manager import ImageManager
from webserver.serve_scheduler import ServeScheduler
from webserver.watcher import Watcher


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_state(app_config: AppConfig) -> AppState:
    clf = ImageClassifier(app_config)
    mgr = ImageManager(app_config, clf)
    sch = ServeScheduler(mgr)
    return AppState(app_config, clf, mgr, sch)


def _alt_config(base: AppConfig, tmp_path: Path) -> AppConfig:
    """A second AppConfig with a different pipeline slug (brightness changed).

    Uses the same upload_dir/cache_dir as *base* so reload() doesn't trip on
    missing directories.
    """
    return replace(
        base,
        image_config_default=replace(base.image_config_default, prepare_brightness=0.9),
    )


# ── AppState unit tests ───────────────────────────────────────────────────────

def test_app_state_holds_initial_references(app_config: AppConfig):
    clf = ImageClassifier(app_config)
    mgr = ImageManager(app_config, clf)
    sch = ServeScheduler(mgr)
    state = AppState(app_config, clf, mgr, sch)
    assert state.config is app_config
    assert state.classifier is clf
    assert state.manager is mgr
    assert state.scheduler is sch


def test_reload_swaps_config(app_config: AppConfig, tmp_path: Path):
    state = _make_state(app_config)
    new_cfg = _alt_config(app_config, tmp_path)
    assert new_cfg.cache_slug() != app_config.cache_slug()

    state.reload(new_cfg)

    assert state.config is new_cfg
    assert state.config.image_config_default.prepare_brightness == pytest.approx(0.9)


def test_reload_swaps_manager(app_config: AppConfig, tmp_path: Path):
    state = _make_state(app_config)
    old_manager = state.manager
    new_cfg = _alt_config(app_config, tmp_path)

    state.reload(new_cfg)

    assert state.manager is not old_manager
    assert state.manager.config is new_cfg


def test_reload_swaps_scheduler(app_config: AppConfig, tmp_path: Path):
    state = _make_state(app_config)
    old_scheduler = state.scheduler
    new_cfg = _alt_config(app_config, tmp_path)

    state.reload(new_cfg)

    assert state.scheduler is not old_scheduler


def test_reload_new_manager_has_new_slug(app_config: AppConfig, tmp_path: Path):
    state = _make_state(app_config)
    new_cfg = _alt_config(app_config, tmp_path)

    state.reload(new_cfg)

    assert state.manager.config.cache_slug() == new_cfg.cache_slug()


def test_reload_raises_for_missing_upload_dir(app_config: AppConfig, tmp_path: Path):
    state = _make_state(app_config)
    old_manager = state.manager
    old_config = state.config

    bad_cfg = replace(app_config, upload_dir=str(tmp_path / "nonexistent_upload"))

    with pytest.raises(ValueError, match="upload_dir"):
        state.reload(bad_cfg)

    # State must be unchanged.
    assert state.config is old_config
    assert state.manager is old_manager


def test_reload_raises_for_missing_cache_dir(app_config: AppConfig, tmp_path: Path):
    state = _make_state(app_config)
    old_manager = state.manager
    old_config = state.config

    bad_cfg = replace(app_config, cache_dir=str(tmp_path / "nonexistent_cache"))

    with pytest.raises(ValueError, match="cache_dir"):
        state.reload(bad_cfg)

    assert state.config is old_config
    assert state.manager is old_manager


def test_reload_is_idempotent(app_config: AppConfig, tmp_path: Path):
    """Reloading with the same config should succeed without error."""
    state = _make_state(app_config)
    state.reload(app_config)
    assert state.config is app_config


def test_reload_builds_new_classifier(app_config: AppConfig, tmp_path: Path):
    """reload() produces a fresh ImageClassifier, not the old one."""
    state = _make_state(app_config)
    old_classifier = state.classifier
    new_cfg = _alt_config(app_config, tmp_path)

    state.reload(new_cfg)

    assert state.classifier is not old_classifier


def test_reload_manager_wired_with_new_classifier(app_config: AppConfig, tmp_path: Path):
    """After reload, state.manager._classifier is state.classifier."""
    state = _make_state(app_config)
    new_cfg = _alt_config(app_config, tmp_path)

    state.reload(new_cfg)

    assert state.manager._classifier is state.classifier


# ── Watcher unit tests ────────────────────────────────────────────────────────

def test_watcher_syncs_immediately_on_construction(app_config: AppConfig):
    """Thread starts in the constructor and syncs without any external call."""
    import threading as _threading
    state = _make_state(app_config)
    synced = _threading.Event()
    original_sync = state.manager.sync

    def tracking_sync():
        original_sync()
        synced.set()

    state.manager.sync = tracking_sync  # type: ignore[method-assign]
    Watcher(state)
    assert synced.wait(timeout=5.0), "sync() should be called immediately on construction"


def test_watcher_wake_triggers_extra_sync(app_config: AppConfig):
    """wake() interrupts the sleep and causes a sync before the interval expires."""
    import threading as _threading
    state = _make_state(app_config)
    sync_count = 0
    second_sync_done = _threading.Event()
    first_sync_done = _threading.Event()
    original_sync = state.manager.sync

    def tracking_sync():
        nonlocal sync_count
        original_sync()
        sync_count += 1
        if sync_count == 1:
            first_sync_done.set()
        elif sync_count >= 2:
            second_sync_done.set()

    state.manager.sync = tracking_sync  # type: ignore[method-assign]
    w = Watcher(state)
    assert first_sync_done.wait(timeout=5.0), "First sync should happen immediately"
    w.wake()
    assert second_sync_done.wait(timeout=5.0), "Second sync should happen after wake()"


def test_watcher_calls_sync_on_manager(app_config: AppConfig):
    """Watcher daemon thread calls sync() on the current manager each iteration."""
    import threading as _threading
    state = _make_state(app_config)
    synced = _threading.Event()
    original_sync = state.manager.sync

    def tracking_sync():
        original_sync()
        synced.set()

    state.manager.sync = tracking_sync  # type: ignore[method-assign]
    Watcher(state)
    assert synced.wait(timeout=5.0), "Watcher should have synced the manager"


def test_watcher_follows_new_manager_after_reload(app_config: AppConfig, tmp_path: Path):
    """After AppState.reload(), the next sync uses the NEW manager."""
    import threading as _threading
    state = _make_state(app_config)
    new_cfg = _alt_config(app_config, tmp_path)
    state.reload(new_cfg)
    new_manager = state.manager

    synced = _threading.Event()
    original_sync = new_manager.sync

    def tracking_sync():
        original_sync()
        synced.set()

    new_manager.sync = tracking_sync  # type: ignore[method-assign]
    Watcher(state)
    assert synced.wait(timeout=5.0), "Watcher should sync the new manager"


def test_watcher_uses_new_poll_interval_after_reload(app_config: AppConfig, tmp_path: Path):
    """After reload, the watcher waits for the new config's poll_interval."""
    import threading as _threading
    state = _make_state(app_config)
    new_cfg = replace(app_config, poll_interval_seconds=42)
    state.reload(new_cfg)

    # Block inside sync() until we've had a chance to patch _wake.wait,
    # then release so the thread proceeds to the sleep.
    sync_may_proceed = _threading.Event()
    sync_done = _threading.Event()
    original_sync = state.manager.sync

    def gated_sync():
        sync_may_proceed.wait()
        original_sync()
        sync_done.set()

    state.manager.sync = gated_sync  # type: ignore[method-assign]

    w = Watcher(state)

    # Thread is now blocked in gated_sync() — safe to install our spy.
    sleep_durations: list[float] = []
    sleep_called = _threading.Event()
    original_wait = w._wake.wait

    def capturing_wait(timeout=None):
        sleep_durations.append(timeout)
        sleep_called.set()
        return original_wait(timeout=0)  # return immediately

    w._wake.wait = capturing_wait  # type: ignore[method-assign]

    sync_may_proceed.set()        # let sync() finish
    sync_done.wait(timeout=5.0)   # wait until sync returns
    sleep_called.wait(timeout=5.0)

    assert sleep_durations and sleep_durations[0] == 42


def test_watcher_stop_exits_cleanly(app_config: AppConfig):
    """stop() causes the thread to exit after its current sync completes."""
    import threading as _threading
    state = _make_state(app_config)
    exited = _threading.Event()

    original_run_forever = Watcher.run_forever

    def patched_run_forever(self):
        original_run_forever(self)
        exited.set()

    Watcher.run_forever = patched_run_forever  # type: ignore[method-assign]
    try:
        w = Watcher(state)
        w.stop()
        assert exited.wait(timeout=5.0), "Thread should exit cleanly after stop()"
    finally:
        Watcher.run_forever = original_run_forever  # type: ignore[method-assign]


# ── Flask integration tests ───────────────────────────────────────────────────

@pytest.fixture
def flask_state(app_config: AppConfig) -> AppState:
    return _make_state(app_config)


@pytest.fixture
def flask_client(flask_state: AppState, tmp_path: Path):
    config_path = tmp_path / "config.json"
    flask_state.config.save(config_path)
    app = create_app(flask_state, config_path=config_path, template_folder=None)
    app.config["TESTING"] = True
    return app.test_client(), flask_state, config_path


def test_flask_config_get_returns_current_config(flask_client):
    client, state, _ = flask_client
    resp = client.get("/hokku/api/config")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "config" in data
    assert data["config"]["orientation"] == state.config.orientation


def test_flask_config_post_returns_ok_not_restarting(flask_client):
    client, state, _ = flask_client
    resp = client.post(
        "/hokku/api/config",
        data=json.dumps({"orientation": "landscape"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["restarting"] is False


def test_flask_config_post_reloads_state(flask_client, tmp_path: Path):
    client, state, _ = flask_client
    old_manager = state.manager

    # Send a brightness change via the full image_config_default dict.
    from dataclasses import asdict
    new_image = asdict(replace(state.config.image_config_default, prepare_brightness=0.8))
    resp = client.post(
        "/hokku/api/config",
        data=json.dumps({"image_config_default": new_image}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert state.manager is not old_manager
    assert state.config.image_config_default.prepare_brightness == pytest.approx(0.8)


def test_flask_config_get_reflects_reloaded_config(flask_client, tmp_path: Path):
    """GET /hokku/api/config after a reload returns the new config, not the old one."""
    client, state, _ = flask_client
    from dataclasses import asdict
    new_image = asdict(replace(state.config.image_config_default, prepare_brightness=0.75))
    client.post(
        "/hokku/api/config",
        data=json.dumps({"image_config_default": new_image}),
        content_type="application/json",
    )

    resp = client.get("/hokku/api/config")
    data = resp.get_json()
    assert data["config"]["image_config_default"]["prepare_brightness"] == pytest.approx(0.75)


def test_flask_config_post_bad_json_returns_400(flask_client):
    client, _, _ = flask_client
    resp = client.post(
        "/hokku/api/config",
        data="not json",
        content_type="application/json",
    )
    assert resp.status_code == 400


def test_flask_config_post_bad_upload_dir_returns_400(flask_client):
    client, state, _ = flask_client
    old_config = state.config

    resp = client.post(
        "/hokku/api/config",
        data=json.dumps({"upload_dir": "/nonexistent/path/xyz"}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert "reload failed" in resp.get_json().get("error", "")
    # State must not have changed.
    assert state.config is old_config


def test_flask_config_post_without_config_path_returns_500(app_config: AppConfig):
    """create_app without config_path should return 500 on save attempt."""
    state = _make_state(app_config)
    app = create_app(state, config_path=None, template_folder=None)
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.post(
        "/hokku/api/config",
        data=json.dumps({"orientation": "landscape"}),
        content_type="application/json",
    )
    assert resp.status_code == 500
    assert "config_path" in resp.get_json()["error"]
