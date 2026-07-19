"""The conversion progress counter must not wedge on cache-hit primary renders.

A primary (image, model) render whose panel file already exists is a cache hit:
no render is dispatched, so _on_render_done never runs for it. The lifecycle must
still be completed (pending -> ok, _inflight discharged, progress advanced) or the
image stays 'pending'/in-flight and `conversion_progress()` sticks below total
forever (observed live as a stuck "14/21" while the server was actually idle and
serving correctly).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from hokku.webserver.app_config import AppConfig
from hokku.webserver.image_manager_single import SingleThreadedImageManager


def test_cache_hit_primary_completes_and_counter_clears(app_config: AppConfig, make_test_image):
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")

    mgr = SingleThreadedImageManager(app_config)
    mgr.sync()  # first render populates the panel cache
    mgr.wait_for_idle()
    rec = mgr.status("a.png")
    assert rec is not None and rec.convert_status == "ok"

    # Force it back to 'pending'. Its panel is already cached, so the re-render's
    # primary render is a cache hit — the path that used to skip completion.
    with mgr._db_lock:
        cur = mgr._records["a.png"]
        mgr._records["a.png"] = replace(cur, convert_status="pending")

    mgr.sync()
    mgr.wait_for_idle()

    # Lifecycle completed off the cache hit: status ok, nothing left in flight,
    # and the counter is not wedged below total.
    rec = mgr.status("a.png")
    assert rec is not None and rec.convert_status == "ok"
    assert not mgr._inflight
    prog = mgr.conversion_progress()
    assert prog.done >= prog.total, f"counter wedged: {prog.done}/{prog.total}"

    # A subsequent sync (nothing in flight) resets the counter to idle.
    mgr.sync()
    prog = mgr.conversion_progress()
    assert prog.total == 0 and prog.done == 0

    mgr.shutdown()
