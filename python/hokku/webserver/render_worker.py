"""Top-level worker function submitted to the ProcessPoolExecutor.

Must be a module-level (picklable) callable.  All imports are done inside
the function body so they happen in the worker process, not in the parent
at import time — this avoids pickling large objects across the IPC boundary.

Public interface
----------------
render_one(image_path, image_config_dict, orientation, crop_to_fill_threshold,
           clahe_keepout_bboxes, face_crop_bboxes)
    → (panel_bytes: bytes, preview_bytes: bytes)

Why dicts, not dataclasses?
    The dataclasses are picklable *today*, but any future refactor that adds a
    non-picklable field (callable, lock) would silently break workers.
    Round-tripping through ``image_config_from_dict_strict`` keeps the IPC
    contract narrow and easy to audit — and validated, so a malformed config
    fails here rather than producing a quietly wrong render.
"""

from __future__ import annotations


def render_one(
    image_path: str,
    image_config_dict: dict,
    model: str,
    orientation: str,
    crop_to_fill_threshold: float = 0.0,
    clahe_keepout_bboxes: tuple[dict, ...] | None = None,
    face_crop_bboxes: tuple[dict, ...] | None = None,
) -> tuple[bytes, bytes]:
    """Render one image inside a worker process.

    Parameters
    ----------
    image_path:
        Absolute path to the source image file.
    image_config_dict:
        ``dataclasses.asdict(image_config)`` — the full ImageConfig as a plain
        dict, reconstructed via ``image_config_from_dict_strict``.
    model:
        Screen model id (e.g. ``"huessen_epf1301"``, ``"bigme_f7"``) selecting
        the ``Display`` that drives palette, geometry, and wire packing.
    orientation:
        ``"landscape"`` or ``"portrait"``.
    crop_to_fill_threshold:
        Passed through to ``render_panel_bytes``; controls when the image is
        cropped to fill the panel (0.0 = never crop, always letterbox).
    clahe_keepout_bboxes:
        BoundingBox instances as dicts (asdict result) of detected faces,
        or None.  Passed to the renderer to scope CLAHE away from the faces.
    face_crop_bboxes:
        BoundingBox instances as dicts (asdict result) of detected faces,
        or None.  When set, the cover-crop window is centered on the union of
        these faces instead of the image center ("face-aware cropping").

    Returns
    -------
    (panel_bytes, preview_bytes)
        ``panel_bytes``   — full-resolution packed panel buffer (TOTAL_BYTES long).
        ``preview_bytes`` — PNG bytes of the preview image.
    """
    # All imports are function-local: this callable runs inside a
    # ProcessPoolExecutor worker subprocess. The subprocess spawns fresh,
    # so every module must be imported here rather than relying on parent
    # process state. PIL format plugins must also be re-registered per process.
    from pathlib import Path  # noqa: PLC0415

    import pillow_avif  # noqa: F401, PLC0415 — PIL plugin registration
    import pillow_jxl  # noqa: F401, PLC0415 — PIL plugin registration
    from pillow_heif import register_heif_opener  # noqa: PLC0415

    register_heif_opener()

    from hokku.screens.registry import DISPLAY_REGISTRY  # noqa: PLC0415
    from hokku.webserver.bounding_box import BoundingBox  # noqa: PLC0415
    from hokku.webserver.dither_streaming_numba import NumbaStreamingDither  # noqa: PLC0415
    from hokku.webserver.image_abc import preview_png_from_panel_bytes  # noqa: PLC0415
    from hokku.webserver.image_config import image_config_from_dict_strict  # noqa: PLC0415
    from hokku.webserver.image_renderer import ImageRenderer, open_image_for_render  # noqa: PLC0415

    display = DISPLAY_REGISTRY[model]
    cfg = image_config_from_dict_strict(image_config_dict)
    renderer = ImageRenderer(NumbaStreamingDither(display), display)

    # Convert bbox dicts back to BoundingBox instances
    bboxes_norm = None
    if clahe_keepout_bboxes:
        try:
            bboxes_norm = tuple(
                BoundingBox(x=b["x"], y=b["y"], w=b["w"], h=b["h"]) for b in clahe_keepout_bboxes
            )
        except (KeyError, TypeError, ValueError):
            bboxes_norm = None

    crop_anchor_norm = None
    if face_crop_bboxes:
        try:
            crop_anchor_norm = tuple(
                BoundingBox(x=b["x"], y=b["y"], w=b["w"], h=b["h"]) for b in face_crop_bboxes
            )
        except (KeyError, TypeError, ValueError):
            crop_anchor_norm = None

    # NOTE: no RLIMIT_AS cap here (there used to be one). It was virtual-address-
    # space, so it never actually prevented the *physical* OOM it was meant to —
    # that is now handled properly upstream by DECODE_BUDGET_PIXELS, which refuses
    # an oversized source at ingest before any phase decodes it, so nothing that
    # reaches this point can blow the RAM budget. Worse, an RLIMIT_AS cap breaks
    # native decoders that legitimately *reserve* (not commit) large virtual
    # arenas / thread stacks — libjxl fails with an opaque "Generic Error" under
    # it (reproduced on the Pi). So the cap was both redundant and harmful.
    with open_image_for_render(Path(image_path)) as img:
        panel_bytes = renderer.render_panel_bytes(
            img,
            cfg,
            orientation,  # type: ignore[arg-type]
            crop_to_fill_threshold,
            clahe_keepout_bboxes_norm=bboxes_norm,
            crop_anchor_bboxes_norm=crop_anchor_norm,
        )
    preview_bytes = preview_png_from_panel_bytes(panel_bytes, orientation, display)  # type: ignore[arg-type]
    return panel_bytes, preview_bytes
