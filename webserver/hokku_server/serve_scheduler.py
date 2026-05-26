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
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from hokku_server.filesystem import atomic_write_json
from hokku_server.image_manager_abstract import AbstractImageManager
from hokku_server.image_record import ConvertStatus, ImageRecord
from hokku_server.orientation import Orientation
from hokku_server.screen_config import ScreenConfig
from hokku_server.screen_headers import battery_percent, parse_battery_header

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
        self._last_served: tuple[str, float] | None = None
        self._next_for: dict[Orientation, str | None] = {o: None for o in Orientation}
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
                self._next_for = {o: None for o in Orientation}
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
            )
            self._save()

    def screens(self) -> dict[str, ScreenTelemetryEntry]:
        with self._lock:
            return dict(self._screens)

    def remove_screen(self, name: str) -> None:
        """Remove a screen's telemetry, serve-stats, and config records.

        Idempotent — silently does nothing if the name is not known.
        The screen can re-register itself the next time it connects.
        """
        with self._lock:
            self._screens.pop(name, None)
            self._stats.pop(name, None)
            self._screen_configs.pop(name, None)
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

    def get_screen_orientation(self, name: str) -> Orientation:
        """Always returns the effective orientation for a screen.

        Returns the per-screen override if one is set; otherwise falls back
        to the global server orientation from AppConfig.
        """
        cfg = self.get_screen_config(name)
        override = cfg.orientation_override
        return override if override is not None else self._manager.config.orientation

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
                self._next_for[orientation] = min(
                    (r.name for r in eligible),
                    key=lambda n: (self._stats[n].show_index, n),
                )

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
        }
        self._atomic_write_json(payload)
