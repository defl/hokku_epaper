"""Background poll loop: periodically calls ImageManager.sync()."""
from __future__ import annotations

import threading
import time

from hokku_server.app_state import AppState


class Watcher:
    """Sync the current AppState's manager on a fixed cadence.

    Reads ``state.manager`` on every tick so it automatically follows a hot
    reload — after ``AppState.reload()`` swaps in a new manager, the next
    iteration syncs the new one without any restart.

    The daemon thread starts immediately on construction.  Call :meth:`wake`
    at any time to interrupt the current sleep and trigger a sync right away
    (e.g. after a config change).  Call :meth:`stop` to signal the thread to
    exit cleanly after its current sync completes::

        watcher = Watcher(state)          # thread running, first sync in progress
        # … config reloaded …
        watcher.wake()                    # skip remaining sleep, sync now
        watcher.stop()                    # signal clean shutdown
    """

    def __init__(self, state: AppState) -> None:
        self._state = state
        self._run = True
        self._wake = threading.Event()
        threading.Thread(
            target=self.run_forever, daemon=True, name="watcher",
        ).start()

    def wake(self) -> None:
        """Interrupt the current sleep and trigger a sync immediately."""
        self._wake.set()

    def stop(self) -> None:
        """Signal the thread to exit after its current sync completes."""
        self._run = False
        self._wake.set()  # interrupt sleep so the exit is prompt

    def run_forever(self) -> None:
        while self._run:
            manager = self._state.manager
            try:
                manager.sync()
            except Exception as e:
                import traceback
                print(f"  Watcher error: {e}")
                traceback.print_exc()
            self._wake.wait(timeout=manager.config.poll_interval_seconds)
            self._wake.clear()
