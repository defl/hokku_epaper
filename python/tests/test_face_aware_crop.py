"""Tests for face-aware cropping.

Unit tests (always fast — synthetic images/bboxes only):
  - _face_centered_crop_offset: interior (unclamped) offset follows the exact
    centering formula on both axes, and both top/bottom clamps fire.
  - Multiple faces: the offset follows the union centroid, NOT just the first
    face and NOT a plain center-crop (guarded against both).
  - End-to-end: a face patch near the top of a tall image is cropped away by a
    plain center-crop but kept in frame with face-aware cropping (asserted on
    the actual rendered pixels).
  - No offset when crop_anchor_bboxes_norm is falsy (plain center-crop, same
    as today's behaviour — pixel-identical).
  - face_crop_bboxes is included in ScreenImageConfig.cache_slug() only when set
    (keeps existing cache slugs stable for everyone with the feature off).
  - AppConfig.classifier_face_aware_crop_enabled round-trips and defaults to False.

Slow visual test (marked time_intensive):
  - Renders every portrait test image (has faces) at a few aggressive
    crop-to-fill thresholds, with and without face-aware cropping, for both
    orientations, to build/test_face_aware_crop/ for visual comparison.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.app_config import AppConfig
from hokku.webserver.bounding_box import BoundingBox
from hokku.webserver.dither_config import DitherConfig
from hokku.webserver.dither_streaming_numba import NumbaStreamingDither
from hokku.webserver.face_detect_yunet_opencv import OpenCVYuNetFaceDetector
from hokku.webserver.image_abc import _face_centered_crop_offset, preview_png_from_panel_bytes
from hokku.webserver.image_config import ImageConfig
from hokku.webserver.image_renderer import ImageRenderer, open_image_for_render
from hokku.webserver.orientation import Orientation
from hokku.webserver.presets import FALLBACK_PRESET, PRESET_IMAGE_CONFIGS
from hokku.webserver.screen_image_config import ScreenImageConfig

_HUESSEN = DISPLAY_REGISTRY["huessen_epf1301"]


def _noop_cfg() -> ImageConfig:
    base = PRESET_IMAGE_CONFIGS[FALLBACK_PRESET]
    return replace(
        base,
        prepare_autocontrast_cutoff=0.0,
        prepare_gamma=1.0,
        prepare_brightness=1.0,
        prepare_contrast=1.0,
        prepare_midtone=1.0,
        clahe_clip_limit=0.0,
        prepare_usm_amount=0,
        color_enhance=1.0,
        adaptive_saturate_space="off",
        adaptive_vivid=False,
        scale_chroma=False,
        dither_noise=0.0,
        dither=DitherConfig(
            algorithm="noop",
            lut_name="euclidean",
            serpentine=False,
            hue_cutoff_deg=95.0,
            neutral_chroma=8.0,
        ),
    )


def _render_indices(img, cfg, orientation, canvas_w, canvas_h, threshold=0.0, **kwargs):
    return ImageRenderer(NumbaStreamingDither(_HUESSEN), _HUESSEN).render_indices(
        img, cfg, orientation, canvas_w, canvas_h, threshold, **kwargs
    )


# ── _face_centered_crop_offset unit tests ────────────────────────────────────


def test_offset_centers_on_single_face():
    """A face box centered in the scaled image yields the plain center-crop offset."""
    bbox = BoundingBox(x=0.4, y=0.4, w=0.2, h=0.2)  # centroid at (0.5, 0.5)
    x_off, y_off = _face_centered_crop_offset((bbox,), 300.0, 300.0, 100.0, 100.0)
    assert x_off == pytest.approx(100.0)  # (300-100)/2
    assert y_off == pytest.approx(100.0)


def test_offset_shifts_toward_face_near_top():
    """A face near the top of a tall source pulls the crop window up (toward
    the head), clamping at 0 — i.e. the bottom of the frame is what gets cut."""
    bbox = BoundingBox(x=0.3, y=0.05, w=0.4, h=0.1)  # centroid y = 0.1
    _, y_off = _face_centered_crop_offset((bbox,), 100.0, 300.0, 100.0, 100.0)
    assert y_off == pytest.approx(0.0)  # clamped: face_cy=30, visible_h/2=50 -> -20 -> 0


def test_offset_shifts_toward_face_near_bottom():
    bbox = BoundingBox(x=0.3, y=0.85, w=0.4, h=0.1)  # centroid y = 0.9 -> 270 in a 300 scaled_h
    _, y_off = _face_centered_crop_offset((bbox,), 100.0, 300.0, 100.0, 100.0)
    # face_cy=270, desired=270-50=220, clamp max = scaled_h - visible_h = 200
    assert y_off == pytest.approx(200.0)


def test_offset_interior_unclamped_uses_centering_formula():
    """A face above center, with room on both sides so no clamp fires, must
    land at exactly ``face_cy - visible/2`` — the actual centering formula,
    distinct from a plain center-crop."""
    # scaled_h=400, visible_h=100 -> clamp range [0, 300]. Face centroid y=0.4
    # -> face_cy=160 -> desired = 160 - 50 = 110 (interior, unclamped).
    bbox = BoundingBox(x=0.3, y=0.35, w=0.4, h=0.1)  # centroid y = 0.40
    _, y_off = _face_centered_crop_offset((bbox,), 100.0, 400.0, 100.0, 100.0)
    assert y_off == pytest.approx(110.0)
    # Guard: this must NOT equal the plain center-crop offset (400-100)/2 = 150,
    # so the test would fail if the function ignored the bbox.
    assert y_off != pytest.approx(150.0)


def test_offset_shifts_horizontally_toward_off_center_face():
    """A face left-of-center pulls the crop window left (interior, unclamped),
    exercising the x axis rather than only the symmetric case."""
    # scaled_w=400, visible_w=100 -> clamp range [0, 300]. Face centroid x=0.3
    # -> face_cx=120 -> desired = 120 - 50 = 70 (interior); center would be 150.
    bbox = BoundingBox(x=0.25, y=0.4, w=0.1, h=0.2)  # centroid x = 0.30
    x_off, _ = _face_centered_crop_offset((bbox,), 400.0, 100.0, 100.0, 100.0)
    assert x_off == pytest.approx(70.0)
    assert x_off != pytest.approx(150.0)


def test_offset_centers_on_union_not_first_face():
    """Two faces both above center: the offset must follow the union centroid,
    NOT just the first bbox and NOT the plain center-crop. This fails if the
    function looks at only one face or ignores the boxes entirely."""
    # scaled_h=400, visible_h=100. Faces span y in [0.10,0.20] and [0.30,0.40];
    # union min_y=0.10, max_y=0.40 -> union centroid = 0.25 -> face_cy=100 ->
    # desired = 100 - 50 = 50 (interior, unclamped).
    a = BoundingBox(x=0.3, y=0.10, w=0.4, h=0.10)  # first face, centroid y=0.15
    b = BoundingBox(x=0.3, y=0.30, w=0.4, h=0.10)  # second face, centroid y=0.35
    _, y_off = _face_centered_crop_offset((a, b), 100.0, 400.0, 100.0, 100.0)
    assert y_off == pytest.approx(50.0)
    # If it centered on only the first face: face_cy=60 -> 10, not 50.
    assert y_off != pytest.approx(10.0)
    # If it ignored the boxes (plain center): 150, not 50.
    assert y_off != pytest.approx(150.0)


# ── integration: face-aware crop keeps the face patch in frame ──────────────


def _patch_image(w: int, h: int, bg=(200, 100, 50), patch_box=None, patch_color=(10, 200, 10)):
    img = Image.new("RGB", (w, h), bg)
    if patch_box is not None:
        x0, y0, x1, y1 = patch_box
        for y in range(y0, y1):
            for x in range(x0, x1):
                img.putpixel((x, y), patch_color)
    return img


def test_face_aware_crop_keeps_top_face_patch_in_frame():
    """End-to-end through the real renderer: a plain center-crop of a very
    tall image drops a patch near the top, but face-aware cropping keeps it.

    The image is a uniform background with one distinct-coloured patch. The
    background dithers to a single palette index everywhere, so:
      - patch cropped out  -> the render is uniform (1 unique index),
      - patch kept in frame -> the render has >=2 unique indices.
    This asserts the patch's actual presence/absence in the rendered pixels
    without depending on which palette index the patch maps to.
    """
    w, h = 100, 300
    # Patch occupies rows [20, 40) -- a "face" near the top of a tall photo.
    # Green is far from the orange-brown background in the panel palette, so it
    # survives the noop dither as a distinct index.
    img = _patch_image(w, h, patch_box=(30, 20, 70, 40))
    cfg = _noop_cfg()
    bbox = BoundingBox(x=30 / w, y=20 / h, w=40 / w, h=20 / h)

    # threshold big enough to force use_cover for a 100x300 image on a 100x100
    # canvas (zoom_ratio = scale_cover/scale_fit - 1 = 1.0/0.333 - 1 = 2.0).
    # Plain center-crop keeps scaled rows [100, 200); the patch at [20, 40) is
    # cropped away. Face-aware crop clamps the window up to rows [0, 100),
    # keeping the patch.
    threshold = 2.0

    without_anchor = _render_indices(img, cfg, "portrait", 100, 100, threshold)
    with_anchor = _render_indices(
        img, cfg, "portrait", 100, 100, threshold, crop_anchor_bboxes_norm=(bbox,)
    )

    assert np.unique(without_anchor).size == 1, (
        "plain center-crop should have cropped the top patch away (uniform render)"
    )
    assert np.unique(with_anchor).size >= 2, (
        "face-aware crop should have kept the top patch in frame"
    )


def test_no_crop_anchor_falls_back_to_center_crop():
    """crop_anchor_bboxes_norm=None (or empty) must be pixel-identical to today's plain crop."""
    img = _patch_image(120, 90, patch_box=(50, 10, 70, 30))
    cfg = _noop_cfg()
    plain = _render_indices(img, cfg, "landscape", 133, 100, 0.5)
    with_none = _render_indices(img, cfg, "landscape", 133, 100, 0.5, crop_anchor_bboxes_norm=None)
    with_empty = _render_indices(img, cfg, "landscape", 133, 100, 0.5, crop_anchor_bboxes_norm=())
    assert (plain == with_none).all()
    assert (plain == with_empty).all()


