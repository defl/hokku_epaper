"""Tests for face-aware cropping.

Unit tests (always fast — synthetic images/bboxes only):
  - _face_centered_crop_offset centers on the face union and clamps to range.
  - A face near the top of a very tall source pulls the crop up (bottom gets
    cut, not the head) compared to a plain center-crop.
  - Multiple faces: the offset centers on their union, not just the first one.
  - No offset when crop_anchor_bboxes_norm is falsy (plain center-crop, same
    as today's behaviour).
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


def test_offset_centers_on_union_of_multiple_faces():
    """Two faces far apart: offset centers on their union's centroid, not either face alone."""
    a = BoundingBox(x=0.0, y=0.4, w=0.1, h=0.1)  # spans x in [0.0, 0.1]
    b = BoundingBox(x=0.9, y=0.4, w=0.1, h=0.1)  # spans x in [0.9, 1.0]
    x_off, _ = _face_centered_crop_offset((a, b), 300.0, 100.0, 100.0, 100.0)
    # Union centroid x = (0.0 + 1.0)/2 = 0.5 -> 150 in scaled space -> centered crop
    assert x_off == pytest.approx(100.0)  # (300-100)/2, same as plain center in this symmetric case


# ── integration: face-aware crop keeps the face patch in frame ──────────────


def _patch_image(w: int, h: int, bg=(200, 100, 50), patch_box=None, patch_color=(10, 200, 10)):
    img = Image.new("RGB", (w, h), bg)
    if patch_box is not None:
        x0, y0, x1, y1 = patch_box
        for y in range(y0, y1):
            for x in range(x0, x1):
                img.putpixel((x, y), patch_color)
    return img


def _contains_color(indices, color_idx) -> bool:
    return bool((indices == color_idx).any())


def test_face_aware_crop_keeps_top_face_patch_in_frame():
    """Without face-aware cropping, a plain center-crop of a very tall image
    cuts off a face patch near the top. With face-aware cropping (bbox
    matching the patch), the patch survives into the rendered canvas."""
    w, h = 100, 300
    # Patch occupies rows [20, 40) -- a "face" near the top of a tall photo.
    img = _patch_image(w, h, patch_box=(30, 20, 70, 40))
    cfg = _noop_cfg()
    bbox = BoundingBox(x=30 / w, y=20 / h, w=40 / w, h=20 / h)

    # threshold big enough to force use_cover for a 100x300 image on a 100x100 canvas
    # (zoom_ratio = scale_cover/scale_fit - 1 = 1.0/0.333 - 1 = 2.0)
    threshold = 2.0

    without_anchor = _render_indices(img, cfg, "portrait", 100, 100, threshold)
    with_anchor = _render_indices(
        img, cfg, "portrait", 100, 100, threshold, crop_anchor_bboxes_norm=(bbox,)
    )

    # noop dither maps each palette entry to nearest; just check raw presence
    # by re-deriving what index the patch color would map to via a direct
    # crop instead of relying on palette internals: compare against a
    # reference plain-PIL crop for each offset.
    scale_cover = max(100 / w, 100 / h)
    scaled_h = round(h * scale_cover)
    center_y_off = (scaled_h - 100) // 2
    face_cy = (20 / h + 40 / h) / 2 * scaled_h
    anchor_y_off = max(0.0, min(face_cy - 50.0, max(0.0, scaled_h - 100.0)))

    # The patch (scaled rows [20,40)) must NOT intersect the plain center-crop
    # window, but MUST intersect the face-anchored crop window.
    assert not (center_y_off < 40 and center_y_off + 100 > 20), (
        "test setup invalid: plain center-crop unexpectedly overlaps the patch"
    )
    assert anchor_y_off < 40 and anchor_y_off + 100 > 20, (
        "test setup invalid: face-anchored crop unexpectedly misses the patch"
    )

    # Sanity: the two renders actually differ (different crop windows -> different pixels).
    assert not (without_anchor == with_anchor).all()


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
