"""ImageManager: image lifecycle + on-disk cache.

Outside callers see real filenames ("photo.jpg"). On-disk cache files are
keyed by sha1(name) so odd characters / unicode / length aren't a concern.
The single source of truth for what's known to ImageManager is
``<cache_dir>/image_manager.json``.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Iterable, Literal

from PIL import Image, ImageOps

from webserver.config import AppConfig
from webserver.display import TOTAL_BYTES
from webserver.image import (
    IMAGE_EXTENSIONS,
    ImageConfig,
    open_image_for_render,
    preview_png_from_panel_bytes,
    render_panel_bytes,
)


_DB_FILENAME = "image_manager.json"
_IMAGES_SUBDIR = "images"
_PANEL_SUFFIX = "_panel.bin"
_PREVIEW_SUFFIX = "_preview.png"
_THUMB_SUFFIX = "_thumb.jpg"
_NAME_HASH_LEN = 14
_THUMB_MAX_PX = 300
_THUMB_QUALITY = 85


ConvertStatus = Literal["ok", "failed", "pending"]


@dataclass(frozen=True)
class ImageRecord:
    name: str                          # outside-world identifier
    name_hash: str                     # sha1(name) — on-disk identifier
    original_sha1: str                 # sha1 of file contents
    original_size_bytes: int
    original_mtime: float
    added_at: float
    convert_status: ConvertStatus
    convert_error: str | None
    convert_pipeline_slug: str | None  # cache_slug at successful conversion


@dataclass(frozen=True)
class ConversionProgress:
    current_name: str | None  # being converted right now (None if idle)
    done: int                 # completed this sync cycle
    total: int                # scheduled this sync cycle


def _hash_name(name: str) -> str:
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:_NAME_HASH_LEN]


def _sha1_of_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def _record_to_dict(rec: ImageRecord) -> dict:
    return asdict(rec)


def _record_from_dict(d: dict) -> ImageRecord:
    return ImageRecord(
        name=d["name"],
        name_hash=d["name_hash"],
        original_sha1=d["original_sha1"],
        original_size_bytes=int(d["original_size_bytes"]),
        original_mtime=float(d["original_mtime"]),
        added_at=float(d["added_at"]),
        convert_status=d["convert_status"],
        convert_error=d.get("convert_error"),
        convert_pipeline_slug=d.get("convert_pipeline_slug"),
    )


class ImageManager:
    """Owns upload_dir, cache_dir/images/, and image_manager.json.

    Conversion happens *only* inside ``sync()``. ``panel_bytes()`` and
    ``preview_png()`` are pure cache reads (return None on miss). Outside
    threads can read ``list()``, ``status()`` etc. without locking; writes
    take ``_db_lock``.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._upload_dir = Path(config.upload_dir).resolve()
        self._cache_dir = Path(config.cache_dir).resolve()
        self._images_dir = self._cache_dir / _IMAGES_SUBDIR
        self._db_path = self._cache_dir / _DB_FILENAME
        self._db_lock = threading.RLock()
        self._records: dict[str, ImageRecord] = {}
        self._progress = ConversionProgress(current_name=None, done=0, total=0)

        self._images_dir.mkdir(parents=True, exist_ok=True)
        self._load_db()

    # ── Properties ───────────────────────────────────────────────

    @property
    def config(self) -> AppConfig:
        return self._config

    # ── Lifecycle ────────────────────────────────────────────────

    def sync(self) -> None:
        """Re-scan upload dir, register new files, detect content changes,
        run pending conversions, scrub orphans."""
        with self._db_lock:
            self._reconcile_with_disk()
            pending = [r for r in self._records.values() if r.convert_status == "pending"]
            self._progress = ConversionProgress(current_name=None, done=0, total=len(pending))

        # Run conversions outside the db lock (each conversion takes the lock
        # only when updating the record + writing the db).
        for rec in pending:
            self._run_one_conversion(rec.name)

        if pending:
            total = len(pending)
            ok = sum(
                1 for r in self._records.values()
                if r.name in {p.name for p in pending} and r.convert_status == "ok"
            )
            failed = total - ok
            if failed:
                print(f"  Dithering complete: {ok}/{total} ok, {failed} failed")
            else:
                print(f"  Dithering complete: all {total} image(s) done")

        with self._db_lock:
            self._scrub_orphan_cache_files()
            self._progress = ConversionProgress(current_name=None, done=0, total=0)

    def add(self, name: str, src_bytes: bytes) -> None:
        """Write to upload_dir and register. Raises FileExistsError if name exists."""
        if not name or "/" in name or "\\" in name:
            raise ValueError(f"Invalid image name: {name!r}")
        target = self._upload_dir / name
        with self._db_lock:
            if name in self._records or target.exists():
                raise FileExistsError(
                    f"Image {name!r} already exists; remove it first to replace."
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(src_bytes)
            self._register_new(name, target)
            self._save_db()

    def remove(self, name: str) -> None:
        """Delete original + cached artifacts + db entry. Raises FileNotFoundError if absent."""
        with self._db_lock:
            rec = self._records.get(name)
            if rec is None:
                raise FileNotFoundError(f"Image {name!r} is not registered.")
            self._delete_cache_files(rec.name_hash)
            try:
                (self._upload_dir / name).unlink()
            except FileNotFoundError:
                pass
            del self._records[name]
            self._save_db()

    def retry(self, name: str) -> None:
        """Mark a failed image as pending so the next sync() retries conversion."""
        with self._db_lock:
            rec = self._records.get(name)
            if rec is None:
                raise FileNotFoundError(f"Image {name!r} is not registered.")
            if rec.convert_status != "failed":
                return
            self._records[name] = replace(
                rec, convert_status="pending", convert_error=None,
            )
            self._save_db()

    # ── Reads (lock-free) ───────────────────────────────────────

    def panel_bytes(self, name: str) -> bytes | None:
        rec = self._records.get(name)
        if rec is None or rec.convert_status != "ok":
            return None
        path = self._panel_path(rec)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if len(data) != TOTAL_BYTES:
            return None
        return data

    def preview_png(self, name: str) -> bytes | None:
        rec = self._records.get(name)
        if rec is None or rec.convert_status != "ok":
            return None
        path = self._preview_path(rec)
        try:
            return path.read_bytes()
        except OSError:
            return None

    def thumbnail_jpg(self, name: str) -> bytes | None:
        rec = self._records.get(name)
        if rec is None:
            return None
        thumb_path = self._thumb_path(rec)
        try:
            src_path = self._upload_dir / name
            if (
                thumb_path.exists()
                and thumb_path.stat().st_mtime >= src_path.stat().st_mtime
            ):
                return thumb_path.read_bytes()
        except OSError:
            pass
        # Materialize on first read (thumbnail is pipeline-independent and
        # cheap; safe to do without _db_lock since file write is to its own path).
        try:
            self._materialize_thumbnail(src_path, thumb_path)
            return thumb_path.read_bytes()
        except (OSError, Exception) as e:  # PIL throws assorted exceptions
            print(f"  Thumbnail error for {name}: {e}")
            return None

    def original_path(self, name: str) -> Path:
        if name not in self._records:
            raise FileNotFoundError(f"Image {name!r} is not registered.")
        return self._upload_dir / name

    def list(self) -> list[ImageRecord]:
        return sorted(self._records.values(), key=lambda r: r.name.lower())

    def status(self, name: str) -> ImageRecord | None:
        return self._records.get(name)

    def conversion_progress(self) -> ConversionProgress:
        return self._progress

    # ── Cache control ────────────────────────────────────────────

    def clear_caches(self) -> None:
        """Wipe all cached panel/preview/thumb files and mark every record pending.

        The next sync() rebuilds them.
        """
        with self._db_lock:
            if self._images_dir.exists():
                for f in list(self._images_dir.iterdir()):
                    if f.is_file():
                        try:
                            f.unlink()
                        except OSError as e:
                            print(f"  Cache: could not remove {f.name}: {e}")
            for name, rec in list(self._records.items()):
                self._records[name] = replace(
                    rec,
                    convert_status="pending",
                    convert_error=None,
                    convert_pipeline_slug=None,
                )
            self._save_db()
            print("  Cache cleared")

    # ── Internals ────────────────────────────────────────────────

    def _panel_path(self, rec: ImageRecord) -> Path:
        slug = rec.convert_pipeline_slug or self._config.cache_slug()
        return self._images_dir / f"{rec.name_hash}_{slug}{_PANEL_SUFFIX}"

    def _preview_path(self, rec: ImageRecord) -> Path:
        slug = rec.convert_pipeline_slug or self._config.cache_slug()
        return self._images_dir / f"{rec.name_hash}_{slug}{_PREVIEW_SUFFIX}"

    def _thumb_path(self, rec: ImageRecord) -> Path:
        return self._images_dir / f"{rec.name_hash}{_THUMB_SUFFIX}"

    def _load_db(self) -> None:
        if not self._db_path.exists():
            return
        try:
            with open(self._db_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  Warning: failed to load {_DB_FILENAME}: {e} (starting empty)")
            return
        for name, rec_dict in data.get("images", {}).items():
            try:
                self._records[name] = _record_from_dict(rec_dict)
            except (KeyError, TypeError, ValueError) as e:
                print(f"  Warning: skipping malformed db entry {name!r}: {e}")

    def _save_db(self) -> None:
        payload = {
            "version": 1,
            "pipeline_slug": self._config.cache_slug(),
            "images": {n: _record_to_dict(r) for n, r in self._records.items()},
        }
        _atomic_write_json(self._db_path, payload)

    def _register_new(self, name: str, src_path: Path) -> None:
        st = src_path.stat()
        self._records[name] = ImageRecord(
            name=name,
            name_hash=_hash_name(name),
            original_sha1=_sha1_of_file(src_path),
            original_size_bytes=st.st_size,
            original_mtime=st.st_mtime,
            added_at=time.time(),
            convert_status="pending",
            convert_error=None,
            convert_pipeline_slug=None,
        )

    def _reconcile_with_disk(self) -> None:
        """Sync the in-memory record set against actual upload_dir contents.

        - Files present but not registered: register pending.
        - Registered but missing on disk: drop.
        - File on disk changed (sha1/size/mtime): mark pending and zap stale cache.
        - Pipeline slug changed since last conversion: mark pending.
        """
        if not self._upload_dir.exists():
            print(f"  Warning: upload_dir missing: {self._upload_dir}")
            return

        on_disk: dict[str, Path] = {}
        for p in self._upload_dir.iterdir():
            if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS:
                on_disk[p.name] = p

        # Drop missing.
        for name in list(self._records.keys()):
            if name not in on_disk:
                rec = self._records.pop(name)
                self._delete_cache_files(rec.name_hash)
                print(f"  Removed {name!r} (no longer on disk)")

        current_slug = self._config.cache_slug()

        # Add new + detect changes.
        for name, src_path in on_disk.items():
            existing = self._records.get(name)
            try:
                st = src_path.stat()
            except OSError:
                continue
            if existing is None:
                self._register_new(name, src_path)
                print(f"  Registered new image: {name!r}")
                continue

            content_changed = (
                existing.original_size_bytes != st.st_size
                or existing.original_mtime != st.st_mtime
            )
            if content_changed:
                # Cheap heuristic missed; fall back to sha1.
                new_sha = _sha1_of_file(src_path)
                if new_sha != existing.original_sha1:
                    self._delete_cache_files(existing.name_hash)
                    self._records[name] = replace(
                        existing,
                        original_sha1=new_sha,
                        original_size_bytes=st.st_size,
                        original_mtime=st.st_mtime,
                        convert_status="pending",
                        convert_error=None,
                        convert_pipeline_slug=None,
                    )
                    print(f"  Detected content change: {name!r}")
                    continue

            slug_changed = (
                existing.convert_status == "ok"
                and existing.convert_pipeline_slug != current_slug
            )
            if slug_changed:
                self._records[name] = replace(
                    existing,
                    convert_status="pending",
                    convert_error=None,
                    convert_pipeline_slug=None,
                )
                print(f"  Pipeline slug changed for {name!r}: re-converting")

        self._save_db()

    def _delete_cache_files(self, name_hash: str) -> None:
        if not self._images_dir.exists():
            return
        for f in list(self._images_dir.iterdir()):
            if f.is_file() and f.name.startswith(name_hash + "_"):
                try:
                    f.unlink()
                except OSError as e:
                    print(f"  Cache: could not remove {f.name}: {e}")

    def _scrub_orphan_cache_files(self) -> None:
        """Remove cache files that aren't claimed by any current record."""
        if not self._images_dir.exists():
            return
        valid: set[str] = set()
        current_slug = self._config.cache_slug()
        for rec in self._records.values():
            slug = rec.convert_pipeline_slug or current_slug
            valid.add(f"{rec.name_hash}_{slug}{_PANEL_SUFFIX}")
            valid.add(f"{rec.name_hash}_{slug}{_PREVIEW_SUFFIX}")
            valid.add(f"{rec.name_hash}{_THUMB_SUFFIX}")
        for f in list(self._images_dir.iterdir()):
            if not f.is_file():
                continue
            if f.name.endswith(".tmp"):
                try:
                    f.unlink()
                except OSError:
                    pass
                continue
            if f.name not in valid:
                try:
                    f.unlink()
                    print(f"  Scrubbed orphan cache file: {f.name}")
                except OSError as e:
                    print(f"  Could not scrub {f.name}: {e}")

    def _run_one_conversion(self, name: str) -> None:
        with self._db_lock:
            rec = self._records.get(name)
            if rec is None or rec.convert_status != "pending":
                return
            self._progress = replace(
                self._progress,
                current_name=name,
                done=self._progress.done,
            )

        src_path = self._upload_dir / name
        slug = self._config.cache_slug()

        try:
            with open_image_for_render(src_path) as img:
                panel_bytes = render_panel_bytes(
                    img, self._config.image, self._config.orientation,
                )
            preview_bytes = preview_png_from_panel_bytes(
                panel_bytes, self._config.orientation,
            )
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            print(f"  Conversion failed for {name!r}: {err}")
            traceback.print_exc()
            with self._db_lock:
                cur = self._records.get(name)
                if cur is not None:
                    self._records[name] = replace(
                        cur, convert_status="failed", convert_error=err,
                        convert_pipeline_slug=None,
                    )
                    self._save_db()
                self._progress = replace(
                    self._progress,
                    current_name=None,
                    done=self._progress.done + 1,
                )
            return

        with self._db_lock:
            cur = self._records.get(name)
            if cur is None:
                # Removed mid-conversion; toss the work.
                return
            new_rec = replace(
                cur,
                convert_status="ok",
                convert_error=None,
                convert_pipeline_slug=slug,
            )
            self._records[name] = new_rec
            self._images_dir.mkdir(parents=True, exist_ok=True)
            self._panel_path(new_rec).write_bytes(panel_bytes)
            self._preview_path(new_rec).write_bytes(preview_bytes)
            self._save_db()
            done = self._progress.done + 1
            total = self._progress.total
            print(f"  Dithered {name!r} ({done}/{total})")
            self._progress = replace(
                self._progress,
                current_name=None,
                done=done,
            )

    def _materialize_thumbnail(self, src_path: Path, thumb_path: Path) -> None:
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src_path) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                img = img.convert("RGBA")
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((_THUMB_MAX_PX, _THUMB_MAX_PX), Image.LANCZOS)
            img.save(thumb_path, format="JPEG", quality=_THUMB_QUALITY)
