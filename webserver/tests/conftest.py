"""Shared test fixtures."""
from __future__ import annotations

import concurrent.futures
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image
from pillow_heif import register_heif_opener

# Register the HEIF/HEIC opener once for the whole test session so that
# PIL.Image.open() works on .heic files in the slow visual tests.
register_heif_opener()

from webserver.app_config import AppConfig
from webserver.dither_config import DitherConfig
from webserver.image_config import ImageConfig
from webserver.presets import PRESET_IMAGE_CONFIGS


@pytest.fixture
def fast_image_config() -> ImageConfig:
    """An ImageConfig that uses the noop kernel — instant dither."""
    base = PRESET_IMAGE_CONFIGS["atkinson"]
    return replace(
        base,
        dither=replace(base.dither, algorithm="noop"),
    )


@pytest.fixture
def app_config(tmp_path: Path, fast_image_config: ImageConfig) -> AppConfig:
    """An AppConfig wired to tmp_path with the noop image config."""
    upload = tmp_path / "uploads"
    cache = tmp_path / "cache"
    upload.mkdir()
    cache.mkdir()
    return AppConfig(
        upload_dir=str(upload),
        cache_dir=str(cache),
        port=18080,
        poll_interval_seconds=1,
        orientation="landscape",
        image_config_default=fast_image_config,
    )


class _InlineRenderPool:
    """Synchronous render pool for unit tests.

    Runs submitted callables in the calling thread (no subprocess/thread
    overhead).  Because ``concurrent.futures.Future.add_done_callback()``
    fires immediately for already-resolved futures, callbacks run inline
    inside ``submit()`` — by the time ``submit()`` returns the task is done.
    """

    resolved_worker_count: int = 1

    def submit(self, fn, *args, **kwargs):
        f: concurrent.futures.Future = concurrent.futures.Future()
        try:
            f.set_result(fn(*args, **kwargs))
        except Exception as exc:
            f.set_exception(exc)
        return f

    def shutdown(self, wait: bool = True) -> None:
        pass


@pytest.fixture
def sync_pool() -> _InlineRenderPool:
    """A render pool that executes tasks synchronously in the calling thread."""
    return _InlineRenderPool()


@pytest.fixture
def make_test_image():
    """Factory for writing a tiny solid-colour image into a path."""
    def _make(path: Path, size=(40, 30), color=(180, 60, 60)) -> Path:
        img = Image.new("RGB", size, color)
        img.save(path)
        return path
    return _make
