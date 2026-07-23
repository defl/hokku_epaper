"""ServeScheduler: rotation + serve stats + screen telemetry on top of ImageManager.

One DB file (``serve_scheduler.json``) carries all of:
- ``by_name``: per-image rotation pointer + cumulative stats
- ``last_served``: which image was served last (used for time-shown attribution)
- ``screens``: per-screen telemetry (request count, battery, frame state)
- ``next_for``: pre-computed next image per orientation (LANDSCAPE, PORTRAIT, NEUTRAL)
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from hokku.webserver.filesystem import atomic_write_json
from hokku.webserver.image_manager_abstract import AbstractImageManager
from hokku.webserver.image_record import ConvertStatus, ImageRecord
from hokku.webserver.orientation import Orientation
from hokku.webserver.screen_config import ScreenConfig
from hokku.webserver.screen_headers import battery_percent, parse_battery_header

logger = logging.getLogger(__name__)


_DB_FILENAME = "serve_scheduler.json"


@dataclass(frozen=True)
class ServeStats:
    show_index: int
    last_served_at: float | None
    total_show_count: int
    total_show_minutes: float

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ServeStats:
        return cls(
            show_index=int(d.get("show_index", 0)),
            last_served_at=d.get("last_served_at"),
            total_show_count=int(d.get("total_show_count", 0)),
            total_show_minutes=float(d.get("total_show_minutes", 0.0)),
        )


@dataclass(frozen=True)
class ScreenTelemetryEntry:
    ip: str
    request_count: int
    last_seen_at: float
    last_sleep_seconds: int | None
    last_served: str | None
    battery_mv: int | None
    battery_percent: int | None
    battery_seen_at: float | None
    frame_state: dict | None
    last_log: str
    last_log_at: float
    firmware_version: str | None
    firmware_build: str | None
    # Last OTA config-migration failure for this screen. A non-None value signals
    # a should-never-happen bug (the server could not build a config for the new
    # firmware schema); surfaced prominently in the dashboard. Cleared once the
    # screen successfully reports the bundled firmware version.
    ota_error: str | None = None
    ota_error_at: float | None = None
    # Hardware model this screen self-reported via X-Screen-Model (e.g.
    # "huessen_epf1301", "bigme_f7"). None until the screen first identifies.
    screen_model: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ScreenTelemetryEntry:
        return cls(
            ip=d.get("ip", ""),
            request_count=int(d.get("request_count", 0)),
            last_seen_at=float(d.get("last_seen_at", 0.0)),
            last_sleep_seconds=d.get("last_sleep_seconds"),
            last_served=d.get("last_served"),
            battery_mv=d.get("battery_mv"),
            battery_percent=d.get("battery_percent"),
            battery_seen_at=d.get("battery_seen_at"),
            frame_state=d.get("frame_state"),
            last_log=d.get("last_log", ""),
            last_log_at=float(d.get("last_log_at", 0.0)),
            firmware_version=d.get("firmware_version"),
            firmware_build=d.get("firmware_build"),
            ota_error=d.get("ota_error"),
            ota_error_at=d.get("ota_error_at"),
            screen_model=d.get("screen_model"),
        )


class ServeScheduler:
    """Fair-rotation scheduler + screen telemetry collector."""

    def __init__(self, manager: AbstractImageManager) -> None:
        self._manager = manager
        self._db_path = Path(manager.config.cache_dir) / _DB_FILENAME
        self._lock = threading.RLock()
        self._stats: dict[str, ServeStats] = {}
        self._screens: dict[str, ScreenTelemetryEntry] = {}
        self._screen_configs: dict[str, ScreenConfig] = {}
        # Screens whose user has toggled "update firmware on next refresh" ON.
        # For a version UPGRADE we keep signalling until the device reports the
        # target version (bounded auto-retry, so a dropped download self-heals);
        # for a same-version RE-FLASH we can't confirm by version, so it stays a
        # one-shot (consumed via take_ota_pending). _ota_reflash marks the latter;
        # _ota_attempts counts signals for the retry cap. All persisted so a
        # pending request survives a server restart.
        self._ota_pending: set[str] = set()
        self._ota_reflash: set[str] = set()
        self._ota_attempts: dict[str, int] = {}
        self._last_served: tuple[str, float] | None = None
        self._next_for: dict[Orientation, str | None] = dict.fromkeys(Orientation, None)
        self._load()
        # Pre-determine the next image right now so the UI can show it
        # immediately without waiting for the first screen request.
        with self._lock:
            ready = [r for r in self._manager.list() if r.convert_status == ConvertStatus.OK]
            if ready:
                ready_names = {r.name for r in ready}
                self._reconcile(ready_names)
                self._precompute_all_locked(ready)

    # ── Rotation ─────────────────────────────────────────────────

    def pick_next(self, orientation: Orientation) -> str | None:
        """Return the pre-determined next image for the given orientation filter.

        orientation=NEUTRAL means no filter — returns the global best next image.
        Reconciles state with manager.list() before returning — adds new
        entries, drops orphans, resets show_index for everyone when a new
        image appears so it gets a fair chance immediately.
        """
        with self._lock:
            ready = [r for r in self._manager.list() if r.convert_status == ConvertStatus.OK]
            ready_names = {r.name for r in ready}
            self._reconcile(ready_names)

            if not ready:
                self._next_for = dict.fromkeys(Orientation, None)
                self._save()
                return None

            # If the pre-computed choice for this orientation is still valid, honour it.
            if self._next_for.get(orientation) in ready_names:
                return self._next_for[orientation]

            # Pre-computed choice is stale or absent — recompute all orientations.
            self._precompute_all_locked(ready)
            self._save()
            return self._next_for.get(orientation)

    def mark_served(self, name: str) -> None:
        """Bump rotation pointer and stats. Attributes elapsed time to the
        previously-served image. Pre-computes the next image for all orientations
        so the UI reflects the upcoming choice immediately."""
        with self._lock:
            now = time.time()
            self._attribute_show_time(now)

            cur = self._stats.get(name) or ServeStats(0, None, 0, 0.0)
            self._stats[name] = ServeStats(
                show_index=cur.show_index + 1,
                last_served_at=now,
                total_show_count=cur.total_show_count + 1,
                total_show_minutes=cur.total_show_minutes,
            )
            self._last_served = (name, now)
            # Consumed — recompute the next image for all orientations immediately.
            ready = [r for r in self._manager.list() if r.convert_status == ConvertStatus.OK]
            ready_names = {r.name for r in ready}
            self._reconcile(ready_names)
            self._precompute_all_locked(ready)
            self._save()

    # ── Stats retrieval ──────────────────────────────────────────

    def stats(self) -> dict[str, ServeStats]:
        with self._lock:
            return dict(self._stats)

    def stats_for(self, name: str) -> ServeStats | None:
        with self._lock:
            return self._stats.get(name)

    def last_served(self) -> tuple[str, float] | None:
        with self._lock:
            return self._last_served

    def peek_next(self, orientation: Orientation) -> str | None:
        """Return the pre-determined next image for the given orientation without consuming it."""
        with self._lock:
            return self._next_for.get(orientation)

    def set_next(self, name: str) -> None:
        """Force a specific image to be served next (overrides rotation order).

        Raises ValueError if the image is not currently ready to serve.
        """
        logger.info("Pinning next image: %r", name)
        with self._lock:
            ready = {r.name for r in self._manager.list() if r.convert_status == ConvertStatus.OK}
            if name not in ready:
                raise ValueError(f"Image {name!r} is not ready to serve")
            # Override all orientation slots to the forced image (it will be
            # filtered by orientation at serve time if a filter is active).
            for o in Orientation:
                self._next_for[o] = name
            self._save()

    # ── Screen telemetry ─────────────────────────────────────────

    def record_screen_call(
        self,
        screen_name: str,
        screen_ip: str,
        sleep_seconds: int,
        served_name: str | None,
        battery_mv: int | None,
        frame_state: dict | None,
        log: str | None = None,
        firmware_version: str | None = None,
        firmware_build: str | None = None,
        screen_model: str | None = None,
    ) -> None:
        with self._lock:
            now = time.time()
            existing = self._screens.get(screen_name)
            req_count = (existing.request_count + 1) if existing else 1

            # Frame-state may carry a more reliable battery reading.
            if frame_state and isinstance(frame_state.get("bat_mv"), (int, float)):
                fs_mv = parse_battery_header(str(int(frame_state["bat_mv"])))
                if fs_mv is not None:
                    battery_mv = fs_mv

            bat_pct = None
            bat_mv_value = existing.battery_mv if existing else None
            bat_seen = existing.battery_seen_at if existing else None
            if battery_mv is not None and battery_mv > 0:
                bat_mv_value = int(battery_mv)
                bat_pct = battery_percent(battery_mv)
                bat_seen = now
            elif existing:
                bat_pct = existing.battery_percent

            fs_with_meta = None
            if frame_state:
                fs_with_meta = dict(frame_state)
                clk_now = frame_state.get("clk_now")
                if isinstance(clk_now, (int, float)) and clk_now > 0:
                    fs_with_meta["clk_drift_s"] = int(clk_now - now)
                fs_with_meta["seen_at"] = now

            last_log = existing.last_log if existing else ""
            last_log_at = existing.last_log_at if existing else 0.0
            if log:
                last_log = log
                last_log_at = now

            self._screens[screen_name] = ScreenTelemetryEntry(
                ip=screen_ip,
                request_count=req_count,
                last_seen_at=now,
                last_sleep_seconds=int(sleep_seconds),
                last_served=served_name
                if served_name is not None
                else (existing.last_served if existing else None),
                battery_mv=bat_mv_value,
                battery_percent=bat_pct,
                battery_seen_at=bat_seen,
                frame_state=fs_with_meta
                if fs_with_meta is not None
                else (existing.frame_state if existing else None),
                last_log=last_log,
                last_log_at=last_log_at,
                firmware_version=firmware_version
                or (existing.firmware_version if existing else None),
                firmware_build=firmware_build or (existing.firmware_build if existing else None),
                # OTA error is sticky across normal polls; cleared explicitly via
                # clear_ota_error (e.g. once the screen reports the new version).
                ota_error=existing.ota_error if existing else None,
                ota_error_at=existing.ota_error_at if existing else None,
                screen_model=screen_model or (existing.screen_model if existing else None),
            )
            self._save()

    def screens(self) -> dict[str, ScreenTelemetryEntry]:
        with self._lock:
            return dict(self._screens)

    def known_models(self) -> set[str]:
        """Set of distinct hardware models across all screens seen so far.

        Only screens that have self-identified via X-Screen-Model contribute;
        screens with no reported model are omitted.  The image manager uses
        this to decide which per-model binaries to render.
        """
        with self._lock:
            return {t.screen_model for t in self._screens.values() if t.screen_model is not None}

    def get_screen_model(self, name: str) -> str | None:
        """The hardware model a screen last self-identified as, or None if unseen."""
        with self._lock:
            t = self._screens.get(name)
            return t.screen_model if t else None

    # ── OTA: per-screen update request + migration errors ─────────

    def set_ota_pending(self, name: str, enabled: bool, reflash: bool = False) -> None:
        """Set/clear the 'update firmware on next refresh' flag.

        ``reflash=True`` marks a same-version re-flash (device already on the
        target version) — signalled one-shot, since success can't be confirmed
        by a version change. Otherwise it's an upgrade: signalled repeatedly
        until the device reports the target version, capped by note_ota_signal.
        """
        logger.info("OTA pending for %r -> %s (reflash=%s)", name, enabled, reflash)
        with self._lock:
            if enabled:
                self._ota_pending.add(name)
                if reflash:
                    self._ota_reflash.add(name)
                else:
                    self._ota_reflash.discard(name)
                self._ota_attempts[name] = 0
            else:
                self._clear_ota_pending_locked(name)
            self._save()

    def is_ota_pending(self, name: str) -> bool:
        """Whether the screen is flagged to update on its next refresh (no consume)."""
        with self._lock:
            return name in self._ota_pending

    def is_ota_reflash(self, name: str) -> bool:
        """Whether the pending update is a same-version re-flash (one-shot)."""
        with self._lock:
            return name in self._ota_reflash

    def note_ota_signal(self, name: str) -> int:
        """Record that an upgrade was signalled; return the running attempt count."""
        with self._lock:
            n = self._ota_attempts.get(name, 0) + 1
            self._ota_attempts[name] = n
            self._save()
            return n

    def clear_ota_pending(self, name: str) -> None:
        """Clear the pending flag + retry state (on confirmed success or give-up)."""
        with self._lock:
            if name in self._ota_pending or name in self._ota_attempts:
                self._clear_ota_pending_locked(name)
                self._save()

    def take_ota_pending(self, name: str) -> bool:
        """Consume the pending flag: returns True once, then clears it (one-shot)."""
        with self._lock:
            if name in self._ota_pending:
                self._clear_ota_pending_locked(name)
                self._save()
                return True
            return False

    def _clear_ota_pending_locked(self, name: str) -> None:
        self._ota_pending.discard(name)
        self._ota_reflash.discard(name)
        self._ota_attempts.pop(name, None)

    def get_screen_firmware_version(self, name: str) -> str | None:
        """The screen's currently-running firmware version. Prefers the value from
        the X-Firmware-Version header (same source serve_binary compares against),
        falling back to the frame-state fw field."""
        with self._lock:
            t = self._screens.get(name)
            if t is None:
                return None
            if t.firmware_version:
                return t.firmware_version
            if t.frame_state and isinstance(t.frame_state.get("fw"), str):
                return t.frame_state["fw"]
            return None

    def record_ota_error(self, name: str, msg: str) -> None:
        """Record an OTA config-migration failure on the screen's record.

        Creates a minimal entry if the screen is otherwise unknown so the error
        is never lost. A non-None ``ota_error`` signals a should-never-happen bug.
        """
        logger.error("OTA config migration failed for %r: %s", name, msg)
        with self._lock:
            now = time.time()
            existing = self._screens.get(name)
            if existing is not None:
                self._screens[name] = replace(existing, ota_error=msg, ota_error_at=now)
            else:
                self._screens[name] = ScreenTelemetryEntry(
                    ip="unknown",
                    request_count=0,
                    last_seen_at=now,
                    last_sleep_seconds=None,
                    last_served=None,
                    battery_mv=None,
                    battery_percent=None,
                    battery_seen_at=None,
                    frame_state=None,
                    last_log="",
                    last_log_at=0.0,
                    firmware_version=None,
                    firmware_build=None,
                    ota_error=msg,
                    ota_error_at=now,
                )
            self._save()

    def clear_ota_error(self, name: str) -> None:
        """Clear any recorded OTA error for the screen (idempotent)."""
        with self._lock:
            existing = self._screens.get(name)
            if existing is not None and existing.ota_error is not None:
                self._screens[name] = replace(existing, ota_error=None, ota_error_at=None)
                self._save()

    def remove_screen(self, name: str) -> None:
        """Remove a screen's telemetry, serve-stats, and config records.

        Idempotent — silently does nothing if the name is not known.
        The screen can re-register itself the next time it connects.
        """
        logger.info("Removing screen telemetry: %r", name)
        with self._lock:
            self._screens.pop(name, None)
            self._stats.pop(name, None)
            self._screen_configs.pop(name, None)
            self._clear_ota_pending_locked(name)
            if self._last_served and self._last_served[0] == name:
                self._last_served = None
            self._save()

    # ── Per-screen config ─────────────────────────────────────────

    def get_screen_config(self, name: str) -> ScreenConfig:
        """Return the full config for a screen (default ScreenConfig if not set)."""
        with self._lock:
            return self._screen_configs.get(name, ScreenConfig())

    def set_screen_config(self, name: str, config: ScreenConfig) -> None:
        """Persist the full config for a screen."""
        with self._lock:
            self._screen_configs[name] = config
            self._save()

    # ── Internals ────────────────────────────────────────────────

    def _atomic_write_json(self, payload: dict) -> None:
        atomic_write_json(self._db_path, payload)

    def _precompute_all_locked(self, ready: list[ImageRecord]) -> None:
        """Pre-compute the next image for every orientation value.

        NEUTRAL = unfiltered (best across all images).
        LANDSCAPE/PORTRAIT = best among images matching that orientation filter.
        Must be called under self._lock.
        """
        for orientation in Orientation:
            if orientation == Orientation.NEUTRAL:
                eligible = ready
            else:
                eligible = [r for r in ready if r.matches_orientation_filter(orientation)]
            if not eligible:
                self._next_for[orientation] = None
            else:
                # Pick the least-shown index, then break ties randomly rather
                # than alphabetically — otherwise every daily rotation reset
                # (see _reconcile) replays the same name-sorted prefix first.
                min_idx = min(self._stats[r.name].show_index for r in eligible)
                tied = [r.name for r in eligible if self._stats[r.name].show_index == min_idx]
                self._next_for[orientation] = random.choice(tied)

    def _reconcile(self, ready_names: set[str]) -> None:
        # Drop orphans.
        for name in list(self._stats.keys()):
            if name not in ready_names and name not in {r.name for r in self._manager.list()}:
                del self._stats[name]

        # Add fresh entries. If we see any genuinely new name, reset all
        # nonzero indices to 1 so the new image isn't perpetually behind.
        currently_known = set(self._stats.keys())
        truly_new = ready_names - currently_known
        if truly_new:
            for n in list(self._stats.keys()):
                if self._stats[n].show_index > 0:
                    self._stats[n] = replace(self._stats[n], show_index=1)
        for name in truly_new:
            self._stats[name] = ServeStats(0, None, 0, 0.0)

    def _attribute_show_time(self, now: float) -> None:
        if self._last_served is None:
            return
        prev_name, prev_time = self._last_served
        elapsed_min = (now - prev_time) / 60.0
        if not (0 < elapsed_min < 60 * 24 * 30):  # sanity bound
            return
        cur = self._stats.get(prev_name)
        if cur is None:
            return
        self._stats[prev_name] = replace(
            cur,
            total_show_minutes=cur.total_show_minutes + elapsed_min,
        )

    def _load(self) -> None:
        if not self._db_path.exists():
            return
        try:
            with open(self._db_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load %s: %s (starting empty)", _DB_FILENAME, e)
            return
        for name, blob in data.get("by_name", {}).items():
            try:
                self._stats[name] = ServeStats.from_dict(blob)
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("Skipping malformed serve stats for %r: %s", name, e)
        for name, blob in data.get("screens", {}).items():
            try:
                self._screens[name] = ScreenTelemetryEntry.from_dict(blob)
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("Skipping malformed telemetry entry %r: %s", name, e)
        for name, blob in data.get("screen_configs", {}).items():
            try:
                self._screen_configs[name] = ScreenConfig.from_dict(blob)
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("Skipping malformed screen config for %r: %s", name, e)
        pending = data.get("ota_pending")
        if isinstance(pending, list):
            self._ota_pending = {n for n in pending if isinstance(n, str)}
        reflash = data.get("ota_reflash")
        if isinstance(reflash, list):
            self._ota_reflash = {n for n in reflash if isinstance(n, str)} & self._ota_pending
        attempts = data.get("ota_attempts")
        if isinstance(attempts, dict):
            self._ota_attempts = {
                n: v for n, v in attempts.items() if isinstance(n, str) and isinstance(v, int)
            }
        ls = data.get("last_served")
        if isinstance(ls, dict) and "name" in ls and "served_at" in ls:
            try:
                self._last_served = (ls["name"], float(ls["served_at"]))
            except (TypeError, ValueError):
                pass
        # Load next_for; migrate from old "next_image" key if needed.
        next_for_raw = data.get("next_for")
        if isinstance(next_for_raw, dict):
            for o in Orientation:
                val = next_for_raw.get(o.value)
                self._next_for[o] = val if isinstance(val, str) else None
        else:
            old_next = data.get("next_image")
            if isinstance(old_next, str):
                self._next_for[Orientation.NEUTRAL] = old_next

    def _save(self) -> None:
        payload = {
            "version": 1,
            "next_for": {o.value: self._next_for.get(o) for o in Orientation},
            "last_served": (
                {"name": self._last_served[0], "served_at": self._last_served[1]}
                if self._last_served
                else None
            ),
            "by_name": {n: s.to_dict() for n, s in self._stats.items()},
            "screens": {n: t.to_dict() for n, t in self._screens.items()},
            "screen_configs": {n: c.to_dict() for n, c in self._screen_configs.items()},
            "ota_pending": sorted(self._ota_pending),
            "ota_reflash": sorted(self._ota_reflash),
            "ota_attempts": dict(self._ota_attempts),
        }
        self._atomic_write_json(payload)