# ── ScreenImageConfig cache_slug ─────────────────────────────────────────────


def test_face_crop_bboxes_included_in_slug_only_when_set():
    ic = _noop_cfg()
    base = ScreenImageConfig(image_config=ic, orientation=Orientation.PORTRAIT)
    with_faces = ScreenImageConfig(
        image_config=ic,
        orientation=Orientation.PORTRAIT,
        face_crop_bboxes=(BoundingBox(x=0.1, y=0.1, w=0.2, h=0.2),),
    )
    assert base.cache_slug() != with_faces.cache_slug()


def test_face_crop_bboxes_empty_matches_pre_feature_slug():
    """Default (feature off, no bboxes) slug must match a config with no
    face_crop_bboxes field at all -- upgrading must not invalidate every
    existing cached render."""
    ic = _noop_cfg()
    a = ScreenImageConfig(image_config=ic, orientation=Orientation.LANDSCAPE)
    b = ScreenImageConfig(image_config=ic, orientation=Orientation.LANDSCAPE, face_crop_bboxes=None)
    assert a.cache_slug() == b.cache_slug()


# ── AppConfig round-trip ──────────────────────────────────────────────────────


def test_app_config_face_aware_crop_defaults_false():
    assert AppConfig().classifier_face_aware_crop_enabled is False


