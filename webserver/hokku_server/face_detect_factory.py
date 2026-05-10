"""Build the face detector picked by ``AppConfig.face_detector``."""
from __future__ import annotations

from hokku_server.app_config import AppConfig
from hokku_server.face_detect_abstract import AbstractFaceDetector


def build_face_detector(config: AppConfig) -> AbstractFaceDetector:
    """Map ``config.face_detector`` to a concrete ``AbstractFaceDetector``.

    Imports each concrete lazily so unselected detectors don't drag their
    dependencies into the running process (notably: importing
    ``face_detect_yunet_onnx`` triggers the onnxruntime import chain).
    """
    name = config.face_detector
    if name == "yunet_opencv":
        from hokku_server.face_detect_yunet_opencv import OpenCVYuNetFaceDetector
        return OpenCVYuNetFaceDetector()
    if name == "haar_opencv":
        from hokku_server.face_detect_haar_opencv import OpenCVHaarFaceDetector
        return OpenCVHaarFaceDetector()
    if name == "yunet_onnx":
        from hokku_server.face_detect_yunet_onnx import ONNXYuNetFaceDetector
        return ONNXYuNetFaceDetector()
    raise ValueError(f"Unknown face_detector: {name!r}")
