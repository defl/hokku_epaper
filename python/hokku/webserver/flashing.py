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

# How long a scanned screen may sit in the bootloader waiting for the flash that
# usually follows. Long enough to fill in the flash form, short enough that a scan
# the operator then abandons does not leave the screen unable to update. The panel
# keeps displaying its last image throughout — e-paper holds without power.
DEFERRED_BOOT_SECONDS = 300.0


class FlashJobManager:
    """Owns at most one in-flight flash job."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._job: dict | None = None
        # A scan drives the same serial port as a flash but is not a job (no
        # thread, no log). It must still hold the single slot so a flash cannot
        # start mid-scan and have two esptools fight over one device.
        self._scanning = False
        # Pending "boot the screen back into its firmware" timers, by port.
        self._boot_timers: dict[str, threading.Timer] = {}

    def arm_deferred_boot(
        self, screen, ports: list[str], delay: float = DEFERRED_BOOT_SECONDS
    ) -> None:
        """Boot *ports* back into firmware after *delay*, unless a flash starts.

        A scan deliberately leaves the device halted in the bootloader: booting it
        would start a full panel repaint (~30-60s) at exactly the moment the
        operator is about to flash, and the flash would interrupt that paint
        mid-refresh and wedge the panel controller. Since a scan is not always
        followed by a flash, this is the safety net that puts the screen back to
        work on its own.
        """
        for port in ports:
            self._cancel_deferred_boot(port)
            timer = threading.Timer(delay, self._deferred_boot, args=(screen, port))
            timer.daemon = True
            with self._lock:
                self._boot_timers[port] = timer
            timer.start()

    def _cancel_deferred_boot(self, port: str | None = None) -> None:
        """Drop pending boot timers — for *port*, or all of them when None."""
        with self._lock:
            ports = [port] if port is not None else list(self._boot_timers)
            timers = [self._boot_timers.pop(p) for p in ports if p in self._boot_timers]
        for t in timers:
            t.cancel()

    def _deferred_boot(self, screen, port: str) -> None:
        """Timer callback: boot the screen if the serial port is free.

        If a flash is running it owns the port and will boot the device itself,
        so this simply steps aside.
        """
        with self._lock:
            self._boot_timers.pop(port, None)
        if not self.begin_scan():
            logger.info("Deferred boot for %s skipped: the serial port is busy", port)
            return
        try:
            logger.info("Scan was not followed by a flash — booting %s back into firmware", port)
            screen.boot_app(port)
        except Exception as e:  # a best-effort recovery must never raise into a timer
            logger.warning("Deferred boot for %s failed: %s", port, e)
        finally:
            self.end_scan()

    def _slot_taken(self) -> bool:
        """Whether the single serial slot is held (caller must hold ``_lock``)."""
        return self._scanning or (self._job is not None and self._job["state"] == "running")

    def is_busy(self) -> bool:
        with self._lock:
            return self._slot_taken()

    def begin_scan(self) -> bool:
        """Reserve the serial slot for a scan. Returns False (and reserves nothing)
        if a flash or another scan already holds it. Pair with :meth:`end_scan`."""
        with self._lock:
            if self._slot_taken():
                return False
            self._scanning = True
            return True

    def end_scan(self) -> None:
        with self._lock:
            self._scanning = False

    def _new_job(self, port: str, kind: str, screen_model: str | None = None) -> dict | None:
        """Create the single in-flight job (caller holds no lock). Returns the job
        dict, or ``None`` if the serial slot is already taken (flash or scan)."""
        # The flash is what the scan was holding the device in the bootloader for,
        # and it boots the screen itself when it finishes.
        self._cancel_deferred_boot(port)
        with self._lock:
            if self._slot_taken():
                return None
            self._job = {
                "id": next(_job_ids),
                "kind": kind,
                "screen_model": screen_model,
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

    def _fail_to_start(self, job: dict, exc: Exception) -> None:
        """Mark a job errored when its worker thread could not be started, so the
        slot frees instead of wedging in ``running`` forever."""
        with self._lock:
            job["error"] = f"could not start flash thread: {exc}"
            job["state"] = "error"
            job["finished_at"] = time.time()
            job["log"].append(f"ERROR: {job['error']}")

    def cancel(self) -> bool:
        """Request the running job stop at its next checkpoint. Only the Bigme F7
        catch loop is interruptible; an esptool flash is not, so cancelling one is
        refused (returns False) rather than falsely reporting success."""
        with self._lock:
            job = self._job
            if job is None or job["state"] != "running":
                return False
            if job.get("kind") != "bigme_f7":
                return False
            job["cancel"] = True
            return True

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
        job = self._new_job(port, kind="esp32", screen_model=screen_model)
        if job is None:
            return None
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
        try:
            self._thread.start()
        except RuntimeError as exc:  # e.g. OS thread exhaustion
            logger.error("Flash job #%d thread failed to start: %s", job_id, exc)
            self._fail_to_start(job, exc)
        return job_id

    def start_f7(self, port: str, image_path: Path, provision: dict | None = None) -> int | None:
        """Begin a Bigme F7 (XR872) BROM bootstrap in a background thread.

        Enters the BROM (no-touch ``upgrade`` if the unit runs Hokku firmware, else a
        replug+press catch), writes slot 0, and — if ``provision`` is given — writes
        Wi-Fi/config over the console after a power-cycle. Returns the job id, or
        ``None`` if a flash is already running."""
        job = self._new_job(port, kind="bigme_f7", screen_model="bigme_f7")
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
        try:
            self._thread.start()
        except RuntimeError as exc:  # e.g. OS thread exhaustion
            logger.error("Flash job #%d (Bigme F7) thread failed to start: %s", job_id, exc)
            self._fail_to_start(job, exc)
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
