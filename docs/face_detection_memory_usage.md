# Face-detection memory budget

> **Reference commit:** to be filled in after the introducing commit lands. Re-measure with
> `pytest webserver/tests/test_face_detect_memory.py -m time_intensive -s` if numbers below diverge from observed behaviour.
> Numbers are from the dev box (Windows / x86, Python 3.13, opencv-contrib-python 4.x, onnxruntime 1.26).
> Pi-class hosts will see different absolute numbers — opencv-python-headless is similar across architectures, but onnxruntime arm64 wheels differ.

This document captures the per-detector memory profile for the three
concrete `AbstractFaceDetector` implementations shipped with hokku-server,
how it was measured, and which detector to pick for which host.

---

## Headline numbers

| Detector | RSS after init (model loaded) | Peak during `has_face` | Transient inference Δ |
|---|---:|---:|---:|
| `yunet_opencv` | ~57 MB | **~63–68 MB** | ~6–11 MB |
| `haar_opencv`  | ~58 MB | **~158–167 MB** ⚠ | ~110 MB |
| `yunet_onnx`   | ~84 MB | **~100–101 MB** | ~17 MB |

`yunet_opencv` is the lightest both at startup *and* during inference on
this dev box. The intuition that "Haar must be lighter than DNN" is
wrong — `cv2.CascadeClassifier.detectMultiScale` allocates a full
multi-scale image pyramid in C-side buffers, and the peak RSS during
that call dwarfs anything either YuNet variant does.

`yunet_onnx` is the heaviest at startup (onnxruntime's CPU execution
provider is bigger than opencv's DNN runtime), but its inference
transient is bounded by the static `[1, 3, 640, 640]` input tensor
(~5 MB) plus a handful of small output tensors. End-to-end it's still
larger than `yunet_opencv` here because the bulk of the cost is the
static onnxruntime baseline.

---

## Which detector to pick

### Default: `yunet_opencv`

Best accuracy and lowest memory on this dev box. Existing users get the
same behaviour they had before the abstract refactor.

### Pi Zero 2 W (512 MB total, ~200 MB free at startup): `yunet_opencv`

Still the right answer — its transient is ~6–11 MB, which fits
comfortably inside the available headroom. **Don't pick `haar_opencv`
here**: its 110 MB transient will reliably tip the system into OOM the
first time the classifier evaluates a portrait, which is exactly the
failure mode this work was triggered by. **Don't pick `yunet_onnx`
here either**: the +27 MB always-resident overhead from onnxruntime
isn't worth it when `yunet_opencv` already fits.

### Quality-driven desktop / mid-tier server: `yunet_opencv` or `yunet_onnx`

Identical accuracy (same model file). Pick `yunet_onnx` if you want to
drop opencv's DNN backend entirely from your dependency graph (e.g.
you're already pulling in onnxruntime for other models and don't want
two ONNX runtimes). Otherwise stay on `yunet_opencv`.

### When `haar_opencv` makes sense: rarely

The only reason to pick it is if `onnxruntime` and opencv's DNN backend
are both unavailable on the target — which is unusual since
`opencv-python` ships the DNN backend by default and the deb postinst
installs it. If you're stuck without a DNN backend, Haar still works
end-to-end; just budget for ~170 MB peak inference RSS.

---

## Where the memory goes

### `yunet_opencv` (~57 MB init, ~67 MB peak)

* `import cv2` itself loads opencv-contrib-python's shared libraries
  and the bundled DNN backend (~30 MB resident).
* `cv2.FaceDetectorYN_create(model_path, "", input_size, ...)` parses
  the ONNX model and constructs an internal opencv-DNN graph (~25 MB
  resident).
* `det.detect(img)` runs forward pass; transient ~6–11 MB for a
  640×640 input.

### `haar_opencv` (~58 MB init, ~167 MB peak)

* `import cv2` plus python baseline ≈ 58 MB resident.
* `cv2.CascadeClassifier(...)` parses the XML in C; tiny.
* `detectMultiScale(gray, scaleFactor=1.1, ...)` builds an internal
  Gaussian-pyramid plus per-scale integral images. For a 640px-long
  input that's ~110 MB transient. RSS climbs in tight bursts — the
  C library realloc'd buffers don't return to the OS even after the
  call returns (visible because `rss_after_has_face` stays at the
  inference peak).

### `yunet_onnx` (~84 MB init, ~101 MB peak)

* `import cv2` (used by the shared `load_image_resized` preprocess)
  plus `import onnxruntime` plus the ORT CPU execution provider's
  arenas ≈ 84 MB resident.
* `InferenceSession.run(...)` allocates the input tensor (5 MB),
  intermediate activation buffers, and 12 output tensors (cls/obj/
  bbox/kps × 3 strides). Transient ~17 MB.
* The ORT memory arena is sticky — buffers allocated for the first
  `run` are reused on subsequent runs, so steady-state per-image
  inference cost is roughly flat.

---

## Per-image table (full matrix)

