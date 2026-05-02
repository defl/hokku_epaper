"""Fair rotation, show-next parity, database.json persistence."""
import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import webserver


class TestFairDistribution:
    def _make_pool(self, names):
        return {f"/images/{n}": {"hash": "abc"} for n in names}

    def _make_entry(self, show_index=0, total_show_count=0, total_show_minutes=0.0):
        return {"show_index": show_index, "last_request": None,
                "total_show_count": total_show_count, "total_show_minutes": total_show_minutes}

    def test_picks_lowest_show_index(self):
        pool = self._make_pool(["a.jpg", "b.jpg", "c.jpg"])
        db = {"serve_data": {
            "a.jpg": self._make_entry(show_index=3),
            "b.jpg": self._make_entry(show_index=1),
            "c.jpg": self._make_entry(show_index=5),
        }}
        webserver.flask_app._last_served = {"key": None, "name": None, "served_at": None}
        key = webserver.flask_app._pick_next_image(pool, db)
        assert Path(key).name == "b.jpg"
        assert db["serve_data"]["b.jpg"]["show_index"] == 2

    def test_new_image_leveling(self):
        pool = self._make_pool(["a.jpg", "b.jpg", "new.jpg"])
        db = {"serve_data": {
            "a.jpg": self._make_entry(show_index=5),
            "b.jpg": self._make_entry(show_index=3),
        }}
        webserver.flask_app._last_served = {"key": None, "name": None, "served_at": None}
        key = webserver.flask_app._pick_next_image(pool, db)
        assert Path(key).name == "new.jpg"
        assert db["serve_data"]["a.jpg"]["show_index"] == 1
        assert db["serve_data"]["b.jpg"]["show_index"] == 1
        assert db["serve_data"]["new.jpg"]["show_index"] == 1

    def test_removes_deleted_images(self):
        pool = self._make_pool(["a.jpg"])
        db = {"serve_data": {
            "a.jpg": self._make_entry(show_index=2),
            "deleted.jpg": self._make_entry(show_index=10),
        }}
        webserver.flask_app._last_served = {"key": None, "name": None, "served_at": None}
        webserver.flask_app._pick_next_image(pool, db)
        assert "deleted.jpg" not in db["serve_data"]

    def test_empty_pool(self):
        db = {"serve_data": {}}
        key = webserver.flask_app._pick_next_image({}, db)
        assert key is None

    def test_random_tiebreak(self):
        pool = self._make_pool(["a.jpg", "b.jpg", "c.jpg"])
        seen = set()
        for _ in range(50):
            db = {"serve_data": {
                "a.jpg": self._make_entry(),
                "b.jpg": self._make_entry(),
                "c.jpg": self._make_entry(),
            }}
            webserver.flask_app._last_served = {"key": None, "name": None, "served_at": None}
            key = webserver.flask_app._pick_next_image(pool, db)
            seen.add(Path(key).name)
        assert len(seen) >= 2

    def test_negative_show_index(self):
        pool = self._make_pool(["a.jpg", "b.jpg"])
        db = {"serve_data": {
            "a.jpg": self._make_entry(show_index=-1),
            "b.jpg": self._make_entry(show_index=3),
        }}
        webserver.flask_app._last_served = {"key": None, "name": None, "served_at": None}
        key = webserver.flask_app._pick_next_image(pool, db)
        assert Path(key).name == "a.jpg"

    def test_total_show_count_increments(self):
        pool = self._make_pool(["a.jpg"])
        db = {"serve_data": {"a.jpg": self._make_entry(total_show_count=5)}}
        webserver.flask_app._last_served = {"key": None, "name": None, "served_at": None}
        webserver.flask_app._pick_next_image(pool, db)
        assert db["serve_data"]["a.jpg"]["total_show_count"] == 6

    def test_updates_last_request(self):
        pool = self._make_pool(["a.jpg"])
        db = {"serve_data": {"a.jpg": self._make_entry()}}
        webserver.flask_app._last_served = {"key": None, "name": None, "served_at": None}
        before = datetime.now().isoformat(timespec="seconds")
        webserver.flask_app._pick_next_image(pool, db)
        after = datetime.now().isoformat(timespec="seconds")
        ts = db["serve_data"]["a.jpg"]["last_request"]
        assert ts is not None
        assert before <= ts <= after


