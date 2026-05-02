"""Background poll loop that re-syncs the image pool."""


class DirectoryWatcher:
    def __init__(self, *, poll_interval_fn, sync_fn, sleep_fn, on_error_print=print):
        self._poll_interval_fn = poll_interval_fn
        self._sync_fn = sync_fn
        self._sleep_fn = sleep_fn
        self._on_error_print = on_error_print

    def run_forever(self):
        while True:
            try:
                self._sync_fn()
            except Exception as e:
                self._on_error_print(f"  Watcher error: {e}")
            self._sleep_fn(self._poll_interval_fn())
