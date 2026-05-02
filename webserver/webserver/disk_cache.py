"""On-disk cache for dithered panel binary + preview PNG."""
import hashlib
import shutil
from collections.abc import Callable
from pathlib import Path

from webserver.config import AppConfig

CACHE_VERSION = "v2"


def hash_file(img_path: Path) -> str:
    h = hashlib.sha1()
    with open(img_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class ImageDiskCache:
    """Caches `{key}.bin` and `{key}.png` under ``{cache_dir}/dc_{app_cache_slug}/``."""

    __slots__ = ("_config_fn", "total_bytes", "version")

    def __init__(
        self,
        config_fn: Callable[[], AppConfig],
        *,
        total_bytes: int,
        version: str = CACHE_VERSION,
    ):
        self._config_fn = config_fn
        self.total_bytes = total_bytes
        self.version = version

    def _base_dir(self) -> Path:
        return Path(self._config_fn().cache_dir).resolve()

    @property
    def cache_dir(self) -> Path:
        """Directory for ``.bin`` / ``.png`` (config-specific subfolder of ``cache_dir``)."""
        cfg = self._config_fn()
        return self._base_dir() / f"dc_{cfg.cache_slug()}"

    def cache_key(self, img_path: Path, content_hash: str, *, dither_slug: str) -> str:
        return f"{img_path.stem}_{content_hash[:12]}_{dither_slug}_{self.version}"

    def try_load(self, img_path: Path, content_hash: str, *, dither_slug: str):
        key = self.cache_key(img_path, content_hash, dither_slug=dither_slug)
        bin_path = self.cache_dir / f"{key}.bin"
        png_path = self.cache_dir / f"{key}.png"
        if bin_path.exists() and png_path.exists():
            raw_bytes = bin_path.read_bytes()
            if len(raw_bytes) == self.total_bytes:
                return raw_bytes, png_path.read_bytes()
        return None

    def save_pair(
        self,
        img_path: Path,
        content_hash: str,
        *,
        dither_slug: str,
        raw_bytes: bytes,
        preview_bytes: bytes,
    ) -> None:
        key = self.cache_key(img_path, content_hash, dither_slug=dither_slug)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / f"{key}.bin").write_bytes(raw_bytes)
        (self.cache_dir / f"{key}.png").write_bytes(preview_bytes)

    def read_binary(self, img_path: Path, content_hash: str, *, dither_slug: str):
        key = self.cache_key(img_path, content_hash, dither_slug=dither_slug)
        bin_path = self.cache_dir / f"{key}.bin"
        if not bin_path.exists():
            return None
        data = bin_path.read_bytes()
        if len(data) != self.total_bytes:
            return None
        return data

    def read_preview(self, img_path: Path, content_hash: str, *, dither_slug: str):
        key = self.cache_key(img_path, content_hash, dither_slug=dither_slug)
        png_path = self.cache_dir / f"{key}.png"
        if not png_path.exists():
            return None
        return png_path.read_bytes()

    def purge_stale(self, valid_keys: set) -> None:
        cache_dir = self.cache_dir
        if not cache_dir.exists():
            return
        valid_files = set()
        for key in valid_keys:
            valid_files.add(f"{key}.bin")
            valid_files.add(f"{key}.png")
        for f in cache_dir.iterdir():
            if f.is_dir():
                continue
            if f.name not in valid_files:
                f.unlink()
                print(f"  Cache: removed stale {f.name}")

    def clear_binaries_and_previews(self) -> None:
        cache_dir = self.cache_dir
        if cache_dir.exists():
            for f in cache_dir.iterdir():
                if f.is_dir():
                    shutil.rmtree(f)
                else:
                    f.unlink()
            print("  Cache cleared")