class TestShowNextParity:
    def _make_pool(self, names):
        return {f"/images/{n}": {"hash": "abc"} for n in names}

    def _make_entry(self, show_index=0, total_show_count=0, total_show_minutes=0.0):
        return {"show_index": show_index, "last_request": None,
                "total_show_count": total_show_count, "total_show_minutes": total_show_minutes}

    def _apply_show_next(self, db, filename):
        serve_data = db["serve_data"]
        min_idx = min(e["show_index"] for e in serve_data.values()) if serve_data else 0
        serve_data[filename]["show_index"] = min_idx - 1

    def test_show_next_then_serve_aligns_with_previous_min_tier(self):
        pool = self._make_pool(["a.jpg", "b.jpg"])
        db = {"serve_data": {
            "a.jpg": self._make_entry(show_index=3),
            "b.jpg": self._make_entry(show_index=5),
        }}
        pool_indices_before = [db["serve_data"][Path(k).name]["show_index"] for k in pool]
        L = min(pool_indices_before)
        assert L == 3

        self._apply_show_next(db, "b.jpg")
        assert db["serve_data"]["b.jpg"]["show_index"] == 2

        webserver.flask_app._last_served = {"key": None, "name": None, "served_at": None}
        key = webserver.flask_app._pick_next_image(pool, db)
        assert Path(key).name == "b.jpg"
        assert db["serve_data"]["a.jpg"]["show_index"] == L
        assert db["serve_data"]["b.jpg"]["show_index"] == L

    def test_show_next_then_serve_with_existing_ties(self):
        pool = self._make_pool(["a.jpg", "b.jpg", "c.jpg"])
        db = {"serve_data": {
            "a.jpg": self._make_entry(show_index=3),
            "b.jpg": self._make_entry(show_index=3),
            "c.jpg": self._make_entry(show_index=5),
        }}
        self._apply_show_next(db, "c.jpg")
        assert db["serve_data"]["c.jpg"]["show_index"] == 2

        webserver.flask_app._last_served = {"key": None, "name": None, "served_at": None}
        key = webserver.flask_app._pick_next_image(pool, db)
        assert Path(key).name == "c.jpg"
        assert db["serve_data"]["a.jpg"]["show_index"] == 3
        assert db["serve_data"]["b.jpg"]["show_index"] == 3
        assert db["serve_data"]["c.jpg"]["show_index"] == 3

    def test_show_next_boost_consumed_pool_min_equals_pre_queue_tier(self):
        pool = self._make_pool(["a.jpg", "b.jpg"])
        db = {"serve_data": {
            "a.jpg": self._make_entry(show_index=3),
            "b.jpg": self._make_entry(show_index=5),
        }}
        L = min(db["serve_data"][Path(k).name]["show_index"] for k in pool)
        self._apply_show_next(db, "b.jpg")
        webserver.flask_app._last_served = {"key": None, "name": None, "served_at": None}
        webserver.flask_app._pick_next_image(pool, db)
        pool_mins = [db["serve_data"][Path(k).name]["show_index"] for k in pool]
        assert min(pool_mins) == L
        assert not any(db["serve_data"][Path(k).name]["show_index"] < L for k in pool)


class TestDatabase:
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            db = {"serve_data": {
                "test.jpg": {"show_index": 3, "last_request": "2026-04-14T12:00:00",
                              "total_show_count": 10, "total_show_minutes": 500.0},
            }}
            webserver._save_database(cache_dir, db)
            loaded = webserver._load_database(cache_dir)
            assert loaded["serve_data"]["test.jpg"]["show_index"] == 3
            assert loaded["serve_data"]["test.jpg"]["total_show_count"] == 10
            assert loaded["serve_data"]["test.jpg"]["total_show_minutes"] == 500.0

    def test_load_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = webserver._load_database(Path(tmpdir))
            assert db == {"serve_data": {}}

    def test_load_corrupt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "database.json").write_text("not json{{{")
            db = webserver._load_database(Path(tmpdir))
            assert db == {"serve_data": {}}

    def test_migrate_show_count_to_show_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = {"serve_data": {"img.jpg": {"show_count": 7, "last_request": None}}}
            (Path(tmpdir) / "database.json").write_text(json.dumps(db))
            loaded = webserver._load_database(Path(tmpdir))
            assert "show_index" in loaded["serve_data"]["img.jpg"]
            assert loaded["serve_data"]["img.jpg"]["show_index"] == 7
            assert "show_count" not in loaded["serve_data"]["img.jpg"]
