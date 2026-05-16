"""Per-image config dispatch policy.

Wired with AppConfig at construction so ImageManager doesn't need to know
about face / B&W detection. Caches raw observations keyed by sha1 of the
original file content in <cache_dir>/image_classifier.json.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import numpy as np
from PIL import Image

from hokku_server.app_config import AppConfig
from hokku_server.bounding_box import BoundingBox
from hokku_server.face_detect_yunet_opencv import OpenCVYuNetFaceDetector
from hokku_server.image_config import ImageConfig
from hokku_server.dither_streaming import rgb_to_lab
from hokku_server.screen_image_config import ScreenImageConfig

_DB_NAME = "image_classifier.json"

GRAYSCALE_CHROMA_THRESHOLD = 8.0


def _is_near_grayscale(img) -> bool:
    """True iff a PIL Image is essentially monochrome."""
    thumb = img.copy()
    thumb.thumbnail((200, 200))
    arr = np.asarray(thumb.convert("RGB"), dtype=np.float64)
    lab = rgb_to_lab(arr)
    chroma = np.sqrt(lab[..., 1] ** 2 + lab[..., 2] ** 2)
    p95_chroma = float(np.percentile(chroma, 95))
    is_bw = p95_chroma < GRAYSCALE_CHROMA_THRESHOLD
    import sys
    status = "B&W" if is_bw else "NOT B&W"
    print(f"  [B&W check] 95th %ile chroma = {p95_chroma:.2f} (threshold {GRAYSCALE_CHROMA_THRESHOLD}): {status}", file=sys.stderr)
    return is_bw


def _check_grayscale(path: Path) -> bool:
    """True iff the image at *path* is essentially monochrome."""
    with Image.open(path) as img:
        return _is_near_grayscale(img)


@dataclass(frozen=True)
class Observations:
    """Raw per-image detection results.  None = not yet observed."""
    is_bw: bool | None = None
    face_bboxes: tuple[BoundingBox, ...] | None = None  # None = not yet detected


class ImageClassifier:
    """Decides which ImageConfig (and orientation) to use for a given image.

    The dispatch order is:
      1. B&W detection (if ``classifier_bw_detect_enabled``).
      2. Face detection (if ``classifier_face_detect_enabled``).
      3. Default.

    Raw observations (``is_bw``, ``has_face``, ``face_bbox``) are persisted in
    ``<cache_dir>/image_classifier.json`` keyed by sha1 of the original file
    so re-instantiation after restart doesn't require re-detection.

    Wiping the JSON (``clear_cache()``) forces re-detection on the next sync
    but does NOT invalidate already-rendered panel .bin files — those are
    keyed by ``ScreenImageConfig.cache_slug()``, which is deterministic from
    the effective ImageConfig + orientation + face_bbox.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._lock = threading.RLock()
        self._db_path = Path(config.cache_dir) / _DB_NAME
        self._cache: dict[str, Observations] = self._load()
        self._face_detector = None

    # ── Public API ───────────────────────────────────────────────────────────

    def screen_config_for(self, path: Path, sha1: str) -> ScreenImageConfig:
        """Return the ScreenImageConfig to render with, including CLAHE keep-out bboxes if detected."""
        cfg = self._config
        chosen, face_bboxes = self._classify(path, sha1)
        keepout = face_bboxes if cfg.classifier_face_detect_clahe_keepout else None
        return ScreenImageConfig(
            image_config=chosen,
            orientation=cfg.orientation,
            crop_to_fill_threshold=cfg.crop_to_fill_threshold,
            clahe_keepout_bboxes=keepout,
        )

    def observations_for(self, sha1: str) -> Observations:
        """Return the cached observations for *sha1*, or an all-None instance."""
        with self._lock:
            return self._cache.get(sha1, Observations())

    def clear_cache(self) -> None:
        """Wipe all cached observations (JSON deleted on disk, empty in memory)."""
        with self._lock:
            self._cache = {}
            try:
                self._db_path.unlink()
            except FileNotFoundError:
                pass

    def release_detector(self) -> None:
        """Free the face detector so the ~57 MB DNN graph is returned to the OS."""
        with self._lock:
            self._face_detector = None

    # ── Internals ────────────────────────────────────────────────────────────

    def _classify(self, path: Path, sha1: str) -> tuple[ImageConfig, tuple[BoundingBox, ...]]:
        """Return (image_config, face_bboxes) for this image."""
        cfg = self._config
        if not (cfg.classifier_bw_detect_enabled or cfg.classifier_face_detect_enabled):
            return cfg.image_config_default, ()

        with self._lock:
            obs = self._cache.get(sha1, Observations())
            dirty = False

            if cfg.classifier_bw_detect_enabled and obs.is_bw is None:
                obs = replace(obs, is_bw=_check_grayscale(path))
                dirty = True

            if cfg.classifier_face_detect_enabled and obs.face_bboxes is None:
                if self._face_detector is None:
                    self._face_detector = OpenCVYuNetFaceDetector()
                bboxes = self._face_detector.detect(path)
                obs = replace(obs, face_bboxes=tuple(bboxes))
                dirty = True

            if dirty:
                self._cache[sha1] = obs
                self._persist()

            # Dispatch order: B&W wins over face wins over default.
            if cfg.classifier_bw_detect_enabled and obs.is_bw:
                return cfg.image_config_bw, ()
            if cfg.classifier_face_detect_enabled and obs.face_bboxes:
                return cfg.image_config_face, obs.face_bboxes
            return cfg.image_config_default, ()

    def _load(self) -> dict[str, Observations]:
        try:
            data = json.loads(self._db_path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        out: dict[str, Observations] = {}
        for sha1, d in data.get("observations", {}).items():
            raw_bboxes = d.get("face_bboxes")
            if raw_bboxes is not None:
                try:
                    face_bboxes = tuple(BoundingBox(x=b['x'], y=b['y'], w=b['w'], h=b['h']) for b in raw_bboxes)
                except (ValueError, KeyError, TypeError):
                    # Invalid bbox data, treat as not yet detected
                    face_bboxes = None
            else:
                face_bboxes = None
            out[sha1] = Observations(
                is_bw=d.get("is_bw"),
                face_bboxes=face_bboxes,
            )
        return out

    def _persist(self) -> None:
        observations_dict = {}
        for sha1, o in self._cache.items():
            obs_dict = asdict(o)
            # Convert nested BoundingBox objects to dicts for JSON serialization
            if o.face_bboxes:
                obs_dict["face_bboxes"] = [asdict(b) for b in o.face_bboxes]
            observations_dict[sha1] = obs_dict

        payload = {
            "version": 1,
            "observations": observations_dict,
        }
        tmp = self._db_path.with_suffix(self._db_path.suffix + ".tmp")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=2), "utf-8")
        tmp.replace(self._db_path)
