"""Implementation-specific tests for the two ImageManager concretes.

The shared API surface is exercised in test_image_manager.py. Tests here
verify the *one* property each concrete is supposed to deliver:

* SingleThreadedImageManager renders inline — no thread pool, no extra
  workers, no callback indirection.
* MultiThreadedImageManager actually parallelises — two simultaneous
  renders both enter the worker function before either returns.
"""

from __future__ import annotations

import threading
from pathlib import Path

from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.app_config import AppConfig
from hokku.webserver.image_manager_multi import MultiThreadedImageManager
from hokku.webserver.image_manager_single import SingleThreadedImageManager
from hokku.webserver.orientation import Orientation

_HUESSEN = DISPLAY_REGISTRY["huessen_epf1301"]
TOTAL_BYTES = _HUESSEN.total_bytes


def test_single_threaded_does_not_spawn_threads(app_config: AppConfig, make_test_image):
    """SingleThreadedImageManager.sync() runs entirely on the calling thread."""
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    make_test_image(upload / "b.png")

    mgr = SingleThreadedImageManager(app_config)
    before = threading.active_count()
    mgr.sync()
    after = threading.active_count()
    mgr.shutdown()

    assert mgr.resolved_worker_count == 1
    rec_a = mgr.status("a.png")
    rec_b = mgr.status("b.png")
    assert rec_a is not None and rec_b is not None
    assert rec_a.convert_status == "ok"
    assert rec_b.convert_status == "ok"
    # ``sync`` may briefly spawn helper threads inside PIL/numpy that exit
    # before sync returns; what we care about is that no persistent worker
    # thread is left running.
    assert after == before, f"thread count grew during sync: before={before} after={after}"


def test_multi_threaded_runs_in_parallel(app_config: AppConfig, monkeypatch):
    """Two renders dispatched simultaneously both enter the worker function
    before either returns. Uses a 3-party barrier (two workers + the test
    thread) so the workers exit cleanly once we've confirmed concurrency.
    """
    barrier = threading.Barrier(parties=3, timeout=10.0)

    def fake_render_one(*_args, **_kwargs):
        # If both workers reach this barrier the test thread will too.
        # If only one reaches it, the timeout trips a BrokenBarrierError.
        barrier.wait()
        return (b"\x00" * TOTAL_BYTES, b"\x89PNG\r\n\x1a\n")

    monkeypatch.setattr("hokku.webserver.image_manager_multi.render_one", fake_render_one)

    mgr = MultiThreadedImageManager(app_config, worker_count=2)
    try:
        mgr._dispatch_render("a.png", "slug", "huessen_epf1301", Orientation.LANDSCAPE, (), 0.0)
        mgr._dispatch_render("b.png", "slug", "huessen_epf1301", Orientation.LANDSCAPE, (), 0.0)
        # Joining the barrier proves both workers entered fake_render_one
        # concurrently. Raises BrokenBarrierError if only one did.
        barrier.wait(timeout=5.0)
    finally:
        mgr.shutdown()


def test_multi_threaded_resolved_worker_count(app_config: AppConfig):
    """resolved_worker_count reports the configured count verbatim."""
    mgr = MultiThreadedImageManager(app_config, worker_count=4)
    try:
        assert mgr.resolved_worker_count == 4
    finally:
        mgr.shutdown()
