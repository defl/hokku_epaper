"""Shared test fixtures."""

from __future__ import annotations

import logging
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image
from pillow_heif import register_heif_opener

# Ensure the repo root is on sys.path so that ``tools.*`` modules are importable
# (e.g. ``from tools.screen_sim import fetch_screen`` in test_integration.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Register the HEIF/HEIC opener once for the whole test session so that
# PIL.Image.open() works on .heic files in the slow visual tests.
register_heif_opener()

from hokku.screens import huessen_epf1301, seeedstudio_e1004
from hokku.webserver import image_renderer as _image_renderer
from hokku.webserver.app_config import AppConfig
from hokku.webserver.image_config import ImageConfig
from hokku.webserver.image_manager_abstract import AbstractImageManager
from hokku.webserver.image_manager_multi import MultiThreadedImageManager
from hokku.webserver.image_manager_single import SingleThreadedImageManager
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS

# The decode budget is a process global that build_manager() installs from the
# host's memory budget (huge on a CI/dev box → the full 40 MP cap). Snapshot the
# module default now (before any test builds a manager) and restore it before
# every test, so a manager-building test can't leak its budget into the
# budget-gate tests that assume the ~16 MP default.
_DEFAULT_DECODE_BUDGET_PIXELS = _image_renderer.DECODE_BUDGET_PIXELS


@pytest.fixture(autouse=True)
def _reset_decode_budget():
    _image_renderer.set_decode_budget_pixels(_DEFAULT_DECODE_BUDGET_PIXELS)
    yield


@pytest.fixture(
    params=[huessen_epf1301, seeedstudio_e1004],
    ids=["huessen_epf1301", "seeedstudio_e1004"],
)
def esp32_mod(request):
    """Each of the two ESP32-S3 screen modules (huessen, seeed) in turn.

    The two boards share the entire flash / NVS / OTA / firmware-config code path
    (only flash size and artifact name differ), so every ESP32 flash-layer test is
    run against both — mirroring the firmware's ``common/esp32`` shared test suite.
    The Bigme F7 (XR872) is deliberately excluded: it has its own bootstrap +
    device-local config path, tested separately in ``test_flash_f7.py``.
    """
    return request.param


@pytest.fixture
def fast_image_config() -> ImageConfig:
    """An ImageConfig that uses the noop kernel — instant dither."""
    base = PRESET_IMAGE_CONFIGS["atkinson_hue_aware"]
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
        except Exception as e:
            logging.warning("manager shutdown failed during fixture teardown: %s", e)


@pytest.fixture
def make_test_image():
    """Factory for writing a tiny solid-colour image into a path."""

    def _make(path: Path, size=(40, 30), color=(180, 60, 60)) -> Path:
        img = Image.new("RGB", size, color)
        img.save(path)
        return path

    return _make
