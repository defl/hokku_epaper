"""Background poll loop: periodically calls ImageManager.sync()."""
from __future__ import annotations

import time as _time

from webserver.app_state import AppState


class Watcher:
    """Sync the current AppState's manager on a fixed cadence.

    Reads ``state.manager`` on every tick so it automatically follows a hot
    reload — after ``AppState.reload()`` swaps in a new manager, the next
    watcher iteration will sync the new one without any restart.
    """

    def __init__(self, state: AppState, sleep=_time.sleep) -> None:
        self._state = state
        self._sleep = sleep

    def run_forever(self) -> None:
        while True:
            manager = self._state.manager
            try:
                manager.sync()
            except Exception as e:
                print(f"  Watcher error: {e}")
            self._sleep(manager.config.poll_interval_seconds)
