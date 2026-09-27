#!/usr/bin/env python3
"""Render a config on an image and measure it, without a human and without glass.

This is the workhorse the search and the preference fit both stand on: one call
renders an ImageConfig through the *production* pipeline and returns a vector of
measurements of the result. Nothing here decides what is good — deliberately.
Every earlier attempt in this project to collapse these numbers into a single
score got overturned by someone looking at the panel, so the collapsing is left
to a weighting fitted against real verdicts (see fit_preference.py) and this
module only supplies the evidence.

Three things make the measurement worth trusting:

**The panel model, not the ink table.** At full resolution every pixel is one of
six inks, so comparing pixels to the source measures dithering, not colour. The
Yule-Nielsen model from the 1733-patch campaign (n=1.38, 1.50 dE residual)
predicts what a block of dithered ink actually looks like, which is what an eye
integrating over that block sees. Metrics prefixed ``yn_`` use it.

**Two references, because they answer different questions.** Against the raw
source, a bright photograph always measures "too dark" — the panel tops out at
L* 66.9 and no pipeline can fix that, so that error is not actionable. Against
the source rescaled into the panel's own range (``adapted_reference``), the
question becomes the answerable one: given 56 L* of range, is it used well?
Both are reported; ``_adapted`` marks the second.

**Detail, separately from colour.** A minimum-dE gamut map once raised measured
red-wall chroma from 11 to 33 while flattening the shading into a slab, and the
chroma metric called that an improvement. ``detail_l``/``detail_c`` are the
guard against that, and they are why this returns a vector and not a number.

Results are cached in SQLite keyed by image content, config slug, display (the
correction LUT rides on it), crop decision, resolution and a hash of the metric
code, so a search that revisits a config pays for it once and a row is never
served to something it was not measured for. Workers compute; the parent writes.

    python tools/render_bank.py --image build/camcal/server_images/foo.jpg
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from cam_compare import block_mean, delta_e00, detail_ratio, model_lab, source_canvas
from color_model import INK_NAMES, fit_n, load_records, primaries
from color_validate_photos import lab_img, srgb_img_to_xyz
from gamut_clamp import chroma_ceiling, lookup_ceiling, reachable_lab
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_streaming import rgb_to_lab
from hokku.webserver.dither_streaming_numba import NumbaStreamingDither
from hokku.webserver.image_config import ImageConfig
from hokku.webserver.image_renderer import ImageRenderer, open_image_for_render
from hokku.webserver.orientation import Orientation

# Block size for the Yule-Nielsen prediction, in panel pixels. 8 is what the
# camera comparisons used, so numbers here are directly comparable to the
# photographed ones in build/camcal/comparison.json.
BLOCK = 8

# Measured on this glass by the campaign (findings.md): the reachable lightness
# range. Everything outside it is a deficit no config can close.
PANEL_BLACK_L, PANEL_WHITE_L = 10.86, 66.94

# Below this source chroma gradient, a chroma-detail ratio is meaningless.
# Greyscale photographs measure ~0.002 here; colour ones 2.3-7.4.
CHROMA_DETAIL_FLOOR = 0.25

# Likewise for the *spread* of source chroma, which divides chroma_contrast_ratio.
# A neutral photograph has essentially none, and the quotient then reports
# hundreds rather than degrading gracefully.
CHROMA_SPREAD_FLOOR = 1.0

_MODEL: dict | None = None
_RENDERER: dict = {}
_REFERENCE: dict = {}


# ── the panel model ──────────────────────────────────────────────────────────


def campaign_path(model: str) -> Path:
    return Path("docs/screens") / model / "measurements/data/campaign.jsonl"


def load_model(model: str = "huessen_epf1301", cache_dir: Path = Path("build/camcal")) -> dict:
    """Fit the Yule-Nielsen model and the gamut ceiling — once per machine.

    All three outputs are pure functions of the campaign file, so they are cached
    to disk and keyed by that file's hash. This matters more than it looks: the
    fit brute-forces 1101 candidate n values over 1475 records and the ceiling
    samples a 40 000-point simplex, and a 24-worker pool otherwise pays that
    twenty-four times over on every start — repeatedly, since the search restarts
    pools. Recomputing it also re-reads a 3.5 MB JSONL from every worker at once.

    Module-level memo on top, for repeated calls inside one process.
    """
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    data = campaign_path(model)
    key = hashlib.sha256(data.read_bytes()).hexdigest()[:16]
    cached = cache_dir / f"panel_model_{model}_{key}.npz"
    if cached.exists():
        with np.load(cached) as z:
            _MODEL = {"prim_mat": z["prim_mat"], "n": float(z["n"]), "ceiling": z["ceiling"]}
        return _MODEL
    records = load_records(data)
    prim = primaries(records)
    n_yn, _err = fit_n([r for r in records if r.get("source") != "ink"], prim, spectral=False)
    _MODEL = {
        "prim_mat": np.array([prim[k]["xyz"] for k in INK_NAMES]),
        "n": n_yn,
        "ceiling": chroma_ceiling(reachable_lab(records)),
    }
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Write via a temp file: several workers can reach this at once on a cold
    # cache, and a half-written npz is worse than no cache at all.
    tmp = cached.with_suffix(f".{os.getpid()}.tmp.npz")
    np.savez_compressed(tmp, **_MODEL)
    tmp.replace(cached)
    return _MODEL


def renderer_for(display) -> ImageRenderer:
    """One renderer per display per process — the numba dither JITs on first use.

    Production builds exactly this pair (render_worker.py:85), so a measurement
    here is a measurement of the shipped code path rather than a lookalike. It is
    also ~9x faster than the pure-numpy StreamingDither: 1.33 s against 12.0 s
    for a full 1200x1600 panel, which is what makes a full-resolution search
    affordable at all.
    """
    key = display.model_id
    if key not in _RENDERER:
        _RENDERER[key] = ImageRenderer(dither=NumbaStreamingDither(display), display=display)
    return _RENDERER[key]


def adapted_reference(src_lab: np.ndarray) -> np.ndarray:
    """The source rescaled into the panel's reachable lightness range.

    Chroma is deliberately left alone, so chroma shortfall still measures
    honestly rather than being defined away. Only lightness is renormalised,
    because that is the axis where the panel's limit otherwise swamps every
    difference between configs.
    """
    out = src_lab.copy()
    out[..., 0] = PANEL_BLACK_L + src_lab[..., 0] / 100.0 * (PANEL_WHITE_L - PANEL_BLACK_L)
    return out


# ── rendering ────────────────────────────────────────────────────────────────


def to_visible(idx: np.ndarray, display) -> np.ndarray:
    return np.rot90(idx, k=1) if display.panel_rotated else idx


def render(
    display,
    img: Image.Image,
    cfg: ImageConfig,
    div: int = 1,
    seed: int | None = None,
    crop_to_fill_threshold: float = 0.0,
) -> np.ndarray:
    """Palette-index raster in *visual* orientation, via the production pipeline.

    Seeded, because the pipeline is not reproducible on its own: at
    ``image_renderer.py:800`` the ``dither_noise`` term is drawn from numpy's
    global unseeded RNG, so the same image and config render differently every
    time. That is harmless when a panel shows a picture once, and corrosive when
    the same config is scored twice and the two scores disagree — a search would
    then chase its own noise, and a cached measurement would never reproduce.

    Seeding the global RNG here fixes it for measurement without touching the
    shipped renderer: production keeps its fresh noise, the bank gets a
    deterministic function of (image, config, seed). ``seed=None`` restores the
    production behaviour, which is what the repeat-measurement path uses to
    estimate how much the noise moves a score.
    """
    if seed is not None:
        np.random.seed(seed % (2**32))
    w, h = display.panel_w // div, display.panel_h // div
    idx = renderer_for(display).render_indices(
        img.copy(), cfg, Orientation.LANDSCAPE, w, h, crop_to_fill_threshold
    )
    return to_visible(idx, display)


def render_seed(image: Path, cfg: ImageConfig) -> int:
    """A stable seed per (image, config) — same pair, same noise, every run."""
    digest = hashlib.sha256(f"{image.name}|{cfg.cache_slug()}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


# ── measurement ──────────────────────────────────────────────────────────────


def _region_masks(src_lab: np.ndarray, ceiling: np.ndarray) -> dict[str, np.ndarray]:
    """Where in the picture to measure separately.

    Skin and neutral use the same definitions as cam_compare and
    color_validate_photos so numbers stay comparable across tools. ``oog`` is the
    region the panel physically cannot reach, which is where error diffusion does
    its worst work and where the configs differ most.
    """
    chroma = np.hypot(src_lab[..., 1], src_lab[..., 2])
    hue = np.degrees(np.arctan2(src_lab[..., 2], src_lab[..., 1]))
    lightness = src_lab[..., 0]
    return {
        "skin": ((hue > -40) & (hue < 70) & (chroma > 12) & (lightness > 20)),
        "neutral": chroma < 5,
        "shadow": lightness < 25,
        "highlight": lightness > 75,
        "oog": chroma > lookup_ceiling(ceiling, src_lab),
    }


def _chroma_variation(lab: np.ndarray, keep: np.ndarray) -> float:
    """Mean absolute chroma gradient — the denominator detail_ratio divides by."""
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    gy = np.abs(np.diff(chroma, axis=0))[:, :-1]
    gx = np.abs(np.diff(chroma, axis=1))[:-1, :]
    inner = keep[:-1, :-1]
    return float(np.mean((gy + gx)[inner])) if inner.any() else 0.0


def _pair_stats(src: np.ndarray, got: np.ndarray, mask: np.ndarray, prefix: str) -> dict:
    """dE / dL / dC / dh of a measured Lab plane against a reference, over a mask."""
    if mask.sum() < 50:
        return {}
    src_c = np.hypot(src[..., 1], src[..., 2])
    got_c = np.hypot(got[..., 1], got[..., 2])
    out = {
        f"{prefix}de00": float(delta_e00(src, got)[mask].mean()),
        f"{prefix}dL": float((got[..., 0] - src[..., 0])[mask].mean()),
        f"{prefix}dC": float((got_c - src_c)[mask].mean()),
    }
    # Hue error is meaningless where there is no chroma to have a hue, and a mean
    # over near-neutrals is dominated by noise. Weight by source chroma and only
    # count pixels that actually carry colour.
    chromatic = mask & (src_c > 8)
    if chromatic.sum() >= 50:
        src_h = np.degrees(np.arctan2(src[..., 2], src[..., 1]))
        got_h = np.degrees(np.arctan2(got[..., 2], got[..., 1]))
        dh = (got_h - src_h + 180.0) % 360.0 - 180.0
        weight = src_c[chromatic]
        out[f"{prefix}dhue"] = float(np.abs(dh[chromatic]) @ weight / weight.sum())
    return out


FACE_CACHE = Path("build/camcal/face_boxes.json")


def face_boxes_on_canvas(
    image: Path, canvas: np.ndarray
) -> list[tuple[float, float, float, float]]:
    """Detected faces as (x, y, w, h) fractions of the RENDER canvas.

    The production detector normalises boxes to the source *file*, but the
    pipeline crops and fits before rendering, so those coordinates do not
    survive. Running the same detector on the canvas instead puts the boxes in
    the space the metrics are computed in — and a portrait cropped to landscape
    genuinely may not contain the face the file did, which is the honest answer
    rather than a mapping artefact.

    This exists because the hue-based "skin" mask is not a face detector: it
    fires on brick, red walls and clothing. The rating notes named blue-in-faces
    as one of the top complaints while the hue-masked blue metrics tested
    unstable, and measuring inside real faces is the obvious way to close that
    gap.

    Cached on disk: the YuNet graph costs ~57 MB and several seconds per load.
    """
    cache = {}
    if FACE_CACHE.exists():
        try:
            cache = json.loads(FACE_CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}
    # Keyed on the canvas CONTENT, not only its size: a sideways and an upright
    # render of one photo share a name and a size, and the boxes of one are
    # wrong for the other. A coarse subsample is enough to tell canvases apart.
    digest = hashlib.sha256(np.ascontiguousarray(canvas[::16, ::16]).tobytes()).hexdigest()[:12]
    key = f"{image.name}|{canvas.shape[1]}x{canvas.shape[0]}|{digest}"
    if key in cache:
        return [tuple(b) for b in cache[key]]

    import tempfile  # noqa: PLC0415 — only the cache-miss path needs these

    import cv2  # noqa: PLC0415

    from hokku.webserver.face_detect_yunet_opencv import OpenCVYuNetFaceDetector  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        shot = Path(tmp) / "canvas.png"
        cv2.imwrite(str(shot), cv2.cvtColor(canvas, cv2.COLOR_RGB2BGR))
        boxes = [(b.x, b.y, b.w, b.h) for b in OpenCVYuNetFaceDetector().detect(shot)]
    cache[key] = [list(b) for b in boxes]
    FACE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    FACE_CACHE.write_text(json.dumps(cache), encoding="utf-8")
    return boxes


def face_mask(boxes, shape: tuple[int, int]) -> np.ndarray:
    """Boolean mask over the given grid covering every detected face box."""
    mask = np.zeros(shape, bool)
    height, width = shape
    for x, y, w, h in boxes:
        x0, y0 = int(x * width), int(y * height)
        x1, y1 = int(min((x + w) * width, width)), int(min((y + h) * height, height))
        if x1 > x0 and y1 > y0:
            mask[y0:y1, x0:x1] = True
    return mask


def reference_for(
    image: Path,
    display,
    model: dict,
    block: int,
    div: int,
    crop_to_fill_threshold: float = 0.0,
) -> dict:
    """Everything about the SOURCE that no config can change, computed once.

    The geometry, the Lab conversion and the region masks depend only on the
    image and the classifier's crop decision — not on ImageConfig — so every
    candidate for one picture shares an identical canvas. Recomputing them per
    candidate was costing more than the render itself: measured at 1.0 render/s
    across 24 workers where the render alone supports roughly 18/s.

    ``crop_to_fill_threshold`` comes from the classifier decision and decides
    whether the picture is fitted or cropped to fill, so it changes the canvas.
    It defaulted to 0 here while production plans carry 0.14, which compared a
    fitted reference against a fill-cropped render — every metric on such a pair
    measures the offset, not the colour.

    Cached per process and keyed by (path, block, div, threshold). Jobs are
    dispatched image-major, so a worker sees a run of candidates for one image
    and a two-entry cache is enough; it is cleared rather than grown so a
    245-image sweep cannot accumulate a gigabyte of canvases per worker.
    """
    key = (str(image), block, div, crop_to_fill_threshold)
    hit = _REFERENCE.get(key)
    if hit is not None:
        return hit
    canvas = source_canvas(image, display, "x", crop_to_fill_threshold)
    # Measure the picture, not the letterbox. A portrait photo on this landscape
    # panel is 40-50 % white bar, which every render reproduces exactly, so any
    # whole-canvas mean, percentile or ratio was diluted by that much and the
    # dilution varied with the photo's shape. The picture is always a rectangle,
    # so cropping to it once makes every reduction below a picture-only one.
    from letterbox import padding_visible, picture_rect  # noqa: PLC0415 — avoids a cycle

    full = picture_rect(padding_visible(image, display, crop_to_fill_threshold))
    if full is None:
        full = (0, 0, canvas.shape[1], canvas.shape[0])
    # The render arrives at 1/div of the canvas, so its crop is the scaled rect.
    rect = tuple(v // div for v in full)
    canvas = canvas[full[1] : full[3], full[0] : full[2]]
    if div != 1:
        target = (rect[2] - rect[0], rect[3] - rect[1])
        canvas = np.asarray(Image.fromarray(canvas).resize(target, Image.Resampling.LANCZOS))
    src_lab = lab_img(block_mean(srgb_img_to_xyz(canvas), block))
    # Full-resolution source colorimetry, for the artifact metrics. Config
    # independent, and by far the most expensive thing in an evaluation if it is
    # recomputed per candidate.
    full_lab = rgb_to_lab(np.asarray(canvas, dtype=np.float64))
    full_c = np.hypot(full_lab[..., 1], full_lab[..., 2])
    full_hue = np.arctan2(full_lab[..., 2], full_lab[..., 1])
    boxes = face_boxes_on_canvas(image, canvas)
    reference = {
        "rect": rect,
        "boxes": boxes,
        "face_full": face_mask(boxes, canvas.shape[:2]),
        "face": face_mask(boxes, src_lab.shape[:2]),
        "canvas": canvas,
        "src_lab": src_lab,
        "ref_lab": adapted_reference(src_lab),
        "masks": _region_masks(src_lab, model["ceiling"]),
        "everywhere": np.ones(src_lab.shape[:2], bool),
        "full_lab": full_lab,
        "full_c": full_c,
        "full_hue": full_hue,
        "neutral": full_c < 10.0,
        "saturated": full_c > 25.0,
        "warm": ((full_hue > np.radians(-40.0)) & (full_hue < np.radians(70.0))),
    }
    # Evict only per-image entries. The shape-keyed FFT mask and the ink Lab
    # table live in the same dict and are tiny, shared and shape-invariant —
    # clearing them would reintroduce the cost this cache exists to remove.
    images_held = [k for k in _REFERENCE if isinstance(k, tuple)]
    if len(images_held) >= 2:
        for k in images_held:
            del _REFERENCE[k]
    _REFERENCE[key] = reference
    return reference


# The six complaints that 72 of 78 free-text rating notes name, as hue sectors
# for the chroma-gain metrics below. Boundaries are the usual 60-degree sectors
# on the source's own hue, so "the grass went neon" is asked of pixels that were
# green to begin with rather than of pixels that ended up green.
HUE_SECTORS = {
    "red": (-30.0, 30.0),
    "yellow": (30.0, 90.0),
    "green": (90.0, 150.0),
    "cyan": (150.0, 210.0),
    "blue": (210.0, 270.0),
    "magenta": (270.0, 330.0),
}
# Below this source chroma a pixel has no colour to be neon about, and its hue
# is noise. Above this chroma gain it is visibly more saturated than the source.
COMPLAINT_CHROMA_FLOOR = 8.0
COMPLAINT_GAIN = 10.0


def complaint_metrics(reference: dict, src_lab: np.ndarray, yn_lab: np.ndarray) -> dict[str, float]:
    """Metrics named after what the notes actually complain about.

    The bank's general metrics turned out to be one-dimensional against the
    ratings: fitted over all 65 of them, held-out accuracy matched the single
    best metric alone. That is not a sample-size problem — 832 ratings did no
    better than 80 — it is the bank measuring overall error six ways and none of
    the six specific failures a person names when they look at the glass:

        oversaturated / neon (33 notes), blue in skin or lips (10), washed out
        (10), yellow skin or hair (10), crushed black (5), banding (3)

    Each metric here is one of those, as a signed quantity so its direction is
    readable, and measured where the complaint lives rather than over the frame.
    """
    out: dict[str, float] = {}
    src_c = np.hypot(src_lab[..., 1], src_lab[..., 2])
    got_c = np.hypot(yn_lab[..., 1], yn_lab[..., 2])
    hue = np.degrees(np.arctan2(src_lab[..., 2], src_lab[..., 1]))
    coloured = src_c >= COMPLAINT_CHROMA_FLOOR

    # "all colours look neon", "neon grass", "red over saturated". Chroma GAIN,
    # not chroma: the complaint is that the render added saturation the
    # photograph did not have, which is a different thing from a colourful photo.
    gain = got_c - src_c
    worst = []
    for name, (low, high) in HUE_SECTORS.items():
        centred = (hue - low) % 360.0
        mask = coloured & (centred < (high - low) % 360.0)
        if mask.sum() >= 200:
            value = float(gain[mask].mean())
            out[f"neon_{name}"] = value
            worst.append(value)
    if worst:
        out["neon_worst"] = max(worst)
    if coloured.sum() >= 200:
        # How much of the picture is visibly more saturated than it should be —
        # "all 3 are terrible, with orange crowns oversaturated red" is about an
        # area of the picture, not about its average.
        out["neon_area"] = float((gain[coloured] > COMPLAINT_GAIN).mean())

    # "skin of baby is yellow", "all 3 have yellow skin", "hair too neon yellow".
    # b* is the yellow-blue axis, so its signed shift is the complaint itself;
    # the existing skin_dhue and skin_dC cannot tell yellow from blue.
    for region in ("skin", "face"):
        mask = reference["masks"]["skin"] if region == "skin" else reference["face"]
        if mask.sum() >= 200:
            out[f"{region}_db"] = float((yn_lab[..., 2] - src_lab[..., 2])[mask].mean())
            out[f"{region}_da"] = float((yn_lab[..., 1] - src_lab[..., 1])[mask].mean())

    # "hands are way too black and look dead in most images". Measured against
    # the panel-adapted reference, so it is the pipeline's crush and not the
    # panel's floor, which no config can lift.
    ref_lab = reference["ref_lab"]
    dark = (src_lab[..., 0] > 18.0) & (src_lab[..., 0] < 45.0)
    if dark.sum() >= 200:
        out["shadow_crush"] = float((ref_lab[..., 0] - yn_lab[..., 0])[dark].mean())

    # "oks have the sky washed out to white", "5 looks washed out". Detail lost
    # off the top, as the share of bright source that lands on the panel's white.
    bright = src_lab[..., 0] > 75.0
    if bright.sum() >= 200:
        out["highlight_blown"] = float((yn_lab[..., 0][bright] > 0.95 * PANEL_WHITE_L).mean())

    # "led of girl has hard lines in them from lack of range in nuance": structure
    # appearing where the photograph was smooth. Taken over the flattest quarter
    # of the picture, where a gradient in the render cannot have come from the
    # source.
    src_grad = _gradient(src_lab[..., 0])
    if src_grad.size >= 400:
        flat = src_grad <= np.percentile(src_grad, 25)
        if flat.sum() >= 100:
            out["banding"] = float(
                _gradient(yn_lab[..., 0])[flat].mean() / max(src_grad[flat].mean(), 1e-6)
            )
    return out


def _gradient(plane: np.ndarray) -> np.ndarray:
    """Local gradient magnitude, same shape as the input."""
    dy, dx = np.gradient(plane.astype(np.float64))
    return np.hypot(dy, dx)


def ink_lab(display) -> np.ndarray:
    """CIELAB of the six inks. The derived image only ever contains these."""
    key = f"inklab_{display.model_id}"
    hit = _REFERENCE.get(key)
    if hit is None:
        hit = rgb_to_lab(np.asarray(display.palette_measured_rgb, dtype=np.float64))
        _REFERENCE[key] = hit
    return hit


def _highpass_mask(shape: tuple[int, int]) -> np.ndarray:
    """Which FFT bins count as high frequency. Depends only on the shape.

    Built with np.indices, which allocates two arrays the size of the image;
    doing that per evaluation was pure waste since every image here is the same
    size.
    """
    key = f"highpass_{shape}"
    hit = _REFERENCE.get(key)
    if hit is None:
        h, w = shape
        y, x = np.indices(shape)
        hit = np.sqrt((y - h // 2) ** 2 + (x - w // 2) ** 2) >= min(h, w) / 4.0
        _REFERENCE[key] = hit
    return hit


def ink_metrics(reference: dict, idx: np.ndarray, display) -> dict[str, float]:
    """The full-resolution artifact metrics, computed by lookup rather than by conversion.

    Equivalent to `image_quality.image_compare` plus `dither_search.warm_blue_fractions`,
    with two changes made deliberately.

    **The derived image is not converted.** It contains exactly six colours, so
    its Lab is a six-entry table indexed by the ink raster. Converting 1.9 M
    pixels to compute six values was most of the cost. For the same reason
    `neutral_blue_fraction` needs no nearest-palette search: the nearest palette
    entry to an ink *is* that ink, so the answer is already in `idx`.

    **Full-resolution dE2000 is dropped.** It measured 5.9 s per evaluation —
    59 % of the total — and at full resolution every pixel is one ink, so a
    per-pixel colour difference is dominated by the dither pattern rather than by
    colour. The block-level `yn_de00` answers that question properly, by
    modelling what the eye integrates. Nothing is lost that was worth having.
    """
    # Callers pass the raster already cropped to the picture (see measure).
    lab = ink_lab(display)
    der_lab = lab[idx]
    der_c = np.hypot(der_lab[..., 1], der_lab[..., 2])
    src_lab = reference["full_lab"]
    src_c = reference["full_c"]
    neutral = reference["neutral"]
    saturated = reference["saturated"]

    diff = der_lab - src_lab
    de76 = np.sqrt((diff**2).sum(axis=-1))
    out = {
        "ink_neutral_leak": float(der_c[neutral].mean()) if neutral.any() else 0.0,
        "ink_sat_hit": float((der_c[saturated] > 15.0).mean()) if saturated.any() else 0.0,
        "ink_overall_dE": float(de76.mean()),
        "ink_error_roughness": float(de76.std()),
        "ink_lightness_dE": float(np.abs(diff[..., 0]).mean()),
        "ink_chroma_dE": float(np.abs(der_c - src_c).mean()),
        "ink_neutral_blue_fraction": (float((idx[neutral] == 4).mean()) if neutral.any() else 0.0),
    }
    if saturated.any():
        delta = np.abs(
            reference["full_hue"][saturated]
            - np.arctan2(der_lab[saturated, 2], der_lab[saturated, 1])
        )
        out["ink_hue_error"] = float(np.degrees(np.minimum(delta, 2 * np.pi - delta).mean()))
    else:
        out["ink_hue_error"] = 0.0

    # Where the L* error energy sits spatially: high means fine-grained, which
    # reads as texture rather than as blotches.
    err = diff[..., 0]
    power = np.abs(np.fft.fftshift(np.fft.fft2(err))) ** 2
    total = power.sum()
    out["ink_high_freq_energy_ratio"] = (
        float(power[_highpass_mask(err.shape)].sum() / total) if total > 0 else 0.0
    )

    # Blue ink leaking into warm colour, split by chroma: the broad view is
    # dominated by skin, the artifact people complain about lives in the small
    # saturated regions (lips, blush).
    for name, floor in (("ink_warm_blue", 20.0), ("ink_lips_blue", 38.0)):
        sel = reference["warm"] & (src_c > floor)
        out[name] = float((idx[sel] == 4).mean()) if sel.any() else 0.0

    # Ink actually laid down inside detected faces. "blue lips", "blue face,
    # looks dead" and "faces WAY too red" were the two commonest complaints, and
    # the hue-masked versions above cannot separate a face from a brick wall.
    faces = reference["face_full"]
    if faces.sum() >= 200:
        out["face_ink_blue"] = float((idx[faces] == 4).mean())
        out["face_ink_red"] = float((idx[faces] == 3).mean())
        out["face_ink_black"] = float((idx[faces] == 0).mean())
        out["face_ink_yellow"] = float((idx[faces] == 2).mean())
    return out


def measure(
    reference: dict, idx_visual: np.ndarray, display, model: dict, block: int = BLOCK
) -> dict[str, float]:
    """Every metric for one render. No weighting, no verdict — just evidence.

    ``reference`` comes from ``reference_for`` and carries the source-side work;
    ``idx_visual`` is the ink raster in visual orientation at the canvas size.
    It is cropped here to the picture rectangle the reference was built on, so
    nothing below ever sees letterbox bars; ``rect`` None means the whole raster.
    """
    rect = reference["rect"]
    if rect is not None:
        x0, y0, x1, y1 = rect
        idx_visual = idx_visual[y0:y1, x0:x1]
    k = block
    src_lab = reference["src_lab"]
    yn_lab = model_lab(idx_visual, model["prim_mat"], model["n"], k)
    ref_lab = reference["ref_lab"]
    masks = reference["masks"]
    everywhere = reference["everywhere"]

    out: dict[str, float] = {}
    out.update(_pair_stats(src_lab, yn_lab, everywhere, "yn_"))
    out.update(_pair_stats(ref_lab, yn_lab, everywhere, "yn_adapted_"))
    out["yn_de00_p90"] = float(np.percentile(delta_e00(src_lab, yn_lab), 90))
    out["yn_adapted_rmsL"] = float(np.sqrt(np.mean((yn_lab[..., 0] - ref_lab[..., 0]) ** 2)))
    for name, mask in masks.items():
        out.update(_pair_stats(src_lab, yn_lab, mask, f"{name}_"))
        out[f"{name}_frac"] = float(mask.mean())

    # Inside actually-detected faces, not a hue mask. These are the regions the
    # rating notes complained about by name.
    faces = reference["face"]
    out["face_frac"] = float(faces.mean())
    if faces.sum() >= 50:
        out.update(_pair_stats(src_lab, yn_lab, faces, "face_"))
        dl, _dc = detail_ratio(src_lab, yn_lab, faces)
        out["face_detail_l"] = float(dl)
        out["face_contrast"] = float(
            yn_lab[..., 0][faces].std() / max(src_lab[..., 0][faces].std(), 1e-6)
        )

    # "Washed out" was a third of the complaints and nothing measured it: how
    # much of the source's global contrast survives, which is a different
    # question from the local gradient ratio detail_l already reports.
    out["contrast_ratio"] = float(yn_lab[..., 0].std() / max(src_lab[..., 0].std(), 1e-6))
    # Omitted for a source with no chroma variation to preserve, the same trap
    # `detail_c` and `chroma_vs_source` above already guard against and this one
    # did not: the denominator is the spread of source chroma, which on a
    # greyscale photograph is ~0. Measured 639.6 on one library image against a
    # judged range of [0.18, 1.38], and since the objective weights this term
    # -1.52, a search "improving" it scored a fictional +970 rating points and
    # dominated every real difference in a 90-image run.
    src_c_spread = float(np.hypot(src_lab[..., 1], src_lab[..., 2]).std())
    if src_c_spread >= CHROMA_SPREAD_FLOOR:
        out["chroma_contrast_ratio"] = float(
            np.hypot(yn_lab[..., 1], yn_lab[..., 2]).std() / src_c_spread
        )

    # Detail. The reason this returns a vector: a config can win every colour
    # metric above by flattening the picture, and this is what catches it.
    #
    # The chroma half is omitted where the source has no chroma variation to
    # preserve. detail_ratio divides by the source's mean chroma gradient with
    # only a 1e-6 floor, and on a black-and-white photograph that denominator is
    # ~0.002 against 2.3-7.4 for a colour one — three orders of magnitude, so the
    # ratio blows up rather than degrading gracefully. Measured across the
    # library: median detail_c 1.06 but maximum 1431, and a preset summary mean
    # of 26.5 driven entirely by a handful of near-neutral images. Same trap as
    # chroma_vs_source; the threshold sits ~10x clear of both populations.
    keep_l, keep_c = detail_ratio(src_lab, yn_lab, everywhere)
    out["detail_l"] = float(keep_l)
    if _chroma_variation(src_lab, everywhere) >= CHROMA_DETAIL_FLOOR:
        out["detail_c"] = float(keep_c)
    for name in ("skin", "oog"):
        if masks[name].sum() >= 500:
            dl, dc = detail_ratio(src_lab, yn_lab, masks[name])
            out[f"detail_l_{name}"] = float(dl)
            if _chroma_variation(src_lab, masks[name]) >= CHROMA_DETAIL_FLOOR:
                out[f"detail_c_{name}"] = float(dc)

    # How much of the reachable chroma the render actually spends. Low means the
    # picture is duller than the glass can manage; near 1 means the gamut is
    # being used for what it is worth.
    #
    # As a ratio of means, not a mean of ratios: near black and near white the
    # reachable chroma goes to zero, and a per-pixel ratio there divides by
    # nothing and explodes (this read 186696 before the fix). Bins with no
    # usable chroma are excluded outright rather than floored, since a ratio
    # against a ceiling of 0.001 is noise however it is clamped.
    # The ceiling is looked up at the RENDER's own lightness and hue, not the
    # source's. Asking "of the chroma available where this pixel actually
    # landed, how much was spent" is a question about the pipeline; asking it
    # against the source's lightness is not answerable, since a render that goes
    # darker to hold colour then scores above 1 and the number stops meaning
    # anything (it read 2.47 that way).
    got_c = np.hypot(yn_lab[..., 1], yn_lab[..., 2])
    reachable = lookup_ceiling(model["ceiling"], yn_lab)
    usable = reachable > 1.0
    out["chroma_use"] = (
        float(got_c[usable].mean() / reachable[usable].mean()) if usable.sum() >= 50 else 0.0
    )
    # And separately: chroma held against what the picture asked for. Below 1
    # means duller than the source, which is the panel's usual failure.
    #
    # Omitted entirely for a source with no chroma to hold. On a greyscale
    # photograph the denominator goes to zero and the ratio explodes — a search
    # scored one such image at +490 against +0.7..4.5 for everything else, and
    # would have spent the whole optimisation chasing that one division. Absent
    # is the honest answer: the question does not apply. Consumers already drop
    # metrics that are missing from some images.
    src_c_all = np.hypot(src_lab[..., 1], src_lab[..., 2])
    if src_c_all.mean() >= 2.0:
        out["chroma_vs_source"] = float(got_c.mean() / src_c_all.mean())

    # Full-resolution artifact metrics: a different and cruder view of colour (no
    # spatial integration), but it carries the dither artifact detectors that the
    # block model averages away.
    out.update(ink_metrics(reference, idx_visual, display))
    # Named after the complaints rather than after the colour space; see there
    # for why the general metrics above needed the company.
    out.update(complaint_metrics(reference, src_lab, yn_lab))
    return out


_BANK_VERSION: str | None = None


def bank_version() -> str:
    """Fingerprint of the code that produces metrics, for the cache key.

    A cached row is only valid for the metric set that produced it. Without this
    the cache happily serves rows computed before a metric was added, changed or
    fixed — which has now bitten twice: once when a chroma metric's guard was
    corrected, and once when face-region metrics were added and the cache
    silently handed back the previous set while reporting success.

    Hashing the source of the functions that build a metric dict means any edit
    to them invalidates the cache automatically, rather than depending on
    somebody remembering to purge it.
    """
    global _BANK_VERSION
    if _BANK_VERSION is None:
        import inspect  # noqa: PLC0415 — only needed once

        # evaluate and source_canvas are in here because they decide WHAT is
        # measured: loading without EXIF rotation once measured sideways photos,
        # and a cache blind to that fix would keep serving them.
        src = "".join(
            inspect.getsource(fn)
            for fn in (
                measure,
                complaint_metrics,
                _gradient,
                ink_metrics,
                _region_masks,
                _pair_stats,
                reference_for,
                evaluate,
                source_canvas,
                face_boxes_on_canvas,
            )
        )
        _BANK_VERSION = hashlib.sha256(src.encode()).hexdigest()[:8]
    return _BANK_VERSION


# ── cache ────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bank (
    img TEXT NOT NULL, slug TEXT NOT NULL, div INTEGER NOT NULL,
    metrics TEXT NOT NULL, PRIMARY KEY (img, slug, div)
)
"""


