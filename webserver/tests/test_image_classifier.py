"""ImageClassifier: dispatch policy, caching, persistence, clear_cache."""
from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hokku_server.app_config import AppConfig
from hokku_server.image_classifier import ImageClassifier, Observations
from hokku_server.image_config import ImageConfig
from hokku_server.presets import PRESET_IMAGE_CONFIGS
from hokku_server.screen_image_config import ScreenImageConfig

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEST_IMAGES = _REPO_ROOT / "images" / "test"

_PORTRAIT = _TEST_IMAGES / "Robert_De_Niro_KVIFF_portrait.jpg"
_BW_IMAGE = _TEST_IMAGES / "Forest_road_Slavne_2017_BW_G9.jpg"
_COLOUR_LANDSCAPE = _TEST_IMAGES / "Fitz_Roy_1.jpg"


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
    import hashlib
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ── both flags off ────────────────────────────────────────────────────────────

def test_both_flags_off_returns_default(tmp_path):
    cfg = _config(tmp_path)
    clf = ImageClassifier(cfg)
    sc = clf.screen_config_for(_COLOUR_LANDSCAPE, _sha1(_COLOUR_LANDSCAPE))
    assert sc.image_config == cfg.image_config_default
    assert sc.orientation == cfg.orientation


def test_both_flags_off_no_json_created(tmp_path):
    cfg = _config(tmp_path)
    clf = ImageClassifier(cfg)
    clf.screen_config_for(_COLOUR_LANDSCAPE, _sha1(_COLOUR_LANDSCAPE))
    db_path = Path(cfg.cache_dir) / "image_classifier.json"
    assert not db_path.exists()


# ── B&W detection ─────────────────────────────────────────────────────────────

def test_bw_detect_on_bw_image_returns_bw_config(tmp_path):
    cfg = _config(tmp_path, bw=True)
    clf = ImageClassifier(cfg)
    sc = clf.screen_config_for(_BW_IMAGE, _sha1(_BW_IMAGE))
    assert sc.image_config == cfg.image_config_bw


def test_bw_detect_on_colour_image_returns_default(tmp_path):
    cfg = _config(tmp_path, bw=True)
    clf = ImageClassifier(cfg)
    sc = clf.screen_config_for(_COLOUR_LANDSCAPE, _sha1(_COLOUR_LANDSCAPE))
    assert sc.image_config == cfg.image_config_default


def test_bw_detect_persists_observation(tmp_path):
    cfg = _config(tmp_path, bw=True)
    clf = ImageClassifier(cfg)
    sha = _sha1(_BW_IMAGE)
    clf.screen_config_for(_BW_IMAGE, sha)
    db_path = Path(cfg.cache_dir) / "image_classifier.json"
    assert db_path.exists()
    data = json.loads(db_path.read_text())
    assert sha in data["observations"]
    assert data["observations"][sha]["is_bw"] is True
    assert data["observations"][sha]["has_face"] is None


# ── face detection ────────────────────────────────────────────────────────────

def test_face_detect_on_portrait_returns_face_config(tmp_path):
    cfg = _config(tmp_path, face=True)
    clf = ImageClassifier(cfg)
    sc = clf.screen_config_for(_PORTRAIT, _sha1(_PORTRAIT))
    assert sc.image_config == cfg.image_config_face


def test_face_detect_on_landscape_returns_default(tmp_path):
    cfg = _config(tmp_path, face=True)
    clf = ImageClassifier(cfg)
    sc = clf.screen_config_for(_COLOUR_LANDSCAPE, _sha1(_COLOUR_LANDSCAPE))
    assert sc.image_config == cfg.image_config_default
    # has_face cached as False.
    sha = _sha1(_COLOUR_LANDSCAPE)
    db = json.loads((Path(cfg.cache_dir) / "image_classifier.json").read_text())
    assert db["observations"][sha]["has_face"] is False


def test_face_detect_persists_observation(tmp_path):
    cfg = _config(tmp_path, face=True)
    clf = ImageClassifier(cfg)
    sha = _sha1(_PORTRAIT)
    clf.screen_config_for(_PORTRAIT, sha)
    db = json.loads((Path(cfg.cache_dir) / "image_classifier.json").read_text())
    assert db["observations"][sha]["has_face"] is True


# ── dispatch priority: B&W beats face ────────────────────────────────────────

def test_bw_beats_face_for_bw_portrait(tmp_path):
    """If an image is B&W, it gets image_config_bw even when a face is also detected."""
    cfg = _config(tmp_path, bw=True, face=True)
    clf = ImageClassifier(cfg)

    # Pre-seed both observations so neither detector runs — the test is
    # purely about dispatch priority: B&W must win over face.
    sha = _sha1(_BW_IMAGE)
    with clf._lock:
        clf._cache[sha] = Observations(
            is_bw=True, has_face=True, face_detector=cfg.face_detector,
        )

    sc = clf.screen_config_for(_BW_IMAGE, sha)
    assert sc.image_config == cfg.image_config_bw


