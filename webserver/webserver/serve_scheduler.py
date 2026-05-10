"""ServeScheduler: rotation + serve stats + screen telemetry on top of ImageManager.

One DB file (``serve_scheduler.json``) carries all of:
- ``by_name``: per-image rotation pointer + cumulative stats
- ``last_served``: which image was served last (used for time-shown attribution)
- ``screens``: per-screen telemetry (request count, battery, frame state)
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from webserver.image_manager import AbstractImageManager
from webserver.screen_headers import battery_percent, parse_battery_header


_DB_FILENAME = "serve_scheduler.json"


@dataclass(frozen=True)
class ServeStats:
    show_index: int
    last_served_at: float | None
    total_show_count: int
    total_show_minutes: float


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


def _stats_to_dict(s: ServeStats) -> dict:
    return asdict(s)


def _stats_from_dict(d: dict) -> ServeStats:
    return ServeStats(
        show_index=int(d.get("show_index", 0)),
        last_served_at=d.get("last_served_at"),
        total_show_count=int(d.get("total_show_count", 0)),
        total_show_minutes=float(d.get("total_show_minutes", 0.0)),
    )


def _telemetry_to_dict(t: ScreenTelemetryEntry) -> dict:
    return asdict(t)


def _telemetry_from_dict(d: dict) -> ScreenTelemetryEntry:
    return ScreenTelemetryEntry(
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


def _atomic_write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


class ServeScheduler:
    """Fair-rotation scheduler + screen telemetry collector."""

    def __init__(self, manager: AbstractImageManager) -> None:
        self._manager = manager
        self._db_path = Path(manager.config.cache_dir) / _DB_FILENAME
        self._lock = threading.RLock()
        self._stats: dict[str, ServeStats] = {}
        self._screens: dict[str, ScreenTelemetryEntry] = {}
        self._last_served: tuple[str, float] | None = None
        self._load()

    # ── Rotation ─────────────────────────────────────────────────

    def pick_next(self) -> str | None:
        """Lowest show_index among ready images. Random tie-break.

        Reconciles state with manager.list() before picking — adds new entries,
        drops orphans, resets show_index for everyone when a new image appears
        so it gets a fair chance immediately.
        """
        with self._lock:
            ready = [r for r in self._manager.list() if r.convert_status == "ok"]
            ready_names = {r.name for r in ready}
            self._reconcile(ready_names)

            if not ready:
                return None

            entries = [(name, self._stats[name].show_index) for name in ready_names]
            min_idx = min(idx for _, idx in entries)
            candidates = [name for name, idx in entries if idx == min_idx]
            chosen = random.choice(candidates)
            return chosen

    def mark_served(self, name: str) -> None:
        """Bump rotation pointer and stats. Attributes elapsed time to the
        previously-served image."""
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
                last_served=served_name if served_name is not None else (
                    existing.last_served if existing else None
                ),
                battery_mv=bat_mv_value,
                battery_percent=bat_pct,
                battery_seen_at=bat_seen,
                frame_state=fs_with_meta if fs_with_meta is not None else (
                    existing.frame_state if existing else None
                ),
            )
            self._save()

    def screens(self) -> dict[str, ScreenTelemetryEntry]:
        with self._lock:
            return dict(self._screens)

    def remove_screen(self, name: str) -> None:
        """Remove a screen's telemetry and serve-stats records.

        Idempotent — silently does nothing if the name is not known.
        The screen can re-register itself the next time it connects.
        """
        with self._lock:
            self._screens.pop(name, None)
            self._stats.pop(name, None)
            if self._last_served and self._last_served[0] == name:
                self._last_served = None
            self._save()

    # ── Internals ────────────────────────────────────────────────

    def _reconcile(self, ready_names: set[str]) -> None:
        # Drop orphans.
        for name in list(self._stats.keys()):
            if name not in ready_names and name not in {
                r.name for r in self._manager.list()
            }:
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
            cur, total_show_minutes=cur.total_show_minutes + elapsed_min,
        )

    def _load(self) -> None:
        if not self._db_path.exists():
            return
        try:
            with open(self._db_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  Warning: failed to load {_DB_FILENAME}: {e} (starting empty)")
            return
        for name, blob in data.get("by_name", {}).items():
            try:
                self._stats[name] = _stats_from_dict(blob)
            except (KeyError, TypeError, ValueError) as e:
                print(f"  Warning: skipping malformed serve stats for {name!r}: {e}")
        for name, blob in data.get("screens", {}).items():
            try:
                self._screens[name] = _telemetry_from_dict(blob)
            except (KeyError, TypeError, ValueError) as e:
                print(f"  Warning: skipping malformed telemetry entry {name!r}: {e}")
        ls = data.get("last_served")
        if isinstance(ls, dict) and "name" in ls and "served_at" in ls:
            try:
                self._last_served = (ls["name"], float(ls["served_at"]))
            except (TypeError, ValueError):
                pass

    def _save(self) -> None:
        payload = {
            "version": 1,
            "last_served": (
                {"name": self._last_served[0], "served_at": self._last_served[1]}
                if self._last_served else None
            ),
            "by_name": {n: _stats_to_dict(s) for n, s in self._stats.items()},
            "screens": {n: _telemetry_to_dict(t) for n, t in self._screens.items()},
        }
        _atomic_write_json(self._db_path, payload)
