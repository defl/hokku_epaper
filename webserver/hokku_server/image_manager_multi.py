"""MultiThreadedImageManager: renders on a private ThreadPoolExecutor.

Single process; GIL-bound while the dither hot path is pure Python.
Switching to a GIL-releasing implementation later requires no callsite
changes — threading already gives parallelism for free at that point.
"""
from __future__ import annotations

import concurrent.futures

from hokku_server.app_config import AppConfig
from hokku_server.image_manager_abstract import AbstractImageManager


class MultiThreadedImageManager(AbstractImageManager):
    """Renders on a private ``concurrent.futures.ThreadPoolExecutor``."""

    def __init__(
        self,
        config: AppConfig,
        classifier=None,
        worker_count: int = 2,
    ) -> None:
        if worker_count < 1:
            raise ValueError(f"worker_count must be >= 1, got {worker_count}")
        super().__init__(config, classifier)
        self._worker_count = worker_count
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="hokku-render",
        )

    @property
    def resolved_worker_count(self) -> int:
        return self._worker_count

    def _dispatch_render(
        self,
        name: str,
        expected_slug: str,
        render_args: tuple,
        t0: float,
    ) -> None:
        from hokku_server.render_worker import render_one
        future = self._executor.submit(render_one, *render_args)
        future.add_done_callback(
            lambda f, _n=name, _s=expected_slug, _t=t0:
                self._on_render_done(_n, _s, f, _t)
        )

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)
        super().shutdown()
