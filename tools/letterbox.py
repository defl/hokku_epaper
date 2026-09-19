#!/usr/bin/env python3
"""The letterbox leak: white bars that change how the photograph is processed.

``AbstractImageRenderer._prepare_canvas`` pastes the fitted photo onto a white
canvas and only then runs ``_apply_prepare_enhancements`` over the whole thing
(``image_abc.py:338-361``). Two of those stages read image-wide statistics:
``ImageOps.autocontrast`` clips a histogram percentile, and PIL's
``ImageEnhance.Contrast`` pivots on the mean. With 40-50 % of a portrait canvas at
255, the top percentile is all bar and the mean is pulled up, so the photo's own
highlights are never stretched and its contrast pivots in the wrong place.
Measured on twenty letterboxed library photos: 1.5-7.6 L* darker on average and
up to 12 L* lower in the highlights than the same photo processed on its own.

This module tests the fix before it touches shipped code: `PhotoFirstRenderer`
enhances the fitted photo *alone* and leaves the bars as they were. Geometry is
not reimplemented — production's own ``_prepare_canvas`` lays out the canvas with
the enhancement stage switched off, and production's own enhancement function is
then applied to the picture rectangle. For a full-frame photo there is no padding
and the result is exactly production's.

It also exposes the picture rectangle itself, which measurement needs: metrics
averaged over the whole canvas are ~45 % bar for a portrait photo, and a judging
crop that includes the bars frames the picture in whatever the rig lit them as.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator
from pathlib import Path

import numpy as np
from PIL import Image

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

import hokku.webserver.image_abc as image_abc
from hokku.webserver.dither_streaming_numba import NumbaStreamingDither
from hokku.webserver.image_config import ImageConfig
from hokku.webserver.image_renderer import ImageRenderer, open_image_for_render
from hokku.webserver.orientation import Orientation
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS

_ENHANCE = image_abc._apply_prepare_enhancements

# Canvas colour of the letterbox, by the ink it is forced to after dithering.
BAR_RGB: dict[int, tuple[int, int, int]] = {0: (0, 0, 0), 1: (255, 255, 255)}


@contextlib.contextmanager
def _geometry_only() -> Iterator[None]:
    """Run ``_prepare_canvas`` with its enhancement stage as the identity.

    ``_prepare_canvas`` looks the function up as a module global at call time,
    so swapping the module attribute is enough. Single-threaded use only.
    Restores whatever was there before, so it nests.
    """
    previous = image_abc._apply_prepare_enhancements
    image_abc._apply_prepare_enhancements = lambda canvas, _cfg, _keepout=None: canvas
    try:
        yield
    finally:
        image_abc._apply_prepare_enhancements = previous


def picture_rect(padding: np.ndarray) -> tuple[int, int, int, int] | None:
    """(x0, y0, x1, y1) of the non-padding rectangle, or None if there is no padding."""
    if not padding.any():
        return None
    rows = np.flatnonzero(~padding.all(axis=1))
    cols = np.flatnonzero(~padding.all(axis=0))
    return int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1


class PhotoFirstRenderer(ImageRenderer):
    """Production rendering, except the photo is enhanced without its bars.

    ``bar_ink`` picks the letterbox colour: 1 is white ink, what production
    forces (``image_renderer.py:806``); 0 is black. Once the photo is processed
    on its own the bar colour no longer changes the picture's tone, so it is a
    free aesthetic choice — and black is filled into the canvas *before*
    dithering, so the picture's edge is diffused against black rather than
    against a white bar that is then repainted.
    """

    def __init__(self, *args, bar_ink: int = 1, **kwargs):
        super().__init__(*args, **kwargs)
        if bar_ink not in BAR_RGB:
            raise ValueError(f"bar_ink must be 0 (black) or 1 (white), got {bar_ink}")
        self._bar_ink = bar_ink
        self._last_padding: np.ndarray | None = None

    def render_indices(self, *args, **kwargs):  # type: ignore[override]
        idx = super().render_indices(*args, **kwargs)
        if self._bar_ink != 1 and self._last_padding is not None:
            idx[self._last_padding] = self._bar_ink
        return idx

    def _prepare_canvas(  # type: ignore[override]
        self,
        img: Image.Image,
        cfg: ImageConfig,
        orientation,
        canvas_w: int,
        canvas_h: int,
        crop_to_fill_threshold: float = 0.0,
        *,
        release_input: bool = False,
        clahe_keepout_bboxes_norm=None,
        crop_anchor_bboxes_norm=None,
    ):
        if clahe_keepout_bboxes_norm:
            # Face keep-out boxes arrive in canvas coordinates of the whole canvas;
            # the bench never passes them, and translating them is not worth
            # getting wrong for an experiment.
            raise NotImplementedError("PhotoFirstRenderer does not take CLAHE keep-outs")
        with _geometry_only():
            arr, padding = super()._prepare_canvas(
                img,
                cfg,
                orientation,
                canvas_w,
                canvas_h,
                crop_to_fill_threshold,
                release_input=release_input,
                crop_anchor_bboxes_norm=crop_anchor_bboxes_norm,
            )
        # Production enhances in the visible orientation and rotates afterwards
        # (image_abc.py:361-365). Undo that rotation so the enhancement sees the
        # same layout — CLAHE's tile grid is not rotation-invariant — then redo it.
        # np.rot90 is an exact index permutation, so nothing is resampled.
        rotated = self._display.panel_rotated and orientation != "portrait"
        vis = np.array(np.rot90(arr, 1) if rotated else arr, dtype=np.uint8)
        pad_vis = np.rot90(padding, 1) if rotated else padding
        rect = picture_rect(pad_vis)
        if rect is None:
            vis = np.asarray(_ENHANCE(Image.fromarray(vis), cfg, None), dtype=np.uint8)
        else:
            x0, y0, x1, y1 = rect
            photo = Image.fromarray(vis[y0:y1, x0:x1])
            vis[y0:y1, x0:x1] = np.asarray(_ENHANCE(photo, cfg, None), dtype=np.uint8)
            vis[pad_vis] = BAR_RGB[self._bar_ink]
        out = np.rot90(vis, 3) if rotated else vis
        # render_indices repaints the bars after dithering; it needs the mask,
        # which the base class does not hand back. Single-threaded use only.
        self._last_padding = padding
        return np.ascontiguousarray(out), padding


_RENDERERS: dict[tuple[str, int], PhotoFirstRenderer] = {}


def photo_first_renderer(display, bar_ink: int = 1) -> PhotoFirstRenderer:
    """One per display and bar colour per process, like render_bank.renderer_for."""
    key = (display.model_id, bar_ink)
    if key not in _RENDERERS:
        _RENDERERS[key] = PhotoFirstRenderer(
            dither=NumbaStreamingDither(display), display=display, bar_ink=bar_ink
        )
    return _RENDERERS[key]


def plan_from(source: dict, reference_arm: str, display, black_bars: bool = True) -> dict:
    """A photo-first A/B over the letterboxed images of an existing plan.

    The reference is captured afresh under its own tag rather than reused from
    the source plan's run. Captures made before the bench loaded images through
    production's ``open_image_for_render`` differ from it by more than the fix
    under test — no EXIF rotation, no pre-shrink — so reusing them would pair
    a fixed-loader render against an old-loader one. Both arms of every image come
    from one sitting, one calibration and one loader.
    """
    refs = [c for c in source["candidates"] if c["config_tag"] == reference_arm]
    candidates, images, bars = [], [], []
    for c in refs:
        padding = padding_visible(Path(c["image"]), display)
        if not padding.any():
            continue  # full-frame: the two arms render identically
        images.append(c["image_name"])
        bars.append(float(padding.mean()))
        stem = c["tag"].rsplit("__", 1)[0]
        ref = dict(c)
        ref["tag"] = f"{stem}__lb_ref"
        ref["config_tag"] = "lb_ref"
        candidates.append(ref)
        new = dict(c)
        new["tag"] = f"{stem}__photo_first"
        new["config_tag"] = "photo_first"
        new["prepare"] = "photo_first"
        candidates.append(new)
        if black_bars:
            black = dict(new)
            black["tag"] = f"{stem}__photo_first_black"
            black["config_tag"] = "photo_first_black"
            black["bars"] = "black"
            candidates.append(black)
    arms = {"lb_ref": "production order", "photo_first": "enhance the photo without its bars"}
    if black_bars:
        arms["photo_first_black"] = "as photo_first, with black bars"
    return {
        "model": source["model"],
        "baseline_config": source["baseline_config"],
        "arms": arms,
        "images": images,
        "bars": dict(zip(images, bars, strict=True)),
        "candidates": candidates,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse  # noqa: PLC0415
    import json  # noqa: PLC0415

    from hokku.screens.registry import DISPLAY_REGISTRY  # noqa: PLC0415

    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="photo-first A/B over an existing plan's letterboxed images")
    p.add_argument("--from", dest="source", type=Path, required=True)
    p.add_argument("--reference-arm", default="ref")
    p.add_argument("--no-black", action="store_true", help="omit the black-bar arm")
    p.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    source = json.loads(args.source.read_text(encoding="utf-8"))
    display = DISPLAY_REGISTRY[source["model"]]
    plan = plan_from(source, args.reference_arm, display, black_bars=not args.no_black)
    args.out.write_text(json.dumps(plan, indent=1), encoding="utf-8")
    bars = list(plan["bars"].values())
    print(
        f"  {len(plan['images'])} letterboxed of {len(source['images'])} images, bars "
        f"{100 * min(bars):.0f}-{100 * max(bars):.0f} % of the canvas; "
        f"{len(plan['candidates'])} captures -> {args.out}"
    )
    return 0


def padding_visible(image: Path, display, crop_to_fill_threshold: float = 0.0) -> np.ndarray:
    """Letterbox padding mask in visual (landscape) coordinates, as the bench renders.

    Pass the decision's crop threshold for a production-faithful plan: at the
    live 0.14 a 3:2 photograph is cropped to fill and has no padding at all.
    """
    from render_bank import to_visible  # noqa: PLC0415 — avoid an import cycle

    renderer = ImageRenderer(dither=NumbaStreamingDither(display), display=display)
    with open_image_for_render(Path(image)) as img, _geometry_only():
        _arr, padding = renderer._prepare_canvas(
            img,
            PRESET_IMAGE_CONFIGS["calibration_raw"],
            Orientation.LANDSCAPE,
            display.panel_w,
            display.panel_h,
            crop_to_fill_threshold,
        )
    return to_visible(np.asarray(padding), display)


if __name__ == "__main__":
    sys.exit(main())
