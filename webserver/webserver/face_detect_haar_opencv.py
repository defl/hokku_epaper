"""OpenCVHaarFaceDetector: cv2.CascadeClassifier with the bundled Haar XML.

Lightest of the three detectors (~10–30 MB resident) because Haar cascades
predate opencv's DNN backend entirely — they don't load any ONNX runtime.
The model file ships inside opencv-python at ``cv2.data.haarcascades``.

Trade: older algorithm, more false positives on textured backgrounds, can
miss profile / tilted faces. Acceptable for the e-ink wallpaper use case
where misclassification flips a preset, not a load-bearing decision.
"""
from __future__ import annotations

from pathlib import Path

import cv2

from webserver.face_detect_abstract import AbstractFaceDetector, load_image_resized


class OpenCVHaarFaceDetector(AbstractFaceDetector):
    """Frontal-face Haar cascade via cv2.CascadeClassifier."""

    def __init__(self) -> None:
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        self._cascade = cv2.CascadeClassifier(cascade_path)
        if self._cascade.empty():
            raise RuntimeError(f"failed to load Haar cascade from {cascade_path}")

    def has_face(self, path: Path) -> bool:
        loaded = load_image_resized(path)
        if loaded is None:
            return False
        img, _, _ = loaded
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        # Histogram equalisation keeps Haar stable across exposure variation.
        gray = cv2.equalizeHist(gray)
        faces = self._cascade.detectMultiScale(
            gray,
            scaleFactor=1.1,
            minNeighbors=5,
            minSize=(30, 30),
        )
        return len(faces) > 0
