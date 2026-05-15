"""Top-level worker function submitted to the ProcessPoolExecutor.

Must be a module-level (picklable) callable.  All imports are done inside
the function body so they happen in the worker process, not in the parent
at import time — this avoids pickling large objects across the IPC boundary.

Public interface
----------------
render_one(image_path, image_config_dict, orientation, crop_to_fill_threshold)
    → (panel_bytes: bytes, preview_bytes: bytes)

Why dicts, not dataclasses?
    The dataclasses are picklable *today*, but any future refactor that adds a
    non-picklable field (callable, lock) would silently break workers.
    Round-tripping through ``_image_config_from_dict`` keeps the IPC contract
    narrow and easy to audit.
"""
from __future__ import annotations


def render_one(
    image_path: str,
    image_config_dict: dict,
    orientation: str,
    crop_to_fill_threshold: float = 0.0,
) -> tuple[bytes, bytes]:
    """Render one image inside a worker process.

    Parameters
    ----------
    image_path:
        Absolute path to the source image file.
    image_config_dict:
        ``dataclasses.asdict(image_config)`` — the full ImageConfig as a plain
        dict, ready to be reconstructed via ``_image_config_from_dict``.
    orientation:
        ``"landscape"`` or ``"portrait"``.
    crop_to_fill_threshold:
        Passed through to ``render_panel_bytes``; controls when the image is
        cropped to fill the panel (0.0 = never crop, always letterbox).

    Returns
    -------
    (panel_bytes, preview_bytes)
        ``panel_bytes``   — full-resolution packed panel buffer (TOTAL_BYTES long).
        ``preview_bytes`` — PNG bytes of the preview image.
    """
    # Register format plugins in this worker process (idempotent; PIL ignores
    # duplicate registrations so it's safe to call on every task).
    import pillow_avif  # noqa: F401
    import pillow_jxl  # noqa: F401
    from pillow_heif import register_heif_opener
    register_heif_opener()

    from pathlib import Path
    from hokku_server.dither_streaming_numba import NumbaStreamingDither
    from hokku_server.image_abc import preview_png_from_panel_bytes
    from hokku_server.image_renderer import ImageRenderer, open_image_for_render
    from hokku_server.image_config import _image_config_from_dict

    cfg = _image_config_from_dict(image_config_dict)
    renderer = ImageRenderer(NumbaStreamingDither())
    with open_image_for_render(Path(image_path)) as img:
        panel_bytes = renderer.render_panel_bytes(
            img, cfg, orientation,  # type: ignore[arg-type]
            crop_to_fill_threshold,
        )
    preview_bytes = preview_png_from_panel_bytes(panel_bytes, orientation)  # type: ignore[arg-type]
    return panel_bytes, preview_bytes
