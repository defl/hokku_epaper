"""AbstractFaceDetector + shared preprocess helpers.

The sole concrete detector is ``OpenCVYuNetFaceDetector`` in
``face_detect_yunet_opencv.py`` (cv2.FaceDetectorYN + YuNet ONNX model).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np
from PIL import Image

from hokku.webserver.bounding_box import BoundingBox
from hokku.webserver.image_renderer import open_image_for_render

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
    """Decode *path* exactly as the renderer does, shrunk so the longer edge ≤ ``max_side``.

    Returns ``(img_bgr_uint8, width, height)`` on success, where ``width`` and
    ``height`` are the resized dimensions. Returns ``None`` if the file can't
    be decoded.

    Face bboxes are fractions of this frame and the renderer applies them to
    its own decode, so both must come from ``open_image_for_render``: same
    EXIF/XMP orientation handling, same formats (HEIF/AVIF/JXL/SVG, which
    cv2.imread can't read), and the same decode budget and ``_DECODE_LOCK``,
    so detection never materialises more than a render would.
    """
    try:
        with open_image_for_render(path) as img:
            img.thumbnail((max_side, max_side))
            rgb = np.asarray(img)
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    h, w = rgb.shape[:2]
    return np.ascontiguousarray(rgb[:, :, ::-1]), w, h
