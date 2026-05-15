"""SingleThreadedImageManager: renders inline on the calling thread.

No executor, no extra processes, no extra threads. Cheapest possible memory
profile — right answer for tiny hosts (e.g. a 512 MB Pi Zero 2 W) where
forking a worker process or even an idle thread pool would push the system
into swap.
"""
from __future__ import annotations

import concurrent.futures

from hokku_server.image_manager_abstract import AbstractImageManager
from hokku_server.render_worker import render_one


class SingleThreadedImageManager(AbstractImageManager):
    """Renders inline on the calling thread."""

    @property
    def resolved_worker_count(self) -> int:
        return 1

    def _dispatch_render(
        self,
        name: str,
        expected_slug: str,
        render_args: tuple,
        t0: float,
    ) -> None:
        future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            future.set_result(render_one(*render_args))
        except BaseException as e:
            future.set_exception(e)
        self._on_render_done(name, expected_slug, future, t0)
