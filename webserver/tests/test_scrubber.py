"""Tests for ImageManager._scrub_orphan_cache_files and clear_caches.

Uses real tmp directories — no mocking of the filesystem — but skips actual
image conversion by writing dummy cache files directly.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from hokku_server.app_config import AppConfig
from hokku_server.image_manager import AbstractImageManager, SingleThreadedImageManager

# Suffixes as defined in image_manager
_PANEL = "_panel.bin"
_PREVIEW = "_preview.png"
_THUMB = "_thumb.jpg"


# ── helpers ──────────────────────────────────────────────────────────────────

def _write(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _images_dir(mgr: AbstractImageManager) -> Path:
    return Path(mgr._config.cache_dir) / "images"


def _slug_for(mgr: AbstractImageManager, name: str) -> str:
    """Return the current screen_image_config_slug for a registered image."""
    return mgr._records[name].screen_image_config_slug


def _name_hash(mgr: AbstractImageManager, name: str) -> str:
    return mgr._records[name].name_hash


def _register_ok(mgr: AbstractImageManager, name: str, make_test_image) -> None:
    """Add an image to the manager via the upload path then fake-convert it."""
    upload = Path(mgr._config.upload_dir)
    make_test_image(upload / name)
    mgr.sync()   # converts (noop kernel — fast; inline pool → synchronous)


# ── always: unknown suffix ────────────────────────────────────────────────────

def test_scrub_always_removes_unknown_suffix(app_config, make_test_image):
    mgr = SingleThreadedImageManager(app_config)
    _register_ok(mgr, "a.png", make_test_image)

    # Drop a file with a foreign suffix in the images dir.
    junk = _images_dir(mgr) / "somefile.xyz"
    _write(junk)

    mgr.sync()
    assert not junk.exists(), "Unknown-suffix file should be scrubbed"


# ── always: orphan (hash unknown) ────────────────────────────────────────────

def test_scrub_always_removes_orphan_hash(app_config, make_test_image):
    mgr = SingleThreadedImageManager(app_config)
    _register_ok(mgr, "a.png", make_test_image)

    # File whose hash prefix doesn't match any registered image.
    orphan = _images_dir(mgr) / f"deadbeef000000_{_slug_for(mgr, 'a.png')}{_PANEL}"
    _write(orphan)

    mgr.sync()
    assert not orphan.exists(), "File with unknown hash should be scrubbed"


def test_scrub_always_removes_orphan_thumb(app_config, make_test_image):
    mgr = SingleThreadedImageManager(app_config)
    _register_ok(mgr, "a.png", make_test_image)

    # Thumb with an unknown hash.
    orphan = _images_dir(mgr) / "deadbeef000000_thumb.jpg"
    _write(orphan)

    mgr.sync()
    assert not orphan.exists(), "Orphan thumb (unknown hash) should be scrubbed"


# ── always: known hash → never remove ────────────────────────────────────────

def test_scrub_always_keeps_current_slug_files(app_config, make_test_image):
    mgr = SingleThreadedImageManager(app_config)
    _register_ok(mgr, "a.png", make_test_image)
    mgr.thumbnail_jpg("a.png")  # thumbnails are generated lazily

    h = _name_hash(mgr, "a.png")
    slug = _slug_for(mgr, "a.png")
    panel   = _images_dir(mgr) / f"{h}_{slug}{_PANEL}"
    preview = _images_dir(mgr) / f"{h}_{slug}{_PREVIEW}"
    thumb   = _images_dir(mgr) / f"{h}{_THUMB}"

    assert thumb.exists(), "Thumbnail should exist after thumbnail_jpg() call"
    mgr.sync()
    assert panel.exists(),   "Current-slug panel.bin should be kept"
    assert preview.exists(), "Current-slug preview.png should be kept"
    assert thumb.exists(),   "Thumbnail should survive scrub"


# ── auto_clear OFF: old-slug files preserved ─────────────────────────────────

def test_scrub_off_keeps_old_slug_files(tmp_path, make_test_image):
    """With auto_clear_cache=False, old-slug files for registered images survive."""
    upload = tmp_path / "up"; upload.mkdir()
    cache  = tmp_path / "ca"; cache.mkdir()

    from hokku_server.presets import PRESET_IMAGE_CONFIGS

    base_cfg = AppConfig(
        upload_dir=str(upload), cache_dir=str(cache), port=18080,
        poll_interval_seconds=1, orientation="landscape",
        auto_clear_cache=False,
        # Disable classifier so the slug tracks image_config_default cleanly.
        classifier_bw_detect_enabled=False,
        classifier_face_detect_enabled=False,
        image_config_default=replace(
            PRESET_IMAGE_CONFIGS["atkinson"],
            dither=replace(PRESET_IMAGE_CONFIGS["atkinson"].dither, algorithm="noop"),
        ),
    )

    mgr = SingleThreadedImageManager(base_cfg)
    make_test_image(upload / "a.png")
    mgr.sync()

    h = _name_hash(mgr, "a.png")
    old_slug = _slug_for(mgr, "a.png")
    assert old_slug is not None, "slug must be set after successful conversion"

    # Pretend the pipeline changed: build a new config with a different slug.
    new_image = replace(
        base_cfg.image_config_default,
        prepare_brightness=0.9,  # changes the slug
        dither=replace(base_cfg.image_config_default.dither, algorithm="noop"),
    )
    new_cfg = replace(base_cfg, image_config_default=new_image)

    # Reload manager with new config — image becomes pending, gets re-converted.
    mgr2 = SingleThreadedImageManager(new_cfg)
    mgr2.sync()

    new_slug = _slug_for(mgr2, "a.png")
    assert new_slug != old_slug, "slug must differ after config change"

    old_panel = Path(cache) / "images" / f"{h}_{old_slug}{_PANEL}"
    new_panel = Path(cache) / "images" / f"{h}_{new_slug}{_PANEL}"

    assert new_panel.exists(), "New-slug panel should exist after re-conversion"
    assert old_panel.exists(), "Old-slug panel should be preserved when auto_clear=False"


# ── auto_clear ON: old-slug files removed ────────────────────────────────────

def test_scrub_on_removes_old_slug_files(tmp_path, make_test_image):
    """With auto_clear_cache=True, old-slug files for registered images are scrubbed."""
    upload = tmp_path / "up"; upload.mkdir()
    cache  = tmp_path / "ca"; cache.mkdir()

    from hokku_server.presets import PRESET_IMAGE_CONFIGS

    base_image = replace(
        PRESET_IMAGE_CONFIGS["atkinson"],
        dither=replace(PRESET_IMAGE_CONFIGS["atkinson"].dither, algorithm="noop"),
    )
    base_cfg = AppConfig(
        upload_dir=str(upload), cache_dir=str(cache), port=18080,
        poll_interval_seconds=1, orientation="landscape",
        auto_clear_cache=True,
        # Disable classifier so the slug tracks image_config_default cleanly.
        classifier_bw_detect_enabled=False,
        classifier_face_detect_enabled=False,
        image_config_default=base_image,
    )

    mgr = SingleThreadedImageManager(base_cfg)
    make_test_image(upload / "a.png")
    mgr.sync()

    h = _name_hash(mgr, "a.png")
    old_slug = _slug_for(mgr, "a.png")
    assert old_slug is not None

    new_image = replace(base_image, prepare_brightness=0.9)
    new_cfg   = replace(base_cfg, image_config_default=new_image, auto_clear_cache=True)

    mgr2 = SingleThreadedImageManager(new_cfg)
    mgr2.sync()

    new_slug = _slug_for(mgr2, "a.png")
    assert new_slug != old_slug, "slug must differ after config change"

    old_panel = Path(cache) / "images" / f"{h}_{old_slug}{_PANEL}"
    new_panel = Path(cache) / "images" / f"{h}_{new_slug}{_PANEL}"

    assert new_panel.exists(), "New-slug panel should exist"
    assert not old_panel.exists(), "Old-slug panel should be scrubbed when auto_clear=True"


def test_scrub_on_keeps_thumb(tmp_path, make_test_image):
    """auto_clear_cache=True must not delete thumbnails."""
    upload = tmp_path / "up"; upload.mkdir()
    cache  = tmp_path / "ca"; cache.mkdir()

    from hokku_server.presets import PRESET_IMAGE_CONFIGS

    base_image = replace(
        PRESET_IMAGE_CONFIGS["atkinson"],
        dither=replace(PRESET_IMAGE_CONFIGS["atkinson"].dither, algorithm="noop"),
    )
    cfg = AppConfig(
        upload_dir=str(upload), cache_dir=str(cache), port=18080,
        poll_interval_seconds=1, orientation="landscape",
        auto_clear_cache=True,
        classifier_bw_detect_enabled=False,
        classifier_face_detect_enabled=False,
        image_config_default=base_image,
    )

    mgr = SingleThreadedImageManager(cfg)
    make_test_image(upload / "a.png")
    mgr.sync()
    # Touch the thumb so it exists.
    mgr.thumbnail_jpg("a.png")

    h = _name_hash(mgr, "a.png")
    thumb = Path(cache) / "images" / f"{h}{_THUMB}"
    assert thumb.exists(), "Thumb should exist after first request"

    # Re-sync with new slug to trigger scrubber with auto_clear=True.
    new_image = replace(base_image, prepare_brightness=0.9)
    new_cfg   = replace(cfg, image_config_default=new_image, auto_clear_cache=True)
    mgr2 = SingleThreadedImageManager(new_cfg)
    mgr2.sync()

    assert thumb.exists(), "Thumbnail must survive auto_clear scrub"


# ── clear_caches: removes everything ─────────────────────────────────────────

def test_clear_caches_removes_panel_preview_thumb(app_config, make_test_image):
    mgr = SingleThreadedImageManager(app_config)
    make_test_image(Path(mgr._config.upload_dir) / "a.png")
    mgr.sync()
    mgr.thumbnail_jpg("a.png")

    h    = _name_hash(mgr, "a.png")
    slug = _slug_for(mgr, "a.png")
    idir = _images_dir(mgr)

    panel   = idir / f"{h}_{slug}{_PANEL}"
    preview = idir / f"{h}_{slug}{_PREVIEW}"
    thumb   = idir / f"{h}{_THUMB}"

    assert panel.exists() and preview.exists() and thumb.exists()

    mgr.clear_caches()

    assert not panel.exists(),   "clear_caches should remove panel.bin"
    assert not preview.exists(), "clear_caches should remove preview.png"
    assert not thumb.exists(),   "clear_caches should remove thumbnail"


def test_clear_caches_marks_all_pending(app_config, make_test_image):
    mgr = SingleThreadedImageManager(app_config)
    make_test_image(Path(mgr._config.upload_dir) / "a.png")
    mgr.sync()
    assert mgr.status("a.png").convert_status == "ok"

    mgr.clear_caches()
    assert mgr.status("a.png").convert_status == "pending"
