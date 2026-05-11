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

from hokku_server.app_config import AppConfig, FaceDetectorName
from hokku_server.face_detect_factory import build_face_detector
from hokku_server.image_config import ImageConfig
from hokku_server.screen_image_config import ScreenImageConfig

_DB_NAME = "image_classifier.json"

GRAYSCALE_CHROMA_THRESHOLD = 8.0


def _is_near_grayscale(img) -> bool:
    """True iff a PIL Image is essentially monochrome."""
    from hokku_server.dither_streaming import rgb_to_lab
    thumb = img.copy()
    thumb.thumbnail((200, 200))
    arr = np.asarray(thumb.convert("RGB"), dtype=np.float64)
    lab = rgb_to_lab(arr)
    chroma = np.sqrt(lab[..., 1] ** 2 + lab[..., 2] ** 2)
    return float(np.percentile(chroma, 95)) < GRAYSCALE_CHROMA_THRESHOLD


def _check_grayscale(path: Path) -> bool:
    """True iff the image at *path* is essentially monochrome."""
    from PIL import Image
    with Image.open(path) as img:
        return _is_near_grayscale(img)


@dataclass(frozen=True)
class Observations:
    """Raw per-image detection results.  None = not yet observed.

    ``face_detector`` records which backend produced ``has_face``. When the
    configured detector changes, cached ``has_face`` is treated as stale and
    re-run; ``is_bw`` is detector-independent and stays valid.
    """
    is_bw: bool | None = None
    has_face: bool | None = None
    face_detector: FaceDetectorName | None = None


class ImageClassifier:
    """Decides which ImageConfig (and orientation) to use for a given image.

    The dispatch order is:
      1. B&W detection (if ``classifier_bw_detect_enabled``).
      2. Face detection (if ``classifier_face_detect_enabled``).
      3. Default.

    Raw observations (``is_bw``, ``has_face``) are persisted in
    ``<cache_dir>/image_classifier.json`` keyed by sha1 of the original file
    so re-instantiation after restart doesn't require re-detection.

    Wiping the JSON (``clear_cache()``) forces re-detection on the next sync
    but does NOT invalidate already-rendered panel .bin files — those are
    keyed by ``ScreenImageConfig.cache_slug()``, which is deterministic from
    the effective ImageConfig + orientation.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._lock = threading.RLock()
        self._db_path = Path(config.cache_dir) / _DB_NAME
        self._cache: dict[str, Observations] = self._load()
        # Built lazily on first face-detection request so a config that
        # disables face detection doesn't pay the import cost of the
        # selected backend.
        self._face_detector = None

    # ── Public API ───────────────────────────────────────────────────────────

    def screen_config_for(self, path: Path, sha1: str) -> ScreenImageConfig:
        """Return the ScreenImageConfig (image_config + orientation + crop threshold) to render with."""
        cfg = self._config
        chosen = self._image_config_for(path, sha1)
        return ScreenImageConfig(
            image_config=chosen,
            orientation=cfg.orientation,
            crop_to_fill_threshold=cfg.crop_to_fill_threshold,
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
        """Free the face detector so the ~57 MB DNN graph is returned to the OS.

        Called by the manager after the classification phase of each sync batch.
        The detector is rebuilt lazily on the next batch that needs it.
        """
        with self._lock:
            self._face_detector = None

    # ── Internals ────────────────────────────────────────────────────────────

    def _image_config_for(self, path: Path, sha1: str) -> ImageConfig:
        cfg = self._config
        if not (cfg.classifier_bw_detect_enabled or cfg.classifier_face_detect_enabled):
            return cfg.image_config_default

        with self._lock:
            obs = self._cache.get(sha1, Observations())
            dirty = False

            # Always run both detectors when their flag is on, regardless of
            # the other result.  This means both is_bw and has_face are always
            # populated for images that go through the enabled detectors, so
            # the UI can show both observations.
            if cfg.classifier_bw_detect_enabled and obs.is_bw is None:
                obs = replace(obs, is_bw=_check_grayscale(path))
                dirty = True

            # Treat cached has_face as stale if it was produced by a
            # different detector — re-run with the currently-configured one.
            face_stale = (
                obs.has_face is None
                or obs.face_detector != cfg.face_detector
            )
            if cfg.classifier_face_detect_enabled and face_stale:
                if self._face_detector is None:
                    self._face_detector = build_face_detector(cfg)
                obs = replace(
                    obs,
                    has_face=self._face_detector.has_face(path),
                    face_detector=cfg.face_detector,
                )
                dirty = True

            if dirty:
                self._cache[sha1] = obs
                self._persist()

            # Dispatch order: B&W wins over face wins over default.
            if cfg.classifier_bw_detect_enabled and obs.is_bw:
                return cfg.image_config_bw
            if cfg.classifier_face_detect_enabled and obs.has_face:
                return cfg.image_config_face
            return cfg.image_config_default

    def _load(self) -> dict[str, Observations]:
        try:
            data = json.loads(self._db_path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        out: dict[str, Observations] = {}
        for sha1, d in data.get("observations", {}).items():
            out[sha1] = Observations(
                is_bw=d.get("is_bw"),
                has_face=d.get("has_face"),
                face_detector=d.get("face_detector"),
            )
        return out

    def _persist(self) -> None:
        payload = {
            "version": 1,
            "observations": {sha1: asdict(o) for sha1, o in self._cache.items()},
        }
        tmp = self._db_path.with_suffix(self._db_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), "utf-8")
        tmp.replace(self._db_path)
