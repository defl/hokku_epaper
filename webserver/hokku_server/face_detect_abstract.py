"""AbstractFaceDetector + shared preprocess helpers.

The sole concrete detector is ``OpenCVYuNetFaceDetector`` in
``face_detect_yunet_opencv.py`` (cv2.FaceDetectorYN + YuNet ONNX model).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import cv2
import numpy as np

from hokku_server.bounding_box import BoundingBox

# Maximum side length for detection input — resizing keeps detection fast and
# ensures consistent sensitivity across images of varying resolution.
DEFAULT_MAX_SIDE = 640

# Score threshold: 0.5 balances recall (catches real faces) against precision
# (avoids false positives on artwork / animals).
DEFAULT_SCORE_THRESHOLD = 0.5


class AbstractFaceDetector(ABC):
    """Detect whether a file contains a face and, if so, where."""

    def has_face(self, path: Path) -> bool:
        """Return True iff ≥1 face is detected.  Convenience wrapper around detect()."""
        return bool(self.detect(path))

    @abstractmethod
    def detect(self, path: Path) -> list[BoundingBox]:
        """Return bboxes for all detected faces.

        Each bbox is (x, y, w, h) expressed as fractions of the image dimensions
        so it is resolution-independent and survives the resizing that happens
        before rendering. Returns an empty list on missing/unreadable files or
        detection errors.
        """


def load_image_resized(
    path: Path, max_side: int = DEFAULT_MAX_SIDE
) -> tuple[np.ndarray, int, int] | None:
    """Read *path* via cv2.imread and resize so the longer edge ≤ ``max_side``.

    Returns ``(img_bgr_uint8, width, height)`` on success, where ``width`` and
    ``height`` are the resized dimensions. Returns ``None`` if the file can't
    be read.

    Concrete detectors share this preprocess so memory measurements compare
    the detection backend itself, not differences in how each variant
    reads the source file.
    """
    img = cv2.imread(str(path))
    if img is None:
        return None
    h, w = img.shape[:2]
    scale = min(max_side / max(w, h), 1.0)
    new_w = max(1, int(w * scale))
    new_h = max(1, int(h * scale))
    if scale < 1.0:
        img = cv2.resize(img, (new_w, new_h))
    return img, new_w, new_h
