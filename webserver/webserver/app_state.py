"""Mutable holder for the live config / classifier / manager / scheduler quartet.

A single AppState instance is shared between the Flask app and the Watcher
thread. Calling reload() atomically swaps in new instances built from a new
AppConfig — no process restart required.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from webserver.app_config import AppConfig
from webserver.image_classifier import ImageClassifier
from webserver.image_manager import ImageManager
from webserver.render_pool import RenderPool
from webserver.serve_scheduler import ServeScheduler
from webserver.worker_count import resolve_worker_count

if TYPE_CHECKING:
    from webserver.watcher import Watcher


class AppState:
    """Thread-safe container for the four live application objects.

    Routes and the watcher read ``state.manager`` / ``state.scheduler`` /
    ``state.config`` / ``state.classifier`` directly.  The attributes are
    written only inside ``reload()``, which holds ``_lock`` for the duration
    of the swap — a microsecond-level critical section.  Readers do *not* need
    to acquire the lock: they grab a local reference at the start of a request
    and work with that snapshot, which is safe under the GIL and our
    single-writer pattern.
    """

    def __init__(
        self,
        config: AppConfig,
        classifier: ImageClassifier,
        manager: ImageManager,
        scheduler: ServeScheduler,
        render_pool: RenderPool,
        watcher: "Watcher | None" = None,
    ) -> None:
        self._lock = threading.Lock()
        self.config = config
        self.classifier = classifier
        self.manager = manager
        self.scheduler = scheduler
        self.render_pool = render_pool
        self.watcher = watcher

    def reload(self, new_config: AppConfig) -> None:
        """Rebuild classifier + manager + scheduler from *new_config* and swap atomically.

        The render pool is reused if the resolved worker count is unchanged;
        otherwise the old pool is shut down and a new one is created.

        Validates that upload_dir and cache_dir exist before touching anything,
        so callers can surface a 400 if the new config is unusable.

        Raises:
            ValueError: if upload_dir or cache_dir in *new_config* is missing.
        """
        upload_dir = Path(new_config.upload_dir)
        cache_dir = Path(new_config.cache_dir)
        if not upload_dir.is_dir():
            raise ValueError(f"upload_dir does not exist: {upload_dir}")
        if not cache_dir.is_dir():
            raise ValueError(f"cache_dir does not exist: {cache_dir}")

        new_resolved = resolve_worker_count(new_config.image_worker_thread_count)
        old_resolved = self.render_pool.resolved_worker_count

        # Build outside the lock — ImageManager.__init__ reads from disk and
        # may take a moment; we don't want to block route handlers for that.
        new_classifier = ImageClassifier(new_config)

        if new_resolved == old_resolved:
            new_pool = self.render_pool   # reuse; no lifecycle change
            old_pool = None
        else:
            new_pool = RenderPool(new_resolved)
            old_pool = self.render_pool

        new_manager = ImageManager(new_config, new_classifier, new_pool)
        new_scheduler = ServeScheduler(new_manager)

        with self._lock:
            self.config = new_config
            self.classifier = new_classifier
            self.manager = new_manager
            self.scheduler = new_scheduler
            self.render_pool = new_pool

        # Shut the old pool down outside the lock (may block briefly on in-flight tasks).
        if old_pool is not None:
            old_pool.shutdown(wait=False)

        print(f"  Config reloaded in-process — pipeline slug: {new_config.cache_slug()}")
        print(f"  Image worker count: configured={new_config.image_worker_thread_count} → resolved={new_resolved}")
        if self.watcher is not None:
            self.watcher.wake()  # skip remaining sleep, pick up new config immediately
