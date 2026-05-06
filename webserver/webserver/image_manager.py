"""Flat on-disk cache for panel binaries, dithered previews, and grid thumbnails."""
from __future__ import annotations

import hashlib
import threading
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageOps

from webserver.config import AppConfig
from webserver.display_constants import TOTAL_BYTES
from webserver.image import (
    IMAGE_EXTENSIONS,
    DisplayImageConfig,
    PRESET_DISPLAY_IMAGE_CONFIGS,
    convert_image,
    preview_png_for_panel_bytes,
)


def _managed_suffixes() -> tuple[str, str, str]:
    return ("_panel.bin", "_preview.png", "_thumb.jpg")


class ImageManager:
    """Caches panel output and previews under ``cache_dir`` using stem + display slugs."""

    __slots__ = (
        "_config",
        "_thumb_lock",
        "_materialize_locks",
        "_materialize_locks_guard",
        "_upload_paths",
    )

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._thumb_lock = threading.Lock()
        self._materialize_locks: dict[tuple[str, str], threading.Lock] = {}
        self._materialize_locks_guard = threading.Lock()
        self._upload_paths: list[Path] = []
        self.refresh_image_files()

    @property
    def config(self) -> AppConfig:
        return self._config

    @property
    def upload_paths(self) -> tuple[Path, ...]:
        return tuple(self._upload_paths)

    def _cache_root(self) -> Path:
        return Path(self._config.cache_dir).resolve()

    def _display(self, display: DisplayImageConfig | None) -> DisplayImageConfig:
        return self._config.display if display is None else display

    def path_panel_bin(
        self, image_path: Path, display: DisplayImageConfig | None = None
    ) -> Path:
        stem = image_path.stem
        slug = self._display(display).cache_slug()
        return self._cache_root() / f"{stem}_{slug}_panel.bin"

    def path_preview_png(
        self, image_path: Path, display: DisplayImageConfig | None = None
    ) -> Path:
        stem = image_path.stem
        slug = self._display(display).cache_slug()
        return self._cache_root() / f"{stem}_{slug}_preview.png"

    def path_thumb_jpg(self, image_path: Path) -> Path:
        return self._cache_root() / f"{image_path.stem}_thumb.jpg"

    @staticmethod
    def sha1_hex(path: Path) -> str:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def _preset_displays_for_scrub(self) -> list[DisplayImageConfig]:
        out: list[DisplayImageConfig] = []
        for name in PRESET_DISPLAY_IMAGE_CONFIGS:
            for serp in (False, True):
                preset = PRESET_DISPLAY_IMAGE_CONFIGS[name]
                disp = replace(
                    preset,
                    image=replace(
                        preset.image,
                        dither=replace(preset.image.dither, serpentine=serp),
                    ),
                    orientation=self._config.display.orientation,
                )
                out.append(disp)
        return out

    def valid_managed_basenames(self) -> set[str]:
        """All panel / preview / thumb basenames that are allowed for current uploads + presets."""
        valid: set[str] = set()
        for img_path in self._upload_paths:
            stem = img_path.stem
            valid.add(f"{stem}_thumb.jpg")
            for disp in self._preset_displays_for_scrub():
                slug = disp.cache_slug()
                valid.add(f"{stem}_{slug}_panel.bin")
                valid.add(f"{stem}_{slug}_preview.png")
        return valid

    def _scrub_cache_dir(self) -> None:
        root = self._cache_root()
        if not root.exists():
            return
        valid = self.valid_managed_basenames()
        panel_suf, preview_suf, thumb_suf = _managed_suffixes()
        for f in root.iterdir():
            if not f.is_file():
                continue
            name = f.name
            if not (
                name.endswith(panel_suf)
                or name.endswith(preview_suf)
                or name.endswith(thumb_suf)
            ):
                continue
            if name not in valid:
                try:
                    f.unlink()
                    print(f"  Cache: removed stale {name}")
                except OSError as e:
                    print(f"  Cache: could not remove {name}: {e}")

    def refresh_image_files(self) -> None:
        upload_dir = Path(self._config.upload_dir)
        paths: list[Path] = []
        if upload_dir.exists():
            for p in upload_dir.iterdir():
                if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS:
                    paths.append(p)
        paths.sort(key=lambda x: x.name.lower())
        self._upload_paths = paths
        self._scrub_cache_dir()

    def panel_cache_hit(
        self, image_path: Path, display: DisplayImageConfig | None = None
    ) -> bool:
        bin_p = self.path_panel_bin(image_path, display=display)
        png_p = self.path_preview_png(image_path, display=display)
        try:
            if not bin_p.is_file() or not png_p.is_file():
                return False
            return bin_p.stat().st_size == TOTAL_BYTES
        except OSError:
            return False

    def read_panel_bin(
        self, image_path: Path, display: DisplayImageConfig | None = None
    ) -> bytes | None:
        bin_p = self.path_panel_bin(image_path, display=display)
        try:
            if not bin_p.is_file():
                return None
            data = bin_p.read_bytes()
            if len(data) != TOTAL_BYTES:
                return None
            return data
        except OSError:
            return None

    def read_preview_png(
        self, image_path: Path, display: DisplayImageConfig | None = None
    ) -> bytes | None:
        png_p = self.path_preview_png(image_path, display=display)
        try:
            if not png_p.is_file():
                return None
            return png_p.read_bytes()
        except OSError:
            return None

    def _lock_for_materialize(self, stem: str, slug: str) -> threading.Lock:
        key = (stem, slug)
        with self._materialize_locks_guard:
            lock = self._materialize_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._materialize_locks[key] = lock
            return lock

    def materialize_panel_cache(
        self,
        image_path: Path,
        *,
        display: DisplayImageConfig | None = None,
        on_convert_begin=None,
        on_convert_end=None,
    ) -> tuple[bytes, bytes] | None:
        """Ensure panel bin + preview png exist; returns (raw, preview_png) or None on failure."""
        disp = self._display(display)
        stem = image_path.stem
        slug = disp.cache_slug()
        if self.panel_cache_hit(image_path, display=disp):
            raw = self.read_panel_bin(image_path, display=disp)
            prev = self.read_preview_png(image_path, display=disp)
            if raw is not None and prev is not None:
                return raw, prev

        lock = self._lock_for_materialize(stem, slug)
        with lock:
            if self.panel_cache_hit(image_path, display=disp):
                raw = self.read_panel_bin(image_path, display=disp)
                prev = self.read_preview_png(image_path, display=disp)
                if raw is not None and prev is not None:
                    return raw, prev
            if on_convert_begin:
                on_convert_begin(image_path.name)
            try:
                raw_bytes = convert_image(Path(image_path), disp)
                preview_bytes = preview_png_for_panel_bytes(raw_bytes, disp)
            finally:
                if on_convert_end:
                    on_convert_end()
            root = self._cache_root()
            root.mkdir(parents=True, exist_ok=True)
            bin_p = self.path_panel_bin(image_path, display=disp)
            png_p = self.path_preview_png(image_path, display=disp)
            bin_p.write_bytes(raw_bytes)
            png_p.write_bytes(preview_bytes)
            return raw_bytes, preview_bytes

    def ensure_thumb_jpg(self, image_path: Path) -> Path | None:
        thumb_path = self.path_thumb_jpg(image_path)
        try:
            if thumb_path.exists() and thumb_path.stat().st_mtime >= image_path.stat().st_mtime:
                return thumb_path
        except OSError:
            pass
        with self._thumb_lock:
            try:
                if thumb_path.exists() and thumb_path.stat().st_mtime >= image_path.stat().st_mtime:
                    return thumb_path
                root = self._cache_root()
                root.mkdir(parents=True, exist_ok=True)
                img = Image.open(image_path)
                img = ImageOps.exif_transpose(img)
                if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                    img = img.convert("RGBA")
                    bg = Image.new("RGB", img.size, (255, 255, 255))
                    bg.paste(img, mask=img.split()[-1])
                    img = bg
                elif img.mode != "RGB":
                    img = img.convert("RGB")
                img.thumbnail((300, 300), Image.LANCZOS)
                img.save(thumb_path, format="JPEG", quality=85)
                return thumb_path
            except Exception as e:
                print(f"  Thumbnail error: {image_path.name}: {e}")
                return None

    def clear_managed_caches(self) -> None:
        root = self._cache_root()
        if not root.exists():
            return
        panel_suf, preview_suf, thumb_suf = _managed_suffixes()
        for f in list(root.iterdir()):
            if f.is_dir():
                continue
            name = f.name
            if name.endswith(panel_suf) or name.endswith(preview_suf) or name.endswith(thumb_suf):
                try:
                    f.unlink()
                except OSError as e:
                    print(f"  Cache: could not remove {name}: {e}")
        print("  Cache cleared")
