"""ONNXYuNetFaceDetector: same YuNet model as the OpenCV variant, but run
under bare ``onnxruntime`` instead of opencv's DNN wrapper.

Same accuracy as ``OpenCVYuNetFaceDetector`` (identical model weights) but
~half the resident footprint because it skips opencv's DNN backend.

The shipped ``face_detection_yunet_2023mar.onnx`` is exported with a fixed
``[1, 3, 640, 640]`` input shape, so we resize the longer edge to 640 and
zero-pad the shorter edge to keep aspect ratio. The model emits
``cls_{stride}`` and ``obj_{stride}`` probability tensors (already
post-sigmoid) for strides 8/16/32; per-anchor face score is
``sqrt(cls * obj)``. For a boolean ``has_face`` we don't need bbox decode
or NMS — the max anchor score against threshold is sufficient.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from webserver.face_detect_abstract import (
    AbstractFaceDetector,
    DEFAULT_SCORE_THRESHOLD,
    load_image_resized,
)

_MODEL = Path(__file__).parent / "models" / "face_detection_yunet_2023mar.onnx"
_INPUT_SIZE = 640  # the .onnx is exported with a fixed 640x640 input.


class ONNXYuNetFaceDetector(AbstractFaceDetector):
    """YuNet via onnxruntime (no opencv DNN backend)."""

    def __init__(self, score_threshold: float = DEFAULT_SCORE_THRESHOLD) -> None:
        # Lazy import so importing this module doesn't require onnxruntime
        # if the user never selects this detector.
        import onnxruntime as ort

        self._score_threshold = score_threshold
        # CPU provider only — onnxruntime auto-selects if other providers
        # aren't available, but pinning here keeps memory/behaviour
        # deterministic across hosts.
        self._session = ort.InferenceSession(
            str(_MODEL), providers=["CPUExecutionProvider"]
        )
        self._input_name = self._session.get_inputs()[0].name

    def has_face(self, path: Path) -> bool:
        loaded = load_image_resized(path, max_side=_INPUT_SIZE)
        if loaded is None:
            return False
        img, w, h = loaded
        # Pad to INPUT_SIZE × INPUT_SIZE with zeros so the fixed-input
        # model accepts non-square sources.
        if w != _INPUT_SIZE or h != _INPUT_SIZE:
            padded = np.zeros((_INPUT_SIZE, _INPUT_SIZE, 3), dtype=np.uint8)
            padded[:h, :w] = img
            img = padded
        # HWC uint8 BGR → NCHW float32. YuNet was trained on raw [0, 255]
        # BGR pixel values; no mean subtraction or scaling required.
        x = img.astype(np.float32).transpose(2, 0, 1)[None, :, :, :]
        outputs = self._session.run(None, {self._input_name: x})
        names = [o.name for o in self._session.get_outputs()]
        by_name = dict(zip(names, outputs))

        max_score = 0.0
        for stride in (8, 16, 32):
            cls = by_name[f"cls_{stride}"].reshape(-1)
            obj = by_name[f"obj_{stride}"].reshape(-1)
            # cls and obj are post-sigmoid probabilities; clip defensively.
            score = np.sqrt(np.clip(cls, 0.0, 1.0) * np.clip(obj, 0.0, 1.0))
            if score.size:
                m = float(score.max())
                if m > max_score:
                    max_score = m
        return max_score > self._score_threshold
