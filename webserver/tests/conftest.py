"""Shared test fixtures."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image
from pillow_heif import register_heif_opener

# Register the HEIF/HEIC opener once for the whole test session so that
# PIL.Image.open() works on .heic files in the slow visual tests.
register_heif_opener()

from hokku_server.app_config import AppConfig
from hokku_server.dither_config import DitherConfig
from hokku_server.image_config import ImageConfig
from hokku_server.image_manager_abstract import AbstractImageManager
from hokku_server.image_manager_multi import MultiThreadedImageManager
from hokku_server.image_manager_single import SingleThreadedImageManager
from hokku_server.presets import PRESET_IMAGE_CONFIGS


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


@pytest.fixture(params=["single", "multi"])
def image_manager_factory(request):
    """Yields a callable ``(config, classifier=None) -> AbstractImageManager``.

    Parametrised so every test that uses it runs twice — once against
    SingleThreadedImageManager, once against MultiThreadedImageManager
    (worker_count=2). This is the primary correctness gate that both
    implementations honour the abstract API identically.

    Tests block on ``mgr.wait_for_idle()`` after ``mgr.sync()`` to ensure
    multi-threaded callbacks have completed before assertions.
    """
    created: list[AbstractImageManager] = []

    def _make(config: AppConfig, classifier=None) -> AbstractImageManager:
        if request.param == "single":
            mgr: AbstractImageManager = SingleThreadedImageManager(config, classifier)
        else:
            mgr = MultiThreadedImageManager(config, classifier, worker_count=2)
        created.append(mgr)
        return mgr

    yield _make

    for mgr in created:
        try:
            mgr.shutdown()
        except Exception:
            pass


@pytest.fixture
def make_test_image():
    """Factory for writing a tiny solid-colour image into a path."""
    def _make(path: Path, size=(40, 30), color=(180, 60, 60)) -> Path:
        img = Image.new("RGB", size, color)
        img.save(path)
        return path
    return _make
