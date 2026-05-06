"""Tests for :func:`webserver.image.convert_image_preview_png` (fast scaled PNG previews)."""
from __future__ import annotations

import shutil
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

import webserver
from webserver.image import (
    PREVIEW_PNG_MAX_INPUT_SIDE,
    PREVIEW_PNG_MAX_PANEL_SIDE,
    convert_image_preview_png,
    default_display_image_config,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE_DIR = REPO_ROOT / "images" / "test"
OUT_DIR = REPO_ROOT / "build" / "images_convert_image_preview_png"

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


def _safe_slug(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in name)


def _fixture_image_paths() -> list[Path]:
    if not IMAGE_DIR.is_dir():
        return []
    return sorted(
        p for p in IMAGE_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES
    )


def test_convert_image_preview_png_synthetic_is_valid_png(tmp_path: Path) -> None:
    path = tmp_path / "tiny.png"
    Image.new("RGB", (640, 480), color=(200, 40, 40)).save(path, format="PNG")
    disp = default_display_image_config()
    png = convert_image_preview_png(path, disp)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 200
    out = Image.open(BytesIO(png))
    assert max(out.size) <= max(PREVIEW_PNG_MAX_INPUT_SIDE, PREVIEW_PNG_MAX_PANEL_SIDE) + 50


@pytest.mark.skipif(not _fixture_image_paths(), reason=f"No raster images under {IMAGE_DIR}")
def test_convert_image_preview_png_fixture_outputs_for_visual_qa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Write originals + one preview PNG per preset × orientation for manual comparison."""
    monkeypatch.setattr("builtins.print", lambda *_a, **_k: None)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    presets = sorted(webserver.PRESET_DISPLAY_IMAGE_CONFIGS.keys())
    orientations = ("landscape", "portrait")

    for img_path in _fixture_image_paths():
        slug = _safe_slug(img_path.stem)
        shutil.copy2(img_path, OUT_DIR / f"{slug}__00_original{img_path.suffix.lower()}")

        for preset_name in presets:
            base = webserver.PRESET_DISPLAY_IMAGE_CONFIGS[preset_name]
            for ori in orientations:
                disp = replace(base, orientation=ori)  # type: ignore[arg-type]
                png_bytes = convert_image_preview_png(img_path, disp)
                preset_slug = preset_name.replace("_", "-")
                out_name = f"{slug}__preview__preset-{preset_slug}__{ori}.png"
                (OUT_DIR / out_name).write_bytes(png_bytes)
