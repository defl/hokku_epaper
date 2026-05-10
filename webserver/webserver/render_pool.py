"""Lazy long-lived ProcessPoolExecutor for image renders.

Workers are spawned on the first ``submit()`` call and recycled after
``IDLE_TIMEOUT_S`` seconds of inactivity (no new submits) to release
memory back to the OS.  A subsequent ``submit()`` after recycling
re-spawns the pool transparently.

Multiprocessing context
-----------------------
* Linux / macOS: ``fork``.  Workers inherit the parent's virtual address
  space copy-on-write, so cached LUTs and PIL/numpy DLLs already loaded
  in the parent are shared pages — additional physical RSS per worker is
  only the ~50 MB render delta.
* Windows: ``spawn`` (fork is not available).  Each worker starts a fresh
  Python interpreter; cost is higher (~130 MB), but this is dev-only pain.

Thread safety
-------------
All public methods are safe to call from any thread.  ``_pool_lock``
serialises pool creation / destruction.

Usage::

    pool = RenderPool(worker_count=2)
    future = pool.submit(my_fn, arg1, arg2)
    result = future.result()          # blocks until done
    pool.shutdown(wait=True)
"""
from __future__ import annotations

import concurrent.futures
import sys
import threading
import time
from typing import Any, Callable


def _get_mp_context():
    import multiprocessing
    method = "fork" if sys.platform != "win32" else "spawn"
    return multiprocessing.get_context(method)


class RenderPool:
    """Lazily-initialised, self-recycling ``ProcessPoolExecutor``.

    Parameters
    ----------
    worker_count:
        Maximum number of parallel worker processes.  Must be >= 1.
    idle_timeout_s:
        Seconds of inactivity after which the pool shuts itself down.
        Defaults to 300 s (5 minutes).  Pass a small value in tests.
    """

    IDLE_TIMEOUT_S: float = 300.0

    def __init__(self, worker_count: int, *, idle_timeout_s: float | None = None) -> None:
        if worker_count < 1:
            raise ValueError(f"worker_count must be >= 1, got {worker_count}")
        self._worker_count = worker_count
        self._idle_timeout_s = idle_timeout_s if idle_timeout_s is not None else self.IDLE_TIMEOUT_S

        self._pool_lock = threading.Lock()
        self._pool: concurrent.futures.ProcessPoolExecutor | None = None
        self._last_submit_time: float = 0.0
        self._shutdown_requested = False

        self._watcher: threading.Thread | None = None

    # ── public API ────────────────────────────────────────────────────────

    @property
    def resolved_worker_count(self) -> int:
        """The configured (resolved) worker count."""
        return self._worker_count

    def submit(self, fn: Callable, *args: Any, **kwargs: Any) -> concurrent.futures.Future:
        """Submit a callable for execution in a worker process.

        Spawns the pool lazily on first call (or after recycling).
        Returns a :class:`concurrent.futures.Future`.
        """
        with self._pool_lock:
            if self._shutdown_requested:
                raise RuntimeError("RenderPool has been shut down")
            if self._pool is None:
                self._pool = concurrent.futures.ProcessPoolExecutor(
                    max_workers=self._worker_count,
                    mp_context=_get_mp_context(),
                )
                self._start_watcher()
            self._last_submit_time = time.monotonic()
            return self._pool.submit(fn, *args, **kwargs)

    def shutdown(self, wait: bool = True) -> None:
        """Shut the pool down.  Safe to call multiple times."""
        with self._pool_lock:
            self._shutdown_requested = True
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=wait)
        # Watcher thread will exit because _shutdown_requested is True.

    # ── internals ─────────────────────────────────────────────────────────

    def _start_watcher(self) -> None:
        """Start a daemon thread that recycles the pool after idle timeout.

        Must be called while holding ``_pool_lock``.
        """
        t = threading.Thread(target=self._idle_watcher, daemon=True)
        t.start()
        self._watcher = t

    def _idle_watcher(self) -> None:
        """Periodically check for idleness and shut down the pool when idle."""
        check_interval = max(1.0, self._idle_timeout_s / 10)
        while True:
            time.sleep(check_interval)
            with self._pool_lock:
                if self._shutdown_requested:
                    return
                if self._pool is None:
                    return
                idle_for = time.monotonic() - self._last_submit_time
                if idle_for >= self._idle_timeout_s:
                    # Recycle: shut down the pool but keep _shutdown_requested False
                    # so the next submit() can re-create it.
                    pool, self._pool = self._pool, None
                    pool.shutdown(wait=False)
                    return  # watcher exits; a new one starts on next submit()
