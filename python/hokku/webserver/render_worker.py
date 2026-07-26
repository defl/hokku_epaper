"""Top-level worker function submitted to the render executor.

Must be a module-level (picklable) callable.  All imports are done inside
the function body so they happen in the worker process, not in the parent
at import time — this avoids pickling large objects across the IPC boundary.

Public interface
----------------
render_image_variants(image_path, variants)
    → list[dict], one entry per variant (aligned with the input order):
      {"ok": True, "panel_bytes": bytes, "preview_bytes": bytes} on success, or
      {"ok": False, "error": str} if that single variant's render raised.
    The source is DECODED ONCE and every variant is rendered off that one
    buffer (decode-once, dither-many).  A failure to *decode* the source raises
    out of the call — it dooms every variant, so the caller fails the whole
    image once instead of per variant.

render_one(image_path, image_config_dict, model, orientation, ...)
    → (panel_bytes, preview_bytes) — thin single-variant wrapper over
      render_image_variants, kept for callers/tests that want one output.

Why dicts, not dataclasses?
    The dataclasses are picklable *today*, but any future refactor that adds a
    non-picklable field (callable, lock) would silently break workers.
    Round-tripping through ``_image_config_from_dict`` keeps the IPC contract
    narrow and easy to audit.
"""

from __future__ import annotations


def render_image_variants(image_path: str, variants: list[dict]) -> list[dict]:
    """Decode ``image_path`` once, then render every variant off that one buffer.

    Parameters
    ----------
    image_path:
        Absolute path to the source image file.
    variants:
        One dict per (model, orientation) render to produce.  Each carries:
        ``model`` (screen id), ``orientation`` ("landscape"/"portrait"),
        ``image_config`` (``dataclasses.asdict(image_config)``),
        ``crop_to_fill_threshold`` (float),
        ``clahe_keepout_bboxes`` / ``face_crop_bboxes`` (tuples of BoundingBox
        asdicts, or None).

    Returns
    -------
    list[dict]
        Aligned with ``variants``.  ``{"ok": True, "panel_bytes", "preview_bytes"}``
        on success; ``{"ok": False, "error": str}`` if that one variant's render
        raised (the others still render).  A *decode* failure raises instead.

    The decoded source is the model/orientation-independent, EXIF-corrected,
    bbox-shrunk RGB image (see ``open_image_for_render``); the same buffer is
    valid for every variant, so it is decoded exactly once regardless of how many
    screens × orientations are rendered.  ``release_input=False`` keeps it alive
    across the loop; the ``with`` closes it once at the end.
    """
    # All imports are function-local: this callable may run inside a worker
    # subprocess. The subprocess spawns fresh, so every module must be imported
    # here rather than relying on parent process state. PIL format plugins must
    # also be re-registered per process.
    from pathlib import Path  # noqa: PLC0415

    import pillow_avif  # noqa: F401, PLC0415 — PIL plugin registration
    import pillow_jxl  # noqa: F401, PLC0415 — PIL plugin registration
    from pillow_heif import register_heif_opener  # noqa: PLC0415

    register_heif_opener()

    from hokku.screens.registry import DISPLAY_REGISTRY  # noqa: PLC0415
    from hokku.webserver.bounding_box import BoundingBox  # noqa: PLC0415
    from hokku.webserver.dither_streaming_numba import NumbaStreamingDither  # noqa: PLC0415
    from hokku.webserver.image_abc import preview_png_from_panel_bytes  # noqa: PLC0415
    from hokku.webserver.image_config import _image_config_from_dict  # noqa: PLC0415
    from hokku.webserver.image_renderer import ImageRenderer, open_image_for_render  # noqa: PLC0415
    from hokku.webserver.orientation import Orientation  # noqa: PLC0415

    def _to_bboxes(raw: tuple[dict, ...] | None) -> tuple[BoundingBox, ...] | None:
        if not raw:
            return None
        try:
            return tuple(BoundingBox(x=b["x"], y=b["y"], w=b["w"], h=b["h"]) for b in raw)
        except (KeyError, TypeError, ValueError):
            return None

    results: list[dict] = []
    # NOTE: no RLIMIT_AS cap here (there used to be one). It was virtual-address-
    # space, so it never actually prevented the *physical* OOM it was meant to —
    # that is now handled properly upstream by DECODE_BUDGET_PIXELS, which refuses
    # an oversized source at ingest before any phase decodes it. Worse, an
    # RLIMIT_AS cap breaks native decoders that legitimately *reserve* (not commit)
    # large virtual arenas / thread stacks — libjxl fails with an opaque "Generic
    # Error" under it. So the cap was both redundant and harmful.
    #
    # Decode ONCE. A decode failure dooms every variant — let it propagate so the
    # caller fails the whole image once.
    with open_image_for_render(Path(image_path)) as img:
        for variant in variants:
            try:
                display = DISPLAY_REGISTRY[variant["model"]]
                orientation = Orientation(variant["orientation"])
                cfg = _image_config_from_dict(variant["image_config"])
                renderer = ImageRenderer(NumbaStreamingDither(display), display)
                panel_bytes = renderer.render_panel_bytes(
                    img,
                    cfg,
                    orientation,  # type: ignore[arg-type]
                    variant.get("crop_to_fill_threshold", 0.0),
                    clahe_keepout_bboxes_norm=_to_bboxes(variant.get("clahe_keepout_bboxes")),
                    crop_anchor_bboxes_norm=_to_bboxes(variant.get("face_crop_bboxes")),
                    # Keep the shared source alive for the remaining variants; the
                    # enclosing `with` closes it once, after the whole batch.
                    release_input=False,
                )
                preview_bytes = preview_png_from_panel_bytes(panel_bytes, orientation, display)  # type: ignore[arg-type]
                results.append(
                    {"ok": True, "panel_bytes": panel_bytes, "preview_bytes": preview_bytes}
                )
            except Exception as exc:  # isolate one variant's failure
                # A single variant's render failed (bad config, palette bug, ...).
                # Record it and keep rendering the others; the manager fails only
                # this (model, orientation), not the whole image.
                results.append({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return results


def render_one(
    image_path: str,
    image_config_dict: dict,
    model: str,
    orientation: str,
    crop_to_fill_threshold: float = 0.0,
    clahe_keepout_bboxes: tuple[dict, ...] | None = None,
    face_crop_bboxes: tuple[dict, ...] | None = None,
) -> tuple[bytes, bytes]:
    """Render a single (model, orientation) variant.

    Thin wrapper over :func:`render_image_variants` for callers/tests that want
    one output.  Raises on decode failure (propagated) or on that variant's
    render failure.

    Returns ``(panel_bytes, preview_bytes)`` — the full-resolution packed panel
    buffer (``TOTAL_BYTES`` long) and PNG preview bytes.
    """
    variant = {
        "model": model,
        "orientation": orientation,
        "image_config": image_config_dict,
        "crop_to_fill_threshold": crop_to_fill_threshold,
        "clahe_keepout_bboxes": clahe_keepout_bboxes,
        "face_crop_bboxes": face_crop_bboxes,
    }
    result = render_image_variants(image_path, [variant])[0]
    if not result["ok"]:
        raise RuntimeError(result["error"])
    return result["panel_bytes"], result["preview_bytes"]
