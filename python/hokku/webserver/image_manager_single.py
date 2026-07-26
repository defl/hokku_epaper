"""SingleThreadedImageManager: renders inline on the calling thread.

No executor, no extra processes, no extra threads. Cheapest possible memory
profile — right answer for tiny hosts (e.g. a 512 MB Pi Zero 2 W) where
forking a worker process or even an idle thread pool would push the system
into swap.
"""

from __future__ import annotations

import concurrent.futures

from hokku.webserver.image_manager_abstract import AbstractImageManager
from hokku.webserver.render_worker import render_image_variants


class SingleThreadedImageManager(AbstractImageManager):
    """Renders inline on the calling thread."""

    @property
    def resolved_worker_count(self) -> int:
        return 1

    def _run_batch(self, image_path: str, worker_variants: list[dict]) -> concurrent.futures.Future:
        # Run the whole decode-once batch inline on the calling thread and return
        # an already-resolved Future; the shared _submit_image_batch callback then
        # fires synchronously, so sync() stays fully serial with no worker thread.
        future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            future.set_result(render_image_variants(image_path, worker_variants))
        except BaseException as e:  # mirror onto the Future for uniform routing
            future.set_exception(e)
        return future
