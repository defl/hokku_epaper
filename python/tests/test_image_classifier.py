"""ImageClassifier: dispatch policy, caching, persistence, clear_cache."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from hokku.webserver.app_config import AppConfig
from hokku.webserver.image_classifier import ImageClassifier
from hokku.webserver.orientation import Orientation
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS
from hokku.webserver.screen_image_config import ScreenImageConfig

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEST_IMAGES = _REPO_ROOT / "images" / "test"

_BW_IMAGE = _TEST_IMAGES / "Forest_road_Slavne_2017_BW_G9.jpg"
_COLOUR_LANDSCAPE = _TEST_IMAGES / "Fitz_Roy_1.avif"


# ── helpers ───────────────────────────────────────────────────────────────────


def _config(
    tmp_path: Path,
    *,
    bw: bool = False,
    face: bool = False,
    **overrides,
) -> AppConfig:
    upload = tmp_path / "up"
    cache = tmp_path / "ca"
    upload.mkdir(exist_ok=True)
    cache.mkdir(exist_ok=True)
    return AppConfig(
        upload_dir=str(upload),
        cache_dir=str(cache),
        classifier_bw_detect_enabled=bw,
        classifier_face_detect_enabled=face,
        **overrides,
    )


def _sha1(path: Path) -> str:
    h = hashlib.sha1(usedforsecurity=False)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ── bw flag off ───────────────────────────────────────────────────────────────


def test_bw_flag_off_returns_default(tmp_path):
    cfg = _config(tmp_path)
    clf = ImageClassifier(cfg)
    dec = clf.decision_for(_COLOUR_LANDSCAPE, _sha1(_COLOUR_LANDSCAPE))
    assert dec.image_config == cfg.image_config_default
    # ImageClassifierDecision is orientation-free — orientation belongs to the
    # render target, not the per-image decision.
    assert not hasattr(dec, "orientation")


def test_bw_flag_off_no_json_created(tmp_path):
    cfg = _config(tmp_path)
    clf = ImageClassifier(cfg)
    clf.decision_for(_COLOUR_LANDSCAPE, _sha1(_COLOUR_LANDSCAPE))
    db_path = Path(cfg.cache_dir) / "image_classifier.json"
    assert not db_path.exists()


# ── B&W detection ─────────────────────────────────────────────────────────────


def test_bw_detect_on_bw_image_returns_bw_config(tmp_path):
    cfg = _config(tmp_path, bw=True)
    clf = ImageClassifier(cfg)
    dec = clf.decision_for(_BW_IMAGE, _sha1(_BW_IMAGE))
    assert dec.image_config == cfg.image_config_bw


def test_bw_detect_on_colour_image_returns_default(tmp_path):
    cfg = _config(tmp_path, bw=True)
    clf = ImageClassifier(cfg)
    dec = clf.decision_for(_COLOUR_LANDSCAPE, _sha1(_COLOUR_LANDSCAPE))
    assert dec.image_config == cfg.image_config_default


def test_bw_detect_persists_observation(tmp_path):
    cfg = _config(tmp_path, bw=True)
    clf = ImageClassifier(cfg)
    sha = _sha1(_BW_IMAGE)
    clf.decision_for(_BW_IMAGE, sha)
    db_path = Path(cfg.cache_dir) / "image_classifier.json"
    assert db_path.exists()
    data = json.loads(db_path.read_text())
    assert sha in data["observations"]
    assert data["observations"][sha]["is_bw"] is True


# ── cache hit: no re-detection ────────────────────────────────────────────────


def test_cache_hit_no_redetection(tmp_path):
    """A second call with the same sha1 uses the cached observation, not re-running detectors."""
    cfg = _config(tmp_path, bw=True)
    clf = ImageClassifier(cfg)

    # First call — populates cache.
    sha = _sha1(_COLOUR_LANDSCAPE)
    clf.decision_for(_COLOUR_LANDSCAPE, sha)

    # Patch detector at its source — must NOT be called on the second call.
    with patch.object(
        ImageClassifier, "_check_grayscale", side_effect=AssertionError("should not re-detect")
    ):
        dec = clf.decision_for(_COLOUR_LANDSCAPE, sha)

    assert dec.image_config == cfg.image_config_default


# ── clear_cache ───────────────────────────────────────────────────────────────


def test_clear_cache_removes_json(tmp_path):
    cfg = _config(tmp_path, bw=True)
    clf = ImageClassifier(cfg)
    clf.decision_for(_BW_IMAGE, _sha1(_BW_IMAGE))
    db = Path(cfg.cache_dir) / "image_classifier.json"
    assert db.exists()
    clf.clear_cache()
    assert not db.exists()


def test_clear_cache_forces_redetection(tmp_path):
    cfg = _config(tmp_path, bw=True)
    clf = ImageClassifier(cfg)
    sha = _sha1(_BW_IMAGE)
    clf.decision_for(_BW_IMAGE, sha)
    clf.clear_cache()
    # After clear, cache is empty.
    assert sha not in clf._cache


def test_clear_cache_idempotent_if_no_json(tmp_path):
    cfg = _config(tmp_path, bw=True)
    clf = ImageClassifier(cfg)
    # No detection was run — JSON doesn't exist yet.
    clf.clear_cache()  # should not raise


# ── persistence across re-instantiation ──────────────────────────────────────


def test_persistence_across_reinstantiation(tmp_path):
    cfg = _config(tmp_path, bw=True)
    sha = _sha1(_COLOUR_LANDSCAPE)

    clf1 = ImageClassifier(cfg)
    clf1.decision_for(_COLOUR_LANDSCAPE, sha)

    # Build a second classifier from the same config/cache_dir.
    clf2 = ImageClassifier(cfg)
    assert sha in clf2._cache
    obs = clf2._cache[sha]
    assert obs.is_bw is False


# ── ScreenImageConfig slug correctness ───────────────────────────────────────


def test_screen_config_slug_differs_by_dispatch_outcome(tmp_path):
    """The two dispatch outcomes (bw vs default) produce different ScreenImageConfig slugs."""
    cfg = _config(
        tmp_path,
        bw=True,
        image_config_default=PRESET_IMAGE_CONFIGS["atkinson_hue_aware"],
        image_config_bw=PRESET_IMAGE_CONFIGS["floyd_steinberg_bw"],
    )
    clf = ImageClassifier(cfg)

    dec_bw = clf.decision_for(_BW_IMAGE, _sha1(_BW_IMAGE))
    dec_default = clf.decision_for(_COLOUR_LANDSCAPE, _sha1(_COLOUR_LANDSCAPE))

    assert dec_bw.image_config == cfg.image_config_bw
    assert dec_default.image_config == cfg.image_config_default

    # Compose each decision with the same orientation; slugs differ.
    sc_bw = ScreenImageConfig(
        image_config=dec_bw.image_config,
        orientation=Orientation.LANDSCAPE,
        crop_to_fill_threshold=dec_bw.crop_to_fill_threshold,
        clahe_keepout_bboxes=dec_bw.clahe_keepout_bboxes,
    )
    sc_default = ScreenImageConfig(
        image_config=dec_default.image_config,
        orientation=Orientation.LANDSCAPE,
        crop_to_fill_threshold=dec_default.crop_to_fill_threshold,
        clahe_keepout_bboxes=dec_default.clahe_keepout_bboxes,
    )
    assert sc_bw.cache_slug() != sc_default.cache_slug()
