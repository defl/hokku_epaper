"""Screen-faithful render test.

For every image in images/test/, runs the same B&W-detection → face-detection
→ default dispatch the live server uses, with configs loaded from
config/config.json, then renders at full panel resolution.

Run with:  pytest -m time_intensive -s -k test_render_as_screen
Output (per image):
  build/test_dither_screen/<stem>.png            — dithered panel PNG
  build/test_dither_screen/<stem>_original<ext>  — source copy
  build/test_dither_screen/<stem>.metrics.txt    — sidecar quality metrics
"""
from __future__ import annotations

import json
import shutil
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from hokku_server.app_config import AppConfig
from hokku_server.display import (
    FULL_W,
    PALETTE_MEASURED_RGB,
    PANEL_H,
    TOTAL_BYTES,
    indices_to_preview_rgb,
    panel_bytes_to_indices,
)
from hokku_server.dither_streaming_numba import NumbaStreamingDither
from hokku_server.face_detect_yunet_opencv import OpenCVYuNetFaceDetector
from hokku_server.image_classifier import ImageClassifier
from hokku_server.image_quality import image_compare
from hokku_server.image_renderer import ImageRenderer, open_image_for_render

from tests._helpers import is_oversize_fixture


_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _REPO_ROOT / "webserver" / "config" / "config.json"
_TEST_IMAGES_DIR = _REPO_ROOT / "images" / "test"
_BUILD_SCREEN_DIR = _REPO_ROOT / "build" / "test_dither_screen"

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".heic", ".heif"}


def _load_app_config() -> AppConfig:
    data = json.loads(_CONFIG_PATH.read_text("utf-8"))
    return AppConfig.from_dict(data)


def _test_images() -> list[Path]:
    if not _TEST_IMAGES_DIR.exists():
        return []
    return sorted(
        p for p in _TEST_IMAGES_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in _IMAGE_EXTS
        and not is_oversize_fixture(p)
    )


def _indices_to_landscape_png(idx: np.ndarray) -> bytes:
    rgb = indices_to_preview_rgb(idx)
    img = Image.fromarray(rgb).rotate(90, expand=True)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _fit_source_to_panel_rgb(src: Path) -> tuple[np.ndarray, np.ndarray]:
    """Open source, letterbox-fit to panel dims (landscape), no enhancements.

    Returns (uint8 (PANEL_H, FULL_W, 3), bool (PANEL_H, FULL_W) padding_mask).
    Mirrors _prepare_canvas geometry so source and rendered output are aligned.
    """
    visible_w, visible_h = PANEL_H, FULL_W  # landscape: 1600 wide × 1200 tall
    with open_image_for_render(src) as img:
        src_w, src_h = img.size
        scale = min(visible_w / src_w, visible_h / src_h)
        new_w, new_h = int(src_w * scale), int(src_h * scale)
        img_resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    composed = Image.new("RGB", (visible_w, visible_h), (255, 255, 255))
    x_off = (visible_w - new_w) // 2
    y_off = (visible_h - new_h) // 2
    composed.paste(img_resized, (x_off, y_off))
    img_resized = None

    padding_mask = np.ones((visible_h, visible_w), dtype=bool)
    padding_mask[y_off:y_off + new_h, x_off:x_off + new_w] = False

    composed = composed.rotate(-90, expand=True)
    padding_mask = np.rot90(padding_mask, k=3)

    arr = np.asarray(composed, dtype=np.uint8).copy()
    composed = None
    return arr, padding_mask


def _classify(
    path: Path,
    app_cfg: AppConfig,
    face_detector: OpenCVYuNetFaceDetector | None,
) -> tuple[str, bool | None, bool | None]:
    """Run BW and face detection then return (config_name, is_bw, has_face).

    Mirrors ImageClassifier dispatch order: BW > face > default.
    Both detectors always run when enabled so both observations are recorded.
    """
    is_bw: bool | None = None
    has_face: bool | None = None

    if app_cfg.classifier_bw_detect_enabled:
        with Image.open(path) as img:
            is_bw = ImageClassifier._is_near_grayscale(img)

    if app_cfg.classifier_face_detect_enabled and face_detector is not None:
        has_face = face_detector.has_face(path)

    if app_cfg.classifier_bw_detect_enabled and is_bw:
        config_name = "bw"
    elif app_cfg.classifier_face_detect_enabled and has_face:
        config_name = "face"
    else:
        config_name = "default"

    return config_name, is_bw, has_face


@pytest.mark.time_intensive
def test_render_as_screen() -> None:
    """Full screen-faithful render for every image in images/test/.

    Uses config/config.json for all settings — B&W detection, face detection,
    and the three image configs (default/bw/face) — exactly as the server would.
    Writes per-image PNG and sidecar metrics.txt to build/test_dither_screen/.
    """
    images = _test_images()
    if not images:
        pytest.skip("no test images found")

    app_cfg = _load_app_config()
    _BUILD_SCREEN_DIR.mkdir(parents=True, exist_ok=True)

    face_detector: OpenCVYuNetFaceDetector | None = None
    if app_cfg.classifier_face_detect_enabled:
        face_detector = OpenCVYuNetFaceDetector()

    image_cfg_map = {
        "default": app_cfg.image_config_default,
        "bw": app_cfg.image_config_bw,
        "face": app_cfg.image_config_face,
    }

    orientation = app_cfg.orientation
    crop_threshold = app_cfg.crop_to_fill_threshold
    dither = NumbaStreamingDither()

    for src in images:
        original_dest = _BUILD_SCREEN_DIR / f"{src.stem}_original{src.suffix}"
        if not original_dest.exists():
            shutil.copy2(src, original_dest)

        config_name, is_bw, has_face = _classify(src, app_cfg, face_detector)
        cfg = image_cfg_map[config_name]

        with open_image_for_render(src) as img:
            raw = ImageRenderer(dither).render_panel_bytes(
                img, cfg, orientation, crop_threshold
            )

        assert len(raw) == TOTAL_BYTES, f"{src.name}: unexpected panel bytes length"
        idx = panel_bytes_to_indices(raw)
        assert int(idx.min()) >= 0 and int(idx.max()) <= 5, (
            f"{src.name}: palette indices out of range"
        )

        png_bytes = _indices_to_landscape_png(idx)
        (_BUILD_SCREEN_DIR / f"{src.stem}.png").write_bytes(png_bytes)

        src_arr, padding_mask = _fit_source_to_panel_rgb(src)
        m = image_compare(src_arr, PALETTE_MEASURED_RGB[idx], padding_mask=padding_mask)

        metric_lines = [f"{k}={v:.4f}" for k, v in m.items()]
        sidecar = "\n".join([
            f"image={src.name}",
            f"config={config_name}",
            f"is_bw={is_bw}",
            f"has_face={has_face}",
            f"orientation={orientation}",
            f"crop_to_fill_threshold={crop_threshold}",
            "",
            *metric_lines,
        ]) + "\n"
        (_BUILD_SCREEN_DIR / f"{src.stem}.metrics.txt").write_text(
            sidecar, encoding="utf-8"
        )

        print(
            f"  {src.name}: config={config_name}"
            f"  is_bw={is_bw} has_face={has_face}"
            f"  dE={m['overall_dE']:.2f}"
        )
