"""Fair rotation database (database.json) and pick-next logic."""
import json
import random
from datetime import datetime
from pathlib import Path
from typing import Callable, Union


class ServeDataStore:
    """JSON persistence for serve_data / screens under cache_dir."""

    __slots__ = ("_cache_dir_fn",)

    def __init__(self, cache_dir: Union[Path, Callable[[], Path]]):
        if callable(cache_dir):
            self._cache_dir_fn = cache_dir
        else:
            p = Path(cache_dir)

            def _fn():
                return p

            self._cache_dir_fn = _fn

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir_fn()

    def load(self) -> dict:
        db_path = self.cache_dir / "database.json"
        if db_path.exists():
            try:
                with open(db_path) as f:
                    db = json.load(f)
                if "serve_data" in db:
                    for fname, entry in db["serve_data"].items():
                        if "show_count" in entry and "show_index" not in entry:
                            entry["show_index"] = entry.pop("show_count")
                        if "total_show_count" not in entry:
                            entry["total_show_count"] = 0
                        if "total_show_minutes" not in entry:
                            entry["total_show_minutes"] = 0.0
                    return db
            except (json.JSONDecodeError, OSError) as e:
                print(f"  Warning: failed to load database.json: {e}")
        return {"serve_data": {}}

    def save(self, db: dict) -> None:
        cdir = self.cache_dir
        db_path = cdir / "database.json"
        cdir.mkdir(parents=True, exist_ok=True)
        with open(db_path, "w") as f:
            json.dump(db, f, indent=2)


def track_display_time(serve_data: dict, last_served: dict) -> None:
    prev_name = last_served.get("name")
    prev_time = last_served.get("served_at")
    if prev_name and prev_time and prev_name in serve_data:
        try:
            prev_dt = datetime.fromisoformat(prev_time)
            elapsed_minutes = (datetime.now() - prev_dt).total_seconds() / 60.0
            if 0 < elapsed_minutes < 1440 * 30:
                serve_data[prev_name]["total_show_minutes"] = (
                    serve_data[prev_name].get("total_show_minutes", 0.0) + elapsed_minutes
                )
        except (ValueError, KeyError):
            pass


def pick_next_image(pool: dict, db: dict, last_served: dict):
    """Pick the image with lowest show_index; random tie-break. Mutates db."""
    if not pool:
        return None

    serve_data = db["serve_data"]
    pool_filenames = {Path(k).name for k in pool}

    has_new = any(Path(k).name not in serve_data for k in pool)
    if has_new:
        for fname in list(serve_data.keys()):
            if serve_data[fname]["show_index"] > 0:
                serve_data[fname]["show_index"] = 1
        for k in pool:
            fname = Path(k).name
            if fname not in serve_data:
                serve_data[fname] = {"show_index": 0, "last_request": None,
                                     "total_show_count": 0, "total_show_minutes": 0.0}

    for fname in list(serve_data.keys()):
        if fname not in pool_filenames:
            del serve_data[fname]

    for k in pool:
        fname = Path(k).name
        if fname not in serve_data:
            serve_data[fname] = {"show_index": 0, "last_request": None,
                                 "total_show_count": 0, "total_show_minutes": 0.0}

    pool_entries = []
    for k in pool:
        fname = Path(k).name
        idx = serve_data[fname]["show_index"]
        pool_entries.append((k, fname, idx))

    min_idx = min(e[2] for e in pool_entries)
    candidates = [(k, fname) for k, fname, idx in pool_entries if idx == min_idx]

    chosen_key, chosen_fname = random.choice(candidates)

    track_display_time(serve_data, last_served)

    serve_data[chosen_fname]["show_index"] += 1
    serve_data[chosen_fname]["last_request"] = datetime.now().isoformat(timespec="seconds")
    serve_data[chosen_fname]["total_show_count"] = serve_data[chosen_fname].get("total_show_count", 0) + 1

    return chosen_key


def _load_database(cache_dir):
    return ServeDataStore(Path(cache_dir)).load()


def _save_database(cache_dir, db):
    ServeDataStore(Path(cache_dir)).save(db)
