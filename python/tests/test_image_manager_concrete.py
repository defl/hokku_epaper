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
from hokku.webserver import image_manager_single
from hokku.webserver.app_config import AppConfig
from hokku.webserver.image_manager_multi import MultiThreadedImageManager
from hokku.webserver.image_manager_single import SingleThreadedImageManager

_HUESSEN = DISPLAY_REGISTRY["huessen_epf1301"]
TOTAL_BYTES = _HUESSEN.total_bytes


def test_single_threaded_renders_inline_on_calling_thread(
    app_config: AppConfig, make_test_image, monkeypatch
):
    """SingleThreadedImageManager.sync() renders inline on the calling thread.

    Record the thread each render actually runs on rather than sampling the
    process-global ``threading.active_count()`` — that global count is polluted by
    unrelated threads in the (xdist) worker process (PIL/numpy pools, other tests)
    and was flaky. If sync rendered inline, every render ran on the test thread;
    if it had offloaded to a worker, the idents would differ. Immune to process
    noise.
    """
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    make_test_image(upload / "b.png")

    real_render_image_variants = image_manager_single.render_image_variants
    render_threads: list[int] = []

    def recording_render_image_variants(*args, **kwargs):
        render_threads.append(threading.get_ident())
        return real_render_image_variants(*args, **kwargs)

    monkeypatch.setattr(
        image_manager_single, "render_image_variants", recording_render_image_variants
    )

    mgr = SingleThreadedImageManager(app_config)
    caller_ident = threading.get_ident()
    mgr.sync()
    mgr.shutdown()

    assert mgr.resolved_worker_count == 1
    rec_a = mgr.status("a.png")
    rec_b = mgr.status("b.png")
    assert rec_a is not None and rec_b is not None
    assert rec_a.convert_status == "ok"
    assert rec_b.convert_status == "ok"
    # Renders actually happened (one decode-once batch per image, so >= 2 for two
    # images), and every one ran on the calling thread — no worker thread was used.
    assert len(render_threads) >= 2, f"expected renders to run, got {render_threads}"
    assert all(t == caller_ident for t in render_threads), (
        f"render ran off the calling thread: caller={caller_ident} renders={render_threads}"
    )


def test_multi_threaded_runs_in_parallel(app_config: AppConfig, monkeypatch):
    """Two renders dispatched simultaneously both enter the worker function
    before either returns. Uses a 3-party barrier (two workers + the test
    thread) so the workers exit cleanly once we've confirmed concurrency.
    """
    barrier = threading.Barrier(parties=3, timeout=10.0)

    def fake_render_image_variants(_image_path, variants):
        # If both workers reach this barrier the test thread will too.
        # If only one reaches it, the timeout trips a BrokenBarrierError.
        barrier.wait()
        return [
            {
                "ok": True,
                "panel_bytes": b"\x00" * TOTAL_BYTES,
                "preview_bytes": b"\x89PNG\r\n\x1a\n",
            }
            for _ in variants
        ]

    monkeypatch.setattr(
        "hokku.webserver.image_manager_multi.render_image_variants", fake_render_image_variants
    )

    mgr = MultiThreadedImageManager(app_config, worker_count=2)
    try:
        # Two images → two decode-once batches → two executor jobs. Joining the
        # barrier proves both jobs entered the worker concurrently; a serial pool
        # would leave one waiting and trip BrokenBarrierError on timeout.
        mgr._run_batch("a.png", [{"model": "huessen_epf1301", "orientation": "landscape"}])
        mgr._run_batch("b.png", [{"model": "huessen_epf1301", "orientation": "landscape"}])
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
