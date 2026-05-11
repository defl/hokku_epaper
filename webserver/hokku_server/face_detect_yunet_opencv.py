"""OpenCVYuNetFaceDetector: cv2.FaceDetectorYN backed by the YuNet ONNX model.

Heaviest of the three detectors (~80–120 MB resident) because importing
``cv2.FaceDetectorYN_create`` triggers opencv's DNN backend, which embeds
its own ONNX runtime. Accuracy is excellent.
"""
from __future__ import annotations

from pathlib import Path

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

    def has_face(self, path: Path) -> bool:
        import cv2
        loaded = load_image_resized(path)
        if loaded is None:
            return False
        img, det_w, det_h = loaded
        det = cv2.FaceDetectorYN_create(
            str(_MODEL), "", (det_w, det_h),
            score_threshold=self._score_threshold,
        )
        _, faces = det.detect(img)
        return faces is not None and len(faces) > 0
