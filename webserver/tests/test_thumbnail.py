"""_ensure_thumbnail mode conversion and cache freshness."""
import os
from dataclasses import replace
from unittest.mock import patch

import pytest
from PIL import Image

import webserver


class TestEnsureThumbnail:
    @pytest.fixture
    def tmp_cache(self, tmp_path):
        cache_dir = tmp_path / "cache"
        upload_dir = tmp_path / "upload"
        upload_dir.mkdir()
        cfg = replace(
            webserver.DEFAULT_CONFIG,
            cache_dir=str(cache_dir),
            upload_dir=str(upload_dir),
        )
        with patch.object(webserver.flask_app, "_config", cfg):
            yield {"cache_dir": cache_dir, "upload_dir": upload_dir}

    def _save(self, upload_dir, name, img):
        path = upload_dir / name
        img.save(path)
        return path

    def _assert_valid_jpeg(self, thumb_path):
        assert thumb_path is not None, "_ensure_thumbnail returned None"
        assert thumb_path.exists()
        assert thumb_path.suffix == ".jpg"
        thumb = Image.open(thumb_path)
        assert thumb.format == "JPEG"
        assert thumb.mode == "RGB"
        assert max(thumb.size) <= 300

    def test_rgba_png_does_not_crash(self, tmp_cache):
        img = Image.new("RGBA", (400, 200), (255, 0, 0, 128))
        path = self._save(tmp_cache["upload_dir"], "rgba.png", img)
        self._assert_valid_jpeg(webserver.flask_app._ensure_thumbnail(path))

    def test_la_grayscale_with_alpha(self, tmp_cache):
        img = Image.new("LA", (400, 200), (128, 200))
        path = self._save(tmp_cache["upload_dir"], "la.png", img)
        self._assert_valid_jpeg(webserver.flask_app._ensure_thumbnail(path))

    def test_palette_with_transparency(self, tmp_cache):
        img = Image.new("P", (400, 200))
        img.putpalette([0, 0, 0, 255, 0, 0, 0, 255, 0] + [0] * (256 * 3 - 9))
        img.info["transparency"] = 0
        path = self._save(tmp_cache["upload_dir"], "palette.png", img)
        self._assert_valid_jpeg(webserver.flask_app._ensure_thumbnail(path))

    def test_palette_no_transparency_converts_to_rgb(self, tmp_cache):
        img = Image.new("P", (400, 200))
        img.putpalette([0, 0, 0, 200, 100, 50] + [0] * (256 * 3 - 6))
        path = self._save(tmp_cache["upload_dir"], "p.png", img)
        self._assert_valid_jpeg(webserver.flask_app._ensure_thumbnail(path))

    def test_grayscale_l_mode(self, tmp_cache):
        img = Image.new("L", (400, 200), 128)
        path = self._save(tmp_cache["upload_dir"], "gray.png", img)
        self._assert_valid_jpeg(webserver.flask_app._ensure_thumbnail(path))

    def test_rgb_passthrough(self, tmp_cache):
        img = Image.new("RGB", (400, 200), (10, 20, 30))
        path = self._save(tmp_cache["upload_dir"], "rgb.jpg", img)
        self._assert_valid_jpeg(webserver.flask_app._ensure_thumbnail(path))

    def test_thumbnail_reused_when_fresh(self, tmp_cache):
        img = Image.new("RGB", (400, 200), (10, 20, 30))
        path = self._save(tmp_cache["upload_dir"], "cached.jpg", img)

        thumb1 = webserver.flask_app._ensure_thumbnail(path)
        mtime1 = thumb1.stat().st_mtime_ns

        thumb2 = webserver.flask_app._ensure_thumbnail(path)
        assert thumb2 == thumb1
        assert thumb2.stat().st_mtime_ns == mtime1, "Fresh thumbnail should not be rewritten"

    def test_thumbnail_regenerated_when_source_newer(self, tmp_cache):
        img = Image.new("RGB", (400, 200), (10, 20, 30))
        path = self._save(tmp_cache["upload_dir"], "stale.jpg", img)

        thumb1 = webserver.flask_app._ensure_thumbnail(path)
        old = thumb1.stat().st_mtime - 100
        os.utime(thumb1, (old, old))

        thumb2 = webserver.flask_app._ensure_thumbnail(path)
        assert thumb2.stat().st_mtime > old, "Stale thumbnail should be regenerated"