def test_both_flags_on_colour_portrait_returns_face(tmp_path):
    cfg = _config(tmp_path, bw=True, face=True)
    clf = ImageClassifier(cfg)
    sc = clf.screen_config_for(_PORTRAIT, _sha1(_PORTRAIT))
    # Portrait is not B&W → falls through to face detection → face found.
    assert sc.image_config == cfg.image_config_face


def test_both_flags_on_colour_landscape_returns_default(tmp_path):
    cfg = _config(tmp_path, bw=True, face=True)
    clf = ImageClassifier(cfg)
    sc = clf.screen_config_for(_COLOUR_LANDSCAPE, _sha1(_COLOUR_LANDSCAPE))
    assert sc.image_config == cfg.image_config_default


# ── cache hit: no re-detection ────────────────────────────────────────────────

def test_cache_hit_no_redetection(tmp_path):
    """A second call with the same sha1 uses the cached observation, not re-running detectors."""
    cfg = _config(tmp_path, bw=True, face=True)
    clf = ImageClassifier(cfg)

    # First call — populates cache.
    sha = _sha1(_COLOUR_LANDSCAPE)
    clf.screen_config_for(_COLOUR_LANDSCAPE, sha)

    # Patch detectors at their source — they must NOT be called on the second call.
    spy_detector = MagicMock(has_face=MagicMock(side_effect=AssertionError("should not re-detect")))
    with patch("hokku_server.image.is_grayscale", side_effect=AssertionError("should not re-detect")):
        with patch.object(clf, "_face_detector", spy_detector):
            sc = clf.screen_config_for(_COLOUR_LANDSCAPE, sha)

    assert sc.image_config == cfg.image_config_default


# ── clear_cache ───────────────────────────────────────────────────────────────

def test_clear_cache_removes_json(tmp_path):
    cfg = _config(tmp_path, bw=True)
    clf = ImageClassifier(cfg)
    clf.screen_config_for(_BW_IMAGE, _sha1(_BW_IMAGE))
    db = Path(cfg.cache_dir) / "image_classifier.json"
    assert db.exists()
    clf.clear_cache()
    assert not db.exists()


def test_clear_cache_forces_redetection(tmp_path):
    cfg = _config(tmp_path, bw=True)
    clf = ImageClassifier(cfg)
    sha = _sha1(_BW_IMAGE)
    clf.screen_config_for(_BW_IMAGE, sha)
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
    cfg = _config(tmp_path, bw=True, face=True)
    sha = _sha1(_COLOUR_LANDSCAPE)

    clf1 = ImageClassifier(cfg)
    clf1.screen_config_for(_COLOUR_LANDSCAPE, sha)

    # Build a second classifier from the same config/cache_dir.
    clf2 = ImageClassifier(cfg)
    assert sha in clf2._cache
    obs = clf2._cache[sha]
    assert obs.is_bw is False
    assert obs.has_face is False


# ── ScreenImageConfig slug correctness ───────────────────────────────────────

def test_screen_config_slug_differs_by_dispatch_outcome(tmp_path):
    """The three dispatch outcomes produce different ScreenImageConfig slugs."""
    # Use explicitly distinct image configs for all three slots.
    cfg = _config(
        tmp_path,
        bw=True,
        face=True,
        image_config_default=PRESET_IMAGE_CONFIGS["atkinson_hue_aware"],
        image_config_bw=PRESET_IMAGE_CONFIGS["floyd_steinberg"],
        image_config_face=PRESET_IMAGE_CONFIGS["stucki_hue_aware"],
    )
    clf = ImageClassifier(cfg)

    sc_bw = clf.screen_config_for(_BW_IMAGE, _sha1(_BW_IMAGE))
    sc_portrait = clf.screen_config_for(_PORTRAIT, _sha1(_PORTRAIT))
    sc_default = clf.screen_config_for(_COLOUR_LANDSCAPE, _sha1(_COLOUR_LANDSCAPE))

    assert sc_bw.image_config == cfg.image_config_bw
    assert sc_portrait.image_config == cfg.image_config_face
    assert sc_default.image_config == cfg.image_config_default

    # All three slugs are different.
    slugs = {sc_bw.cache_slug(), sc_portrait.cache_slug(), sc_default.cache_slug()}
    assert len(slugs) == 3


def test_screen_config_orientation_matches_app_config(tmp_path):
    cfg = _config(tmp_path, bw=False, face=False, orientation="portrait")
    clf = ImageClassifier(cfg)
    sc = clf.screen_config_for(_COLOUR_LANDSCAPE, _sha1(_COLOUR_LANDSCAPE))
    assert sc.orientation == "portrait"
