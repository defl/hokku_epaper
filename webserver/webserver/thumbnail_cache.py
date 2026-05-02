"""JPEG thumbnails under cache_dir/thumbs/."""
import threading
from pathlib import Path

from PIL import Image, ImageOps


class ThumbnailCache:
    def __init__(self, cache_dir_fn):
        self._cache_dir_fn = cache_dir_fn
        self._lock = threading.Lock()

    def _cache_dir(self) -> Path:
        return self._cache_dir_fn()

    def thumb_path_for(self, img_path: Path) -> Path:
        return self._cache_dir() / "thumbs" / (img_path.stem + "_thumb.jpg")

    def ensure(self, img_path: Path):
        """Build thumbnail if missing or stale. Returns Path or None."""
        thumb_path = self.thumb_path_for(img_path)
        try:
            if thumb_path.exists() and thumb_path.stat().st_mtime >= img_path.stat().st_mtime:
                return thumb_path
        except OSError:
            pass
        with self._lock:
            try:
                if thumb_path.exists() and thumb_path.stat().st_mtime >= img_path.stat().st_mtime:
                    return thumb_path
                thumb_path.parent.mkdir(parents=True, exist_ok=True)
                img = Image.open(img_path)
                img = ImageOps.exif_transpose(img)
                if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                    img = img.convert("RGBA")
                    bg = Image.new("RGB", img.size, (255, 255, 255))
                    bg.paste(img, mask=img.split()[-1])
                    img = bg
                elif img.mode != "RGB":
                    img = img.convert("RGB")
                img.thumbnail((300, 300), Image.LANCZOS)
                img.save(thumb_path, format="JPEG", quality=80)
                return thumb_path
            except Exception as e:
                print(f"  Thumbnail error: {img_path.name}: {e}")
                return None
