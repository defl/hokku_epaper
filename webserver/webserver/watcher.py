"""Background poll loop: periodically calls ImageManager.sync()."""
from __future__ import annotations

import threading
import time as _time

from webserver.app_state import AppState


class Watcher:
    """Sync the current AppState's manager on a fixed cadence.

    Reads ``state.manager`` on every tick so it automatically follows a hot
    reload — after ``AppState.reload()`` swaps in a new manager, the next
    watcher iteration will sync the new one without any restart.

    The daemon thread is started immediately on construction and waits for
    :meth:`kick` before running its first sync.  Call ``kick()`` once
    everything is built so the first sync starts without delay::

        watcher = Watcher(state)
        # … finish building app …
        watcher.kick()   # first sync begins immediately, non-blocking
    """

    def __init__(self, state: AppState, sleep=_time.sleep) -> None:
        self._state = state
        self._sleep = sleep
        self._ready = threading.Event()
        threading.Thread(
            target=self.run_forever, daemon=True, name="watcher",
        ).start()

    def kick(self) -> None:
        """Signal the thread to begin its first sync immediately."""
        self._ready.set()

    def run_forever(self) -> None:
        self._ready.wait()
        while True:
            manager = self._state.manager
            try:
                manager.sync()
            except Exception as e:
                print(f"  Watcher error: {e}")
            self._sleep(manager.config.poll_interval_seconds)
