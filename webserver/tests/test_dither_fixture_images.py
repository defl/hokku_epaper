"""Dither ``images/test`` fixtures: visual QA + full-size RSS checks (``time_intensive``, skipped by default)."""
from __future__ import annotations

import gc
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageOps

from dataclasses import replace

import webserver
from webserver.display_constants import PALETTE_PREVIEW_RGB
from webserver.dither import dither
from webserver.image import apply_prepare_enhancements, compress_dynamic_range

REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE_DIR = REPO_ROOT / "images" / "test"
OUT_DIR = REPO_ROOT / "build" / "dithering_test"

_RESIZE_MAX_PX = 1024
_MAX_DELTA_BYTES = 50 * 1024 * 1024  # per (full-size image × preset) in RSS test

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


def _resize_max_side(img: Image.Image, max_px: int) -> Image.Image:
    w, h = img.size
    longest = max(w, h)
    if longest <= max_px:
        return img.copy()
    scale = max_px / longest
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    return img.resize((nw, nh), Image.LANCZOS)


def _preview_indices_to_png(result_idx: np.ndarray) -> bytes:
    """RGB preview for arbitrary H×W (tests only; no panel rotation)."""
    preview_rgb = PALETTE_PREVIEW_RGB[np.asarray(result_idx, dtype=np.uint8)]
    buf = BytesIO()
    Image.fromarray(preview_rgb).save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.time_intensive
@pytest.mark.skipif(not _fixture_image_paths(), reason=f"No raster images under {IMAGE_DIR}")
def test_dither_fixture_images_resized_outputs_for_visual_qa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Write resized inputs (max side 1024) and preset previews; canvas matches input size."""
    monkeypatch.setattr("builtins.print", lambda *_a, **_k: None)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for img_path in _fixture_image_paths():
        stem = _safe_slug(img_path.stem)
        pil = ImageOps.exif_transpose(Image.open(img_path).convert("RGB"))
        resized = _resize_max_side(pil, _RESIZE_MAX_PX)
        resized_path = OUT_DIR / f"{stem}__input_resized_max{_RESIZE_MAX_PX}px.png"
        resized.save(resized_path, format="PNG")

        for preset_name in sorted(webserver.PRESET_DITHER_ALGORITHMS.keys()):
            preset = webserver.PRESET_DITHER_ALGORITHMS[preset_name]
            recipe = replace(preset, dither=replace(preset.dither, serpentine=False))
            canvas = apply_prepare_enhancements(resized.copy(), recipe)
            assert canvas.size == resized.size
            arr = np.asarray(canvas, dtype=np.float32)
            compressed = compress_dynamic_range(
                arr,
                scale_chroma=recipe.scale_chroma,
                adaptive_vivid=recipe.adaptive_vivid,
                vivid_chroma_low=recipe.vivid_chroma_low,
                vivid_chroma_high=recipe.vivid_chroma_high,
            )
            result_idx = dither(Image.fromarray(compressed.astype(np.uint8)), recipe.dither)
            assert result_idx.shape == (canvas.size[1], canvas.size[0])
            png_bytes = _preview_indices_to_png(result_idx)
            out_file = OUT_DIR / f"{stem}__preset-{preset_name}__preview.png"
            out_file.write_bytes(png_bytes)

            del canvas, result_idx, png_bytes
            gc.collect()


@pytest.mark.time_intensive
@pytest.mark.skipif(not _fixture_image_paths(), reason=f"No raster images under {IMAGE_DIR}")
def test_dither_fullsize_images_stay_within_memory_budget_per_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Original-resolution pipeline; RSS delta must stay under 50 MiB per image × preset.

    Uses the same-size canvas as the source image (no letterbox), like the default fixture test.

    Not collected by default. Run::

        pytest -m time_intensive
    """
    psutil = pytest.importorskip("psutil")
    monkeypatch.setattr("builtins.print", lambda *_a, **_k: None)

    proc = psutil.Process()

    def rss() -> int:
        return int(proc.memory_info().rss)

    for img_path in _fixture_image_paths():
        pil = ImageOps.exif_transpose(Image.open(img_path).convert("RGB"))

        for preset_name in sorted(webserver.PRESET_DITHER_ALGORITHMS.keys()):
            preset = webserver.PRESET_DITHER_ALGORITHMS[preset_name]
            recipe = replace(preset, dither=replace(preset.dither, serpentine=False))

            gc.collect()
            before = rss()

            canvas = apply_prepare_enhancements(pil.copy(), recipe)
            arr = np.asarray(canvas, dtype=np.float32)
            compressed = compress_dynamic_range(
                arr,
                scale_chroma=recipe.scale_chroma,
                adaptive_vivid=recipe.adaptive_vivid,
                vivid_chroma_low=recipe.vivid_chroma_low,
                vivid_chroma_high=recipe.vivid_chroma_high,
            )
            result_idx = dither(Image.fromarray(compressed.astype(np.uint8)), recipe.dither)
            _ = PALETTE_PREVIEW_RGB[result_idx]

            after = rss()
            delta = after - before
            assert delta < _MAX_DELTA_BYTES, (
                f"RSS grew {delta / (1024 * 1024):.1f} MiB (limit 50 MiB) for "
                f"{img_path.name!r} preset={preset_name!r}"
            )

            del canvas, result_idx, _
            gc.collect()
