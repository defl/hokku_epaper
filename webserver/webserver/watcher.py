"""Background poll loop: periodically calls ImageManager.sync()."""
from __future__ import annotations

import threading
import time

from webserver.app_state import AppState


class Watcher:
    """Sync the current AppState's manager on a fixed cadence.

    Reads ``state.manager`` on every tick so it automatically follows a hot
    reload — after ``AppState.reload()`` swaps in a new manager, the next
    watcher iteration will sync the new one without any restart.

    The thread is built in the constructor but not started.  Call
    :meth:`start` once everything is ready so the first sync fires
    immediately.  Call :meth:`stop` to signal the thread to exit cleanly
    after its current sleep::

        watcher = Watcher(state)
        # … finish building app …
        watcher.start()   # first sync begins immediately, non-blocking

        # On teardown / hot-reload:
        watcher.stop()    # signals thread to exit after its next sleep
    """

    def __init__(self, state: AppState) -> None:
        self._state = state
        self._run = True
        self._thread = threading.Thread(
            target=self.run_forever, daemon=True, name="watcher",
        )

    def start(self) -> None:
        """Start the watcher thread; first sync begins immediately."""
        self._thread.start()

    def stop(self) -> None:
        """Signal the thread to exit cleanly after its current sleep."""
        self._run = False

    def run_forever(self) -> None:
        while self._run:
            manager = self._state.manager
            try:
                manager.sync()
            except Exception as e:
                print(f"  Watcher error: {e}")
            time.sleep(manager.config.poll_interval_seconds)
