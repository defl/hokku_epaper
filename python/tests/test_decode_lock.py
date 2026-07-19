"""open_image_for_render must serialize decoding across threads.

libheif (via pillow_heif, for HEIF/HEIC) is not thread-safe: two render workers
decoding HEIF images concurrently deadlock/stall, which made the
MultiThreadedImageManager appear to hang on image sets containing .heic/.heif.
open_image_for_render holds a module lock around the decode so it never runs
concurrently; the parallel win is the numba dither downstream, which stays unlocked.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from hokku.webserver import image_renderer


def test_open_image_for_render_serializes_decode(monkeypatch):
    """Concurrent open_image_for_render calls never enter the decode at the same
    time — the wrapper's lock serializes them."""
    state = {"active": 0, "max": 0}
    guard = threading.Lock()

    def fake_impl(path: Path):
        with guard:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(0.02)  # widen the window a concurrent call could slip into
        with guard:
            state["active"] -= 1
        return object()  # stand-in for a decoded image

    monkeypatch.setattr(image_renderer, "_open_image_for_render", fake_impl)

    threads = [
        threading.Thread(target=lambda: image_renderer.open_image_for_render(Path("x.heic")))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert all(not t.is_alive() for t in threads), "a decode call deadlocked"
    assert state["max"] == 1, f"decode ran concurrently (max {state['max']} at once)"
