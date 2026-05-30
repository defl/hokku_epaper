"""Background flash-job orchestration for the web UI.

Wraps :mod:`hokku.screens.epf1301` (the pure flash ops) in a single-slot,
thread-backed job so a long (~30-60s) flash can run while the browser polls a
status endpoint. Only one flash may run at a time; scanning is refused while a
flash is in progress (the serial port can only be driven by one esptool at a
time, and flashing resets the device).
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from pathlib import Path

from hokku.screens import epf1301

logger = logging.getLogger(__name__)

_job_ids = itertools.count(1)


class FlashJobManager:
    """Owns at most one in-flight flash job."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._job: dict | None = None

    def is_busy(self) -> bool:
        with self._lock:
            return self._job is not None and self._job["state"] == "running"

    def start(self, port: str, config: dict, firmware_path: Path) -> int | None:
        """Begin a flash in a background thread. Returns the job id, or ``None``
        if a flash is already running."""
        with self._lock:
            if self._job is not None and self._job["state"] == "running":
                return None
            job_id = next(_job_ids)
            self._job = {
                "id": job_id,
                "state": "running",
                "port": port,
                "log": [],
                "error": None,
                "result": None,
                "started_at": time.time(),
                "finished_at": None,
            }
            job = self._job
        self._thread = threading.Thread(
            target=self._run,
            args=(job, port, config, firmware_path),
            name=f"flash-{job_id}",
            daemon=True,
        )
        self._thread.start()
        return job_id

    def _append(self, job: dict, line: str) -> None:
        with self._lock:
            job["log"].append(line)

    def _run(self, job: dict, port: str, config: dict, firmware_path: Path) -> None:
        try:
            result = epf1301.flash_device(
                port, config, firmware_path, on_line=lambda ln: self._append(job, ln)
            )
            with self._lock:
                job["result"] = result
                job["state"] = "done"
                job["finished_at"] = time.time()
        except Exception as exc:
            logger.warning("flash job %s failed: %s", job["id"], exc)
            with self._lock:
                job["error"] = str(exc)
                job["state"] = "error"
                job["finished_at"] = time.time()
                job["log"].append(f"ERROR: {exc}")

    def status(self) -> dict | None:
        """Snapshot of the current/last job (log copied), or ``None`` if no job
        has ever started."""
        with self._lock:
            if self._job is None:
                return None
            job = self._job
            return {
                "id": job["id"],
                "state": job["state"],
                "port": job["port"],
                "log": list(job["log"]),
                "error": job["error"],
                "result": job["result"],
                "started_at": job["started_at"],
                "finished_at": job["finished_at"],
            }