def open_cache(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(_SCHEMA)
    conn.commit()
    return conn


def image_key(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def cache_key(slug: str, model_id: str, crop: float = 0.0) -> str:
    """The stored slug: config, metric code, display, and the crop decision.

    ``model_id`` belongs here because the correction LUT is a property of the
    *display*, not of the ImageConfig — variant displays are how every LUT arm in
    this project is rendered (`ab_session._variant_display`). Without it, a
    gamut-mapped arm and the baseline share `(image, config, div)` and the cache
    hands back whichever was measured first, silently, reporting success. That
    would have poisoned any fit over the rated LUT arms, which are most of them.

    ``crop`` is the classifier's `crop_to_fill_threshold`, which decides whether
    the picture is fitted or cropped to fill and so changes both the render and
    the reference it is measured against.
    """
    return f"{slug}@{model_id}@{crop:g}@{bank_version()}"


def cache_get(conn, img: str, slug: str, div: int, model_id: str, crop: float = 0.0) -> dict | None:
    row = conn.execute(
        "SELECT metrics FROM bank WHERE img=? AND slug=? AND div=?",
        (img, cache_key(slug, model_id, crop), div),
    ).fetchone()
    return json.loads(row[0]) if row else None


def cache_put(
    conn, img: str, slug: str, div: int, model_id: str, metrics: dict, crop: float = 0.0
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO bank VALUES (?,?,?,?)",
        (img, cache_key(slug, model_id, crop), div, json.dumps(metrics)),
    )


# ── one-shot evaluation ──────────────────────────────────────────────────────


def evaluate(
    image: Path,
    cfg: ImageConfig,
    model_id: str = "huessen_epf1301",
    div: int = 1,
    seed: int | None = -1,
    crop_to_fill_threshold: float = 0.0,
) -> dict[str, float]:
    """Render *cfg* on *image* and measure it. The unit of work for the search.

    ``seed=-1`` (the default) derives a stable seed from the pair, so the result
    is reproducible and cacheable. Pass an explicit integer to sample a different
    draw of the dither noise, or ``None`` for production's unseeded behaviour.

    ``crop_to_fill_threshold`` is the classifier decision's, and must be the same
    one production would use for this picture: it changes the canvas, so the
    render and the reference have to agree on it or the metrics measure a
    misregistration. The live server's value is 0.14.
    """
    display = DISPLAY_REGISTRY[model_id]
    model = load_model(model_id)
    block = max(1, BLOCK // div)
    reference = reference_for(image, display, model, block, div, crop_to_fill_threshold)
    use = render_seed(image, cfg) if seed == -1 else seed
    # Production's loader: EXIF rotation and the server's pre-shrink included.
    with open_image_for_render(Path(image)) as img:
        idx = render(
            display, img, cfg, div=div, seed=use, crop_to_fill_threshold=crop_to_fill_threshold
        )
    return measure(reference, idx, display, model, block=block)


# ── parallel evaluation ──────────────────────────────────────────────────────


def _worker_init(model_id: str) -> None:
    """Pay the per-process costs once: campaign fit, gamut sampling, numba JIT.

    Roughly 5 s a worker, against ~1.3 s per render afterwards, so a pool is only
    worth starting for jobs in the dozens or more.
    """
    load_model(model_id)
    renderer_for(DISPLAY_REGISTRY[model_id])


def _worker(job: tuple) -> tuple[str, str, dict]:
    image, cfg, model_id, div, crop = job
    try:
        return (
            str(image),
            cfg.cache_slug(),
            evaluate(Path(image), cfg, model_id, div, crop_to_fill_threshold=crop),
        )
    except Exception as exc:  # one bad config must not kill a whole sweep
        return str(image), cfg.cache_slug(), {"error": f"{type(exc).__name__}: {exc}"}


_POOL = None
_POOL_KEY: tuple | None = None
DEFAULT_WORKERS = 12


def get_pool(model_id: str, workers: int) -> Any:
    """One pool, reused across batches.

    Worker startup is expensive and fragile here for the same reason: importing
    anything from `hokku.webserver` runs that package's __init__, which pulls in
    the whole Flask app and numba. Twenty-four processes doing that at once also
    race on loading llvmlite.dll, which fails outright often enough to matter.
    Paying it once per session instead of once per batch fixes both the cost and
    most of the flakiness.
    """
    global _POOL, _POOL_KEY
    import multiprocessing as mp  # noqa: PLC0415 — only the pool path needs it

    key = (model_id, workers)
    if _POOL_KEY != key:
        close_pool()
        _POOL = mp.Pool(workers, initializer=_worker_init, initargs=(model_id,))
        _POOL_KEY = key
    return _POOL


def close_pool() -> None:
    global _POOL, _POOL_KEY
    if _POOL is not None:
        _POOL.terminate()
        _POOL.join()
    _POOL, _POOL_KEY = None, None


atexit.register(close_pool)


def evaluate_many(
    jobs: list[tuple[Path, ImageConfig]],
    model_id: str = "huessen_epf1301",
    workers: int | None = None,
    cache_path: Path | None = None,
    div: int = 1,
    verbose: bool = True,
    crop_to_fill_threshold: float = 0.0,
) -> list[dict[str, float]]:
    """Measure many (image, config) pairs across a process pool, with caching.

    Only the parent touches SQLite. Workers are pure functions returning dicts,
    which keeps the database single-writer and avoids the lock contention that
    makes concurrent SQLite writes a bad idea from a pool this size.

    Anything a worker failed on is retried once in the parent, because the
    failures seen here are transient (a worker losing the numba DLL race) rather
    than a property of the job — re-running the same job in a process that is
    known to work usually succeeds, and silently dropping it would put a hole in
    the search.
    """
    import time  # noqa: PLC0415

    conn = open_cache(cache_path) if cache_path else None
    keys = [(image_key(Path(img)), cfg.cache_slug()) for img, cfg in jobs]
    results: list[dict | None] = [
        cache_get(conn, ik, slug, div, model_id, crop_to_fill_threshold) if conn else None
        for ik, slug in keys
    ]
    todo = [i for i, r in enumerate(results) if r is None]
    if verbose:
        print(f"  {len(jobs)} jobs, {len(jobs) - len(todo)} cached, {len(todo)} to render")

    def remember(i: int, metrics: dict) -> None:
        results[i] = metrics
        if conn is not None and "error" not in metrics and metrics:
            cache_put(conn, keys[i][0], keys[i][1], div, model_id, metrics, crop_to_fill_threshold)

    if todo:
        n_workers = workers or DEFAULT_WORKERS
        started = time.time()
        # Dispatch image-major. The per-image reference cache holds two entries,
        # so a caller that emits jobs grouped by knob (every image once, then
        # every image again) makes every single job rebuild the source canvas,
        # its Lab conversion and its masks. Measured at 0.6 renders/s against
        # ~1.3 s of actual render. Sorting here fixes it for every caller rather
        # than asking each one to remember.
        todo = sorted(todo, key=lambda i: str(jobs[i][0]))
        payload = [
            (str(jobs[i][0]), jobs[i][1], model_id, div, crop_to_fill_threshold) for i in todo
        ]
        pool = get_pool(model_id, n_workers)
        for done, (i, (_img, _slug, metrics)) in enumerate(
            zip(todo, pool.imap(_worker, payload, chunksize=4), strict=True), 1
        ):
            remember(i, metrics)
            if verbose and (done % 50 == 0 or done == len(todo)):
                rate = done / max(time.time() - started, 1e-9)
                print(f"    {done}/{len(todo)}  {rate:.1f} renders/s", flush=True)

        retry = [i for i in todo if (results[i] or {}).get("error") is not None]
        if retry:
            if verbose:
                print(f"    retrying {len(retry)} failed job(s) in-process")
            for i in retry:
                try:
                    remember(
                        i,
                        evaluate(
                            jobs[i][0],
                            jobs[i][1],
                            model_id,
                            div,
                            crop_to_fill_threshold=crop_to_fill_threshold,
                        ),
                    )
                except Exception as exc:
                    results[i] = {"error": f"{type(exc).__name__}: {exc}"}
        if conn is not None:
            conn.commit()
    if conn is not None:
        conn.close()
    return [r or {} for r in results]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--image", type=Path, required=True)
    ap.add_argument("--preset", default="default_general")
    ap.add_argument("--div", type=int, default=1)
    args = ap.parse_args(argv)

    from hokku.webserver.presets import PRESET_IMAGE_CONFIGS  # noqa: PLC0415 — CLI only

    cfg = PRESET_IMAGE_CONFIGS[args.preset]
    metrics = evaluate(args.image, cfg, args.model, args.div)
    for key in sorted(metrics):
        print(f"  {key:28s} {metrics[key]:+9.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
