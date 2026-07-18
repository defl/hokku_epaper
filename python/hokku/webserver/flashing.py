"""Background flash-job orchestration for the web UI.

Wraps the pure flash ops (per-model, via :mod:`hokku.screens.flasher_registry`)
in a single-slot, thread-backed job so a long (~30-60s) flash can run while the
browser polls a status endpoint. Only one flash may run at a time; scanning is
refused while a flash is in progress (the serial port can only be driven by one
esptool at a time, and flashing resets the device).
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from pathlib import Path

from hokku.screens.bigme_f7 import bootstrap as f7_bootstrap
from hokku.screens.flasher_registry import esp32_screen

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

    def _new_job(self, port: str, kind: str) -> dict | None:
        """Create the single in-flight job (caller holds no lock). Returns the job
        dict, or ``None`` if one is already running."""
        with self._lock:
            if self._job is not None and self._job["state"] == "running":
                return None
            self._job = {
                "id": next(_job_ids),
                "kind": kind,
                "state": "running",
                "port": port,
                "log": [],
                "error": None,
                "result": None,
                "cancel": False,
                "started_at": time.time(),
                "finished_at": None,
            }
            return self._job

    def cancel(self) -> bool:
        """Request the running job stop at its next checkpoint (F7 catch loop only;
        the esptool flash is not interruptible). Returns True if a job was running."""
        with self._lock:
            if self._job is not None and self._job["state"] == "running":
                self._job["cancel"] = True
                return True
            return False

    def start(
        self,
        port: str,
        config: dict,
        firmware_path: Path,
        screen_model: str = "huessen_epf1301",
    ) -> int | None:
        """Begin a flash in a background thread. Returns the job id, or ``None``
        if a flash is already running.

        ``screen_model`` selects which ESP32-S3 screen's flash layout (flash size,
        NVS offsets, artifact name) drives the flash — the two ESP32 boards share a
        USB VID:PID, so the operator's model choice, not the scan, disambiguates."""
        job = self._new_job(port, kind="esp32")
        if job is None:
            return None
        job["screen_model"] = screen_model
        job_id = job["id"]
        logger.info(
            "Flash job #%d starting: model=%s port=%s firmware=%s screen_name=%r ssid=%r url=%s",
            job_id,
            screen_model,
            port,
            Path(firmware_path).name,
            config.get("screen_name", ""),
            config.get("wifi_ssid1", ""),
            config.get("image_url", ""),
        )
        self._thread = threading.Thread(
            target=self._run,
            args=(job, port, config, firmware_path, screen_model),
            name=f"flash-{job_id}",
            daemon=True,
        )
        self._thread.start()
        return job_id

    def start_f7(self, port: str, image_path: Path, provision: dict | None = None) -> int | None:
        """Begin a Bigme F7 (XR872) BROM bootstrap in a background thread.

        Enters the BROM (no-touch ``upgrade`` if the unit runs Hokku firmware, else a
        replug+press catch), writes slot 0, and — if ``provision`` is given — writes
        Wi-Fi/config over the console after a power-cycle. Returns the job id, or
        ``None`` if a flash is already running."""
        job = self._new_job(port, kind="bigme_f7")
        if job is None:
            return None
        job_id = job["id"]
        logger.info(
            "Flash job #%d starting: Bigme F7 bootstrap port=%s image=%s provision=%s",
            job_id,
            port,
            Path(image_path).name,
            bool(provision),
        )
        self._thread = threading.Thread(
            target=self._run_f7,
            args=(job, port, image_path, provision),
            name=f"flash-f7-{job_id}",
            daemon=True,
        )
        self._thread.start()
        return job_id

    def _run_f7(self, job: dict, port: str, image_path: Path, provision: dict | None) -> None:
        try:
            result = f7_bootstrap.bootstrap_device(
                port,
                image_path,
                on_line=lambda ln: self._append(job, ln),
                should_cancel=lambda: bool(job.get("cancel")),
                provision=provision,
            )
            duration = time.time() - job["started_at"]
            with self._lock:
                job["result"] = result
                job["state"] = "done"
                job["finished_at"] = time.time()
            logger.info("Flash job #%d (Bigme F7) done in %.0fs", job["id"], duration)
        except Exception as exc:
            duration = time.time() - job["started_at"]
            logger.error(
                "Flash job #%d (Bigme F7) failed after %.0fs: %s", job["id"], duration, exc
            )
            with self._lock:
                job["error"] = str(exc)
                job["state"] = "error"
                job["finished_at"] = time.time()
                job["log"].append(f"ERROR: {exc}")

    def _append(self, job: dict, line: str) -> None:
        with self._lock:
            job["log"].append(line)

    def _run(
        self, job: dict, port: str, config: dict, firmware_path: Path, screen_model: str
    ) -> None:
        try:
            screen = esp32_screen(screen_model)
            if screen is None:
                raise ValueError(f"unknown ESP32 screen model {screen_model!r}")
            result = screen.flash_device(
                port, config, firmware_path, on_line=lambda ln: self._append(job, ln)
            )
            duration = time.time() - job["started_at"]
            with self._lock:
                job["result"] = result
                job["state"] = "done"
                job["finished_at"] = time.time()
            firmware_ok = (result or {}).get("firmware_current")
            config_ok = (result or {}).get("config_version_ok")
            logger.info(
                "Flash job #%d done in %.0fs: firmware_current=%s config_ok=%s",
                job["id"],
                duration,
                firmware_ok,
                config_ok,
            )
        except Exception as exc:
            duration = time.time() - job["started_at"]
            logger.error("Flash job #%d failed after %.0fs: %s", job["id"], duration, exc)
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
                "kind": job.get("kind", "esp32"),
                "screen_model": job.get("screen_model"),
                "state": job["state"],
                "port": job["port"],
                "log": list(job["log"]),
                "error": job["error"],
                "result": job["result"],
                "started_at": job["started_at"],
                "finished_at": job["finished_at"],
            }
