"""MultiThreadedImageManager: renders on a private ThreadPoolExecutor.

Single process, so all render threads share one address space (and one RAM
budget). The numba dither releases the GIL, so concurrent renders make real
CPU progress; a batch decodes its source once and dithers every variant off
that one buffer.
"""

from __future__ import annotations

import concurrent.futures

from hokku.webserver.app_config import AppConfig
from hokku.webserver.image_manager_abstract import AbstractImageManager
from hokku.webserver.render_worker import render_image_variants


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

    def _run_batch(self, image_path: str, worker_variants: list[dict]) -> concurrent.futures.Future:
        # One executor job per image: decode once, dither every variant. Different
        # images run concurrently across the pool; result routing happens in the
        # shared _submit_image_batch done-callback.
        return self._executor.submit(render_image_variants, image_path, worker_variants)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)
        super().shutdown()
