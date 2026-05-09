"""Face detection using OpenCV YuNet. Pure detection, no caching, no policy."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

_MODEL = Path(__file__).parent / "models" / "face_detection_yunet_2023mar.onnx"

# Maximum side length for detection input — resizing keeps the model fast and
# ensures consistent sensitivity across images of varying resolution.
_MAX_SIDE = 640

# Score threshold: 0.5 balances recall (catches real faces) against precision
# (avoids false positives on artwork / animals).
_SCORE_THRESHOLD = 0.5


def has_face(path: Path) -> bool:
    """Return True iff ≥1 face is detected in the image at *path*.

    Returns False (not raises) if the file is missing or unreadable.
    """
    img = cv2.imread(str(path))
    if img is None:
        return False
    h, w = img.shape[:2]

    # Resize so the longer side is at most _MAX_SIDE for consistent detection.
    scale = min(_MAX_SIDE / max(w, h), 1.0)
    det_w = max(1, int(w * scale))
    det_h = max(1, int(h * scale))
    if scale < 1.0:
        img = cv2.resize(img, (det_w, det_h))

    det = cv2.FaceDetectorYN_create(
        str(_MODEL), "", (det_w, det_h),
        score_threshold=_SCORE_THRESHOLD,
    )
    _, faces = det.detect(img)
    return faces is not None and len(faces) > 0
