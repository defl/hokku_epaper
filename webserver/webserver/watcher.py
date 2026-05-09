"""Background poll loop: periodically calls ImageManager.sync()."""
from __future__ import annotations

import time as _time

from webserver.image_manager import ImageManager


class Watcher:
    """Sync ImageManager on a fixed cadence (config.poll_interval_seconds)."""

    def __init__(self, manager: ImageManager, sleep=_time.sleep) -> None:
        self._manager = manager
        self._sleep = sleep

    def run_forever(self) -> None:
        while True:
            try:
                self._manager.sync()
            except Exception as e:
                print(f"  Watcher error: {e}")
            self._sleep(self._manager.config.poll_interval_seconds)
