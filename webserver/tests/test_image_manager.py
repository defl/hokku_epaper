"""ImageManager: hashed filenames, sync, retry, scrub, db survival."""
from __future__ import annotations

from pathlib import Path

import pytest

from webserver.app_config import AppConfig
from webserver.display import TOTAL_BYTES
from webserver.image_manager import ImageManager, _hash_name


def test_hash_name_stable():
    assert _hash_name("photo.jpg") == _hash_name("photo.jpg")
    assert _hash_name("photo.jpg") != _hash_name("photo.png")


def test_register_and_convert(app_config: AppConfig, make_test_image):
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    make_test_image(upload / "b.png")

    mgr = ImageManager(app_config)
    mgr.sync()

    from webserver.screen_image_config import ScreenImageConfig
    records = mgr.list()
    assert [r.name for r in records] == ["a.png", "b.png"]
    assert all(r.convert_status == "ok" for r in records)
    assert all(r.original_sha1 for r in records)
    expected_slug = ScreenImageConfig(
        image_config=app_config.image_config_default,
        orientation=app_config.orientation,
    ).cache_slug()
    assert all(r.screen_image_config_slug == expected_slug for r in records)


def test_panel_bytes_after_sync(app_config: AppConfig, make_test_image):
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    mgr = ImageManager(app_config)
    mgr.sync()
    raw = mgr.panel_bytes("a.png")
    assert raw is not None and len(raw) == TOTAL_BYTES


def test_preview_after_sync(app_config: AppConfig, make_test_image):
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    mgr = ImageManager(app_config)
    mgr.sync()
    png = mgr.preview_png("a.png")
    assert png is not None and png.startswith(b"\x89PNG")


def test_thumbnail_jpg(app_config: AppConfig, make_test_image):
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    mgr = ImageManager(app_config)
    mgr.sync()
    jpg = mgr.thumbnail_jpg("a.png")
    assert jpg is not None and jpg[:3] == b"\xff\xd8\xff"  # JPEG SOI


def test_add_existing_raises(app_config: AppConfig):
    mgr = ImageManager(app_config)
    mgr.add("hello.png", _tiny_png_bytes())
    with pytest.raises(FileExistsError):
        mgr.add("hello.png", _tiny_png_bytes())


def test_remove_missing_raises(app_config: AppConfig):
    mgr = ImageManager(app_config)
    with pytest.raises(FileNotFoundError):
        mgr.remove("nope.png")


def test_remove_clears_cache(app_config: AppConfig, make_test_image):
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    mgr = ImageManager(app_config)
    mgr.sync()
    rec = mgr.status("a.png")
    assert rec is not None
    panel_path = Path(app_config.cache_dir) / "images" / f"{rec.name_hash}_{rec.screen_image_config_slug}_panel.bin"
    assert panel_path.exists()

    mgr.remove("a.png")
    assert not panel_path.exists()
    assert mgr.status("a.png") is None
    assert not (upload / "a.png").exists()


def test_db_survives_restart(app_config: AppConfig, make_test_image):
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    mgr = ImageManager(app_config)
    mgr.sync()
    rec = mgr.status("a.png")

    mgr2 = ImageManager(app_config)
    rec2 = mgr2.status("a.png")
    assert rec2 == rec


def test_disk_change_detected(app_config: AppConfig, make_test_image):
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png", color=(255, 0, 0))
    mgr = ImageManager(app_config)
    mgr.sync()
    sha_before = mgr.status("a.png").original_sha1

    # Replace contents
    make_test_image(upload / "a.png", color=(0, 0, 255))
    mgr.sync()
    sha_after = mgr.status("a.png").original_sha1
    assert sha_before != sha_after


def test_orphan_scrubbed(app_config: AppConfig, make_test_image):
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    mgr = ImageManager(app_config)
    mgr.sync()

    # Plant an orphan file in cache
    orphan = Path(app_config.cache_dir) / "images" / "deadbeefdeadbe_xx_panel.bin"
    orphan.write_bytes(b"x" * TOTAL_BYTES)

    mgr.sync()
    assert not orphan.exists()


def test_retry_on_failed(app_config: AppConfig):
    mgr = ImageManager(app_config)
    # Create a corrupt "image" (non-image bytes with an image extension)
    src = Path(app_config.upload_dir) / "broken.png"
    src.write_bytes(b"not actually a png")

    mgr.sync()
    rec = mgr.status("broken.png")
    assert rec is not None and rec.convert_status == "failed"

    # Retry while still broken — stays failed
    mgr.retry("broken.png")
    mgr.sync()
    assert mgr.status("broken.png").convert_status == "failed"


def test_clear_caches_marks_pending(app_config: AppConfig, make_test_image):
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    mgr = ImageManager(app_config)
    mgr.sync()
    assert mgr.status("a.png").convert_status == "ok"

    mgr.clear_caches()
    assert mgr.status("a.png").convert_status == "pending"
    assert mgr.panel_bytes("a.png") is None

    mgr.sync()
    assert mgr.status("a.png").convert_status == "ok"


def _tiny_png_bytes() -> bytes:
    """Smallest valid 1x1 PNG."""
    from io import BytesIO
    from PIL import Image as _Image
    buf = BytesIO()
    _Image.new("RGB", (1, 1), (0, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()
