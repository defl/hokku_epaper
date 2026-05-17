"""Mutable holder for the live config / classifier / manager / scheduler quartet.

A single AppState instance is shared between the Flask app and the Watcher
thread. Calling reload() atomically swaps in new instances built from a new
AppConfig — no process restart required.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

from hokku_server.app_config import AppConfig
from hokku_server.image_classifier import ImageClassifier
from hokku_server.image_manager_abstract import AbstractImageManager
from hokku_server.image_manager_multi import MultiThreadedImageManager
from hokku_server.image_manager_single import SingleThreadedImageManager
from hokku_server.mdns import start_mdns, stop_mdns
from hokku_server.serve_scheduler import ServeScheduler
from hokku_server.worker_count import resolve_worker_count

if TYPE_CHECKING:
    from hokku_server.watcher import Watcher


def build_manager(
    config: AppConfig,
    classifier: ImageClassifier,
) -> AbstractImageManager:
    """Pick the right concrete ImageManager based on ``image_worker_thread_count``.

    ``1`` (default) → SingleThreadedImageManager (renders inline; cheapest).
    ``0`` (auto)    → MultiThreadedImageManager with auto-resolved worker count.
    ``>= 2``        → MultiThreadedImageManager with that many workers.
    """
    if config.image_worker_thread_count == 1:
        return SingleThreadedImageManager(config, classifier)
    worker_count = resolve_worker_count(config.image_worker_thread_count)
    return MultiThreadedImageManager(config, classifier, worker_count=worker_count)


class AppState:
    """Thread-safe container for the live application objects.

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
        manager: AbstractImageManager,
        scheduler: ServeScheduler,
        watcher: "Watcher | None" = None,
        zc: object = None,
    ) -> None:
        self._lock = threading.Lock()
        self.config = config
        self.classifier = classifier
        self.manager = manager
        self.scheduler = scheduler
        self.watcher = watcher
        self._zc = zc  # live Zeroconf instance (None if mDNS disabled)

    def reload(self, new_config: AppConfig) -> None:
        """Rebuild classifier + manager + scheduler from *new_config* and swap atomically.

        Always builds a fresh manager — its render dispatch (inline or
        thread pool) is reconstructed from scratch every reload. The old
        manager is shut down outside the lock.

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

        # Capture old mDNS hostname before swapping config.
        old_hostname = self.config.mdns_hostname

        # Build outside the lock — ImageManager.__init__ reads from disk and
        # may take a moment; we don't want to block route handlers for that.
        new_classifier = ImageClassifier(new_config)
        new_manager = build_manager(new_config, new_classifier)
        new_scheduler = ServeScheduler(new_manager)

        with self._lock:
            self.config = new_config
            self.classifier = new_classifier
            old_manager = self.manager
            self.manager = new_manager
            self.scheduler = new_scheduler

        # Shut the old manager down outside the lock (releases its workers).
        old_manager.shutdown()

        # Restart mDNS if the hostname changed (or toggled on/off).
        if new_config.mdns_hostname != old_hostname:
            stop_mdns(self._zc)
            if new_config.mdns_hostname:
                self._zc = start_mdns(new_config.port, new_config.mdns_hostname)
            else:
                self._zc = None
                logger.info("mDNS disabled (mdns_hostname is empty)")

        logger.info("Config reloaded in-process — pipeline slug: %s", new_config.cache_slug())
        logger.info(
            "Image worker count: configured=%s -> resolved=%s",
            new_config.image_worker_thread_count,
            new_manager.resolved_worker_count,
        )
        if self.watcher is not None:
            self.watcher.wake()  # skip remaining sleep, pick up new config immediately