| Detector | Image | RSS after init | Peak during inference | Detected |
|---|---|---:|---:|:---:|
| yunet_opencv | Actress_Anna_Unterberger-2.jpg                    | 56.6 MB | 67.7 MB | True |
| yunet_opencv | Robert_De_Niro_KVIFF_portrait.jpg                  | 56.5 MB | 63.0 MB | True |
| yunet_opencv | Wayuu_woman_with_sad_face_in_the_market_buying.jpg | 56.8 MB | 63.4 MB | True |
| yunet_opencv | Forest_road_Slavne_2017_BW_G9.jpg                  | 56.4 MB | 67.7 MB | False |
| yunet_opencv | Albi_Panorama_Sunset_Panini_General.jpg            | 56.7 MB | 64.2 MB | False |
| haar_opencv  | Actress_Anna_Unterberger-2.jpg                    | 58.2 MB | 166.6 MB | True |
| haar_opencv  | Robert_De_Niro_KVIFF_portrait.jpg                  | 57.9 MB | 161.8 MB | True |
| haar_opencv  | Wayuu_woman_with_sad_face_in_the_market_buying.jpg | 58.2 MB | 167.6 MB | True |
| haar_opencv  | Forest_road_Slavne_2017_BW_G9.jpg                  | 58.1 MB | 167.0 MB | False |
| haar_opencv  | Albi_Panorama_Sunset_Panini_General.jpg            | 58.4 MB | 158.0 MB | False |
| yunet_onnx   | Actress_Anna_Unterberger-2.jpg                    | 83.8 MB | 100.7 MB | True |
| yunet_onnx   | Robert_De_Niro_KVIFF_portrait.jpg                  | 83.8 MB | 100.8 MB | True |
| yunet_onnx   | Wayuu_woman_with_sad_face_in_the_market_buying.jpg | 83.9 MB | 100.9 MB | True |
| yunet_onnx   | Forest_road_Slavne_2017_BW_G9.jpg                  | 83.9 MB | 100.8 MB | False |
| yunet_onnx   | Albi_Panorama_Sunset_Panini_General.jpg            | 83.6 MB | 100.9 MB | False |

All detectors agree on the contract test pile (3 portraits → True, 11
non-portraits → False) without any allowlist. See
`webserver/tests/test_face_detect.py` for the full agreement assertions.

---

## How to measure

Layer-C subprocess + psutil RSS sampling (the same pattern used in
`docs/image_processing_memory_usage.md`):

* Spawn a fresh `python -c` subprocess. The child imports `psutil`,
  records RSS, then imports + instantiates the requested detector and
  records RSS again. It signals `READY <rss_python_only> <rss_after_init>`
  on stdout and waits for a one-byte go signal.
* Parent reads `READY`, then attaches `psutil.Process(child.pid)` and
  polls `memory_info().rss` at 5 ms intervals.
* Parent writes the go byte. Child runs `has_face(image)` once and
  records final RSS, then writes `OK <detected> <rss_after_has_face>`
  and exits.
* Parent reports four RSS checkpoints plus the polled peak.

Helper: `webserver/tests/_face_detect_memory_helpers.py` (function
`peak_rss_subprocess_face_detect`).

The Windows-specific quirk encountered while building this:
`psutil.Process(child.pid).memory_info().rss` initially returns a stale
value before the child has touched its memory. We work around it by
having the child report its own RSS at known checkpoints inside the
driver script, and only treating the parent-side polling as a max
upper-bound. The headline numbers come from the child's self-reported
checkpoints.

---

## Why the headline picks `yunet_opencv` even though we needed less RAM

The original motivation for this refactor was a Pi OOM during a 6-image
sync batch. The hypothesis was that opencv's DNN backend was the heavy
hitter and a Haar / ONNX-direct alternative would save tens of MB.

The measurement disagrees:

* `haar_opencv` is the *worst* of the three during inference, by ~100 MB.
* `yunet_onnx` is heavier than `yunet_opencv` at startup (+27 MB) and
  also heavier during inference (+33 MB peak), even though it skips
  opencv's DNN backend — the onnxruntime CPU provider is itself
  larger than that backend.
* `yunet_opencv` peaks at ~67 MB during inference, ~57 MB resident
  between calls.

So the right answer for the Pi-OOM case isn't "switch face detector";
it's "look elsewhere in the per-render budget" (the 50 MB dither
stripe, opencv-headless's static cost, or something else entirely).
The pluggable architecture is still useful — it makes future detector
additions cheap and lets ops swap out `yunet_opencv` if a specific
host needs a different trade-off — but on the dev box none of the
alternatives shipped here are actually lighter than the original.

---

## Reference: file map

```
webserver/
  webserver/
    face_detect.py                 ← public re-export shim
    face_detect_abstract.py        ← AbstractFaceDetector + load_image_resized
    face_detect_yunet_opencv.py    ← OpenCVYuNetFaceDetector (cv2.FaceDetectorYN)
    face_detect_haar_opencv.py     ← OpenCVHaarFaceDetector (cv2.CascadeClassifier)
    face_detect_yunet_onnx.py      ← ONNXYuNetFaceDetector (onnxruntime)
    face_detect_factory.py         ← build_face_detector(config)
    models/face_detection_yunet_2023mar.onnx
  tests/
    test_face_detect.py            ← parametrised contract tests (33 cases)
    test_face_detect_memory.py     ← Layer-C peak-RSS assertions
    _face_detect_memory_helpers.py ← peak_rss_subprocess_face_detect

docs/
  face_detection_memory_usage.md   ← this document
```
