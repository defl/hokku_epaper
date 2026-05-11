"""Build the face detector picked by ``AppConfig.face_detector``."""
from __future__ import annotations

from hokku_server.app_config import AppConfig
from hokku_server.face_detect_abstract import AbstractFaceDetector


def build_face_detector(config: AppConfig) -> AbstractFaceDetector:
    """Build the (only) supported face detector: YuNet via OpenCV DNN."""
    from hokku_server.face_detect_yunet_opencv import OpenCVYuNetFaceDetector
    return OpenCVYuNetFaceDetector()
