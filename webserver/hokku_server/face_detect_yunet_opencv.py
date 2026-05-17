"""OpenCVYuNetFaceDetector: cv2.FaceDetectorYN backed by the YuNet ONNX model.

Heaviest of the three detectors (~80–120 MB resident) because importing
``cv2.FaceDetectorYN_create`` triggers opencv's DNN backend, which embeds
its own ONNX runtime. Accuracy is excellent.
"""
from __future__ import annotations

from pathlib import Path

import cv2

from hokku_server.bounding_box import BoundingBox
from hokku_server.face_detect_abstract import (
    AbstractFaceDetector,
    DEFAULT_SCORE_THRESHOLD,
    load_image_resized,
)

_MODEL = Path(__file__).parent / "models" / "face_detection_yunet_2023mar.onnx"


class OpenCVYuNetFaceDetector(AbstractFaceDetector):
    """YuNet via opencv's DNN-backed FaceDetectorYN."""

    def __init__(self, score_threshold: float = DEFAULT_SCORE_THRESHOLD) -> None:
        self._score_threshold = score_threshold

    def detect(self, path: Path) -> list[BoundingBox]:
        loaded = load_image_resized(path)
        if loaded is None:
            return []
        img, det_w, det_h = loaded
        det = cv2.FaceDetectorYN_create(
            str(_MODEL), "", (det_w, det_h),
            score_threshold=self._score_threshold,
        )
        _, faces = det.detect(img)
        if faces is None or len(faces) == 0:
            return []
        bboxes: list[BoundingBox] = []
        for row in faces:
            x, y, w, h = (float(v) for v in row[:4])
            nx = max(0.0, min(x / det_w, 1.0))
            ny = max(0.0, min(y / det_h, 1.0))
            bbox = BoundingBox(
                x=nx,
                y=ny,
                w=max(0.0, min(w / det_w, 1.0 - nx)),
                h=max(0.0, min(h / det_h, 1.0 - ny)),
            )
            bboxes.append(bbox)
        return bboxes
