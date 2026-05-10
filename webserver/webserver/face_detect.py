"""Public surface of the face-detection subsystem.

Re-exports the abstract base, the three concretes, and the factory so
callers can import everything from one place.

Implementations live in:
* ``face_detect_abstract``      — AbstractFaceDetector + shared preprocess.
* ``face_detect_yunet_opencv``  — OpenCVYuNetFaceDetector.
* ``face_detect_haar_opencv``   — OpenCVHaarFaceDetector.
* ``face_detect_yunet_onnx``    — ONNXYuNetFaceDetector.
* ``face_detect_factory``       — build_face_detector(config).
"""
from webserver.face_detect_abstract import (
    AbstractFaceDetector,
    DEFAULT_MAX_SIDE,
    DEFAULT_SCORE_THRESHOLD,
    load_image_resized,
)
from webserver.face_detect_factory import build_face_detector
from webserver.face_detect_haar_opencv import OpenCVHaarFaceDetector
from webserver.face_detect_yunet_opencv import OpenCVYuNetFaceDetector
from webserver.face_detect_yunet_onnx import ONNXYuNetFaceDetector

__all__ = [
    "AbstractFaceDetector",
    "DEFAULT_MAX_SIDE",
    "DEFAULT_SCORE_THRESHOLD",
    "ONNXYuNetFaceDetector",
    "OpenCVHaarFaceDetector",
    "OpenCVYuNetFaceDetector",
    "build_face_detector",
    "load_image_resized",
]