def test_app_config_face_aware_crop_roundtrip():
    cfg = AppConfig(classifier_face_aware_crop_enabled=True)
    restored = AppConfig.from_dict(cfg.to_dict())
    assert restored.classifier_face_aware_crop_enabled is True


def test_app_config_cache_slug_changes_with_face_aware_crop():
    a = AppConfig(classifier_face_aware_crop_enabled=False)
    b = AppConfig(classifier_face_aware_crop_enabled=True)
    assert a.cache_slug() != b.cache_slug()


# ── slow visual experiment ────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEST_IMAGES_DIR = _REPO_ROOT / "images" / "test"
_BUILD_DIR = _REPO_ROOT / "build" / "test_face_aware_crop"

_PORTRAIT_FACE_IMAGES = [
    "Actress_Anna_Unterberger-2.jpg",
    "Robert_De_Niro_KVIFF_portrait.jpg",
    "Wayuu_woman_with_sad_face_in_the_market_buying.jpg",
    "string_ensemble_concert.jpeg",
]

_THRESHOLDS = [
    ("10pct", 0.10),
    ("50pct", 0.50),
    # High enough that a portrait source rendered in landscape (a near-90-degree
    # aspect mismatch) actually triggers cover-crop -- the scenario face-aware
    # cropping targets. See images/test/CREDITS.md for source attribution.
    ("120pct", 1.20),
]
_ORIENTATIONS: list[Orientation] = [Orientation.PORTRAIT, Orientation.LANDSCAPE]


@pytest.fixture(scope="module", autouse=False)
def _wipe_build():
    if _BUILD_DIR.exists():
        shutil.rmtree(_BUILD_DIR)
    _BUILD_DIR.mkdir(parents=True, exist_ok=True)


@pytest.mark.time_intensive
def test_visual_face_aware_crop_experiments(_wipe_build):
    """Render each portrait test image at increasingly aggressive crop-to-fill
    thresholds, with and without face-aware cropping, for both orientations.

    Output layout:
      build/test_face_aware_crop/<stem>_original<ext>
      build/test_face_aware_crop/<stem>__portrait_30pct_center.png
      build/test_face_aware_crop/<stem>__portrait_30pct_faceaware.png
      ...
    """
    detector = OpenCVYuNetFaceDetector()
    # Real preset (not the noop test config) so the output is actually useful
    # for judging framing/quality, not just crop-window mechanics.
    face_cfg = PRESET_IMAGE_CONFIGS["atkinson_hue_aware"]

    for name in _PORTRAIT_FACE_IMAGES:
        src = _TEST_IMAGES_DIR / name
        assert src.exists(), f"Test image missing: {src}"
        shutil.copy(src, _BUILD_DIR / f"{src.stem}_original{src.suffix}")

        bboxes = detector.detect(src)
        assert bboxes, f"Expected at least one face in {name}"

        for orientation in _ORIENTATIONS:
            for label, threshold in _THRESHOLDS:
                for variant, anchor in (("center", None), ("faceaware", tuple(bboxes))):
                    with open_image_for_render(src) as img:
                        panel_bytes = ImageRenderer(
                            NumbaStreamingDither(_HUESSEN), _HUESSEN
                        ).render_panel_bytes(
                            img,
                            face_cfg,
                            orientation,
                            threshold,
                            crop_anchor_bboxes_norm=anchor,
                        )
                    assert len(panel_bytes) == _HUESSEN.total_bytes
                    preview = preview_png_from_panel_bytes(panel_bytes, orientation, _HUESSEN)
                    out = _BUILD_DIR / f"{src.stem}__{orientation}_{label}_{variant}.png"
                    out.write_bytes(preview)
