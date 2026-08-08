#!/usr/bin/env python3
"""Generate a colour-calibration target for a Hokku panel.

The target is a grid of large flat patches designed to be metered one at a
time with a colorimeter or spectrophotometer:

  * **Ink anchors** — one patch per palette entry, 100 % of a single ink.
    Metering these replaces the guessed/borrowed ``palette_measured_rgb``
    arrays in ``hokku/screens/*/display.py`` with values measured on our own
    glass.
  * **Tone-response ramp** — Bayer-dithered black/white patches at exactly
    k/8 black-ink area coverage for k = 1..7.  The dither pipeline assumes
    palette inks mix *linearly* by area; metering the ramp shows whether the
    real panel agrees (print media famously does not — dot gain).  Any
    deviation is a systematic mid-tone error in every rendered image.
  * **Repeat patches** — white and black appear twice, at opposite ends of
    the measurement order, as a drift check on the illumination and on
    meter placement repeatability.

Two outputs matter:

  ``<stem>.png``   Upload this to the server and pin it to the screen.  It is
                   authored at exact panel-canvas dimensions and painted in
                   the display's own ``palette_measured_rgb``, so the render
                   pipeline reproduces the intended ink per pixel with no
                   resampling and no dither speckle — provided it is rendered
                   with the ``calibration_raw`` preset.  ``test_color_target``
                   asserts exactly that, so a pipeline change that would
                   corrupt the target turns a test red rather than silently
                   producing bad measurements.

  ``<stem>.json``  The patch manifest: measurement order, what each patch is,
                   and where it sits in pixels and millimetres.  ``color_read``
                   consumes this to prompt for readings in order and to do the
                   normalisation.

``<stem>.bin`` (the packed panel bytes) is also written, for offline
inspection with ``screen_sim.py --file`` and as the reference the test
compares against.

Usage:
    # Default: the Bigme F7
    python tools/color_target.py

    # Another model, custom output location
    python tools/color_target.py --model huessen_epf1301 --out build/cal

    # Fewer, larger patches (skip the tone ramp — anchors only)
    python tools/color_target.py --no-ramp
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from PIL import Image

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from hokku.screens.registry import DISPLAY_REGISTRY

# Physical panel dimensions, needed only to report patch sizes and centres in
# millimetres so the operator can sanity-check that a patch is comfortably
# larger than the meter's aperture. Derived from the panel diagonal and the
# pixel aspect ratio; absent here just means the manifest omits the mm fields.
PANEL_DIAGONAL_INCH: dict[str, float] = {
    "bigme_f7": 7.3,
    "huessen_epf1301": 13.3,
    "seeedstudio_e1004": 13.3,
}

# Standard 8×8 ordered (Bayer) threshold matrix. Values 0..63.
BAYER_8 = np.array(
    [
        [0, 32, 8, 40, 2, 34, 10, 42],
        [48, 16, 56, 24, 50, 18, 58, 26],
        [12, 44, 4, 36, 14, 46, 6, 38],
        [60, 28, 52, 20, 62, 30, 54, 22],
        [3, 35, 11, 43, 1, 33, 9, 41],
        [51, 19, 59, 27, 49, 17, 57, 25],
        [15, 47, 7, 39, 13, 45, 5, 37],
        [63, 31, 55, 23, 61, 29, 53, 21],
    ],
    dtype=np.uint8,
)

BLACK_IDX = 0
WHITE_IDX = 1

INK_NAMES = ("black", "white", "yellow", "red", "blue", "green")

# Ramp levels as eighths of black-ink area coverage. 0/8 and 8/8 are omitted:
# they are the black and white anchors, already measured as flat patches.
RAMP_EIGHTHS = (1, 2, 3, 4, 5, 6, 7)


@dataclass(frozen=True)
class Patch:
    """One measurable patch. Coordinates are canvas pixels, origin top-left."""

    order: int  # 1-based measurement sequence
    name: str  # human label, e.g. "yellow" or "ramp 3/8"
    kind: str  # "ink" | "ramp"
    ink_index: int | None  # palette index, for kind == "ink"
    black_fraction: float  # intended black-ink area coverage, 0.0 .. 1.0
    row: int
    col: int
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


def _panel_mm(model: str, panel_w: int, panel_h: int) -> tuple[float, float] | None:
    """Physical panel size in mm, or None when the diagonal isn't recorded."""
    diagonal_in = PANEL_DIAGONAL_INCH.get(model)
    if diagonal_in is None:
        return None
    diagonal_mm = diagonal_in * 25.4
    aspect_diag = (panel_w**2 + panel_h**2) ** 0.5
    return diagonal_mm * panel_w / aspect_diag, diagonal_mm * panel_h / aspect_diag


def plan_patches(
    *,
    n_ink: int,
    include_ramp: bool,
    panel_w: int,
    panel_h: int,
    cols: int,
    rows: int,
    min_gutter: int,
) -> list[Patch]:
    """Lay out the measurement patches on a cols×rows grid.

    Patch side is forced to a multiple of 8 so the Bayer tile divides it
    exactly — otherwise a partial tile at the patch edge would skew the
    black-area fraction away from the intended k/8 and the whole point of
    the ramp (a *known* coverage) would be lost.
    """
    ramp = RAMP_EIGHTHS if include_ramp else ()
    # Two repeats (white, black) close the sequence as a drift check.
    n_patches = n_ink + len(ramp) + 2
    if n_patches > cols * rows:
        raise ValueError(f"{n_patches} patches do not fit in a {cols}×{rows} grid")

    cell_w, cell_h = panel_w // cols, panel_h // rows
    side = min(cell_w, cell_h) - 2 * min_gutter
    side -= side % 8
    if side < 8:
        raise ValueError(
            f"Grid {cols}×{rows} on a {panel_w}×{panel_h} panel leaves no room "
            f"for a patch (need >= 8 px after a {min_gutter} px gutter)"
        )

    # Centre the whole grid on the panel rather than the top-left corner, so
    # edge patches keep an even margin from the bezel.
    origin_x = (panel_w - cols * cell_w) // 2
    origin_y = (panel_h - rows * cell_h) // 2

    specs: list[tuple[str, str, int | None, float]] = [
        (
            INK_NAMES[i] if i < len(INK_NAMES) else f"ink{i}",
            "ink",
            i,
            1.0 if i == BLACK_IDX else 0.0,
        )
        for i in range(n_ink)
    ]
    specs += [(f"ramp {k}/8", "ramp", None, k / 8.0) for k in ramp]
    specs += [
        ("white (repeat)", "ink", WHITE_IDX, 0.0),
        ("black (repeat)", "ink", BLACK_IDX, 1.0),
    ]

    patches: list[Patch] = []
    for i, (name, kind, ink_index, black_fraction) in enumerate(specs):
        row, col = divmod(i, cols)
        cx = origin_x + col * cell_w + (cell_w - side) // 2
        cy = origin_y + row * cell_h + (cell_h - side) // 2
        patches.append(
            Patch(
                order=i + 1,
                name=name,
                kind=kind,
                ink_index=ink_index,
                black_fraction=black_fraction,
                row=row,
                col=col,
                x=cx,
                y=cy,
                w=side,
                h=side,
            )
        )
    return patches


def build_index_raster(
    patches: list[Patch], panel_w: int, panel_h: int, frame_px: int = 4
) -> np.ndarray:
    """Paint the patches into a (panel_h, panel_w) palette-index raster.

    Gutters are white ink — the panel's natural "paper". That alone would
    leave the white patches invisible against their surround and therefore
    impossible to aim at, so every patch gets a thin black registration frame
    drawn *outside* its rectangle. The frame is an aiming aid only: it never
    touches the patch interior, so ink purity and the ramp's black-area
    fraction are unaffected.
    """
    idx = np.full((panel_h, panel_w), WHITE_IDX, dtype=np.uint8)

    for p in patches:
        if frame_px <= 0:
            break
        # Clamp so a patch near the bezel cannot push the frame off-panel.
        t = min(frame_px, p.x, p.y, panel_w - (p.x + p.w), panel_h - (p.y + p.h))
        if t <= 0:
            continue
        idx[p.y - t : p.y + p.h + t, p.x - t : p.x + p.w + t] = BLACK_IDX

    for p in patches:
        if p.kind == "ink":
            assert p.ink_index is not None
            idx[p.y : p.y + p.h, p.x : p.x + p.w] = p.ink_index
            continue
        # Ramp: threshold the Bayer tile so exactly round(f*64) of every 64
        # cells land on black ink. With f = k/8 that is exactly 8k cells.
        threshold = round(p.black_fraction * 64)
        tile = np.where(BAYER_8 < threshold, BLACK_IDX, WHITE_IDX).astype(np.uint8)
        block = np.tile(tile, (p.h // 8, p.w // 8))
        idx[p.y : p.y + p.h, p.x : p.x + p.w] = block
    return idx


def fullscreen_ramp_raster(panel_w: int, panel_h: int, eighths: int) -> np.ndarray:
    """A whole-panel Bayer field at exactly *eighths*/8 black coverage.

    The grid target assumes the operator re-aims the meter at each patch. When
    the instrument is clamped to the glass and cannot be moved, the measured
    area is fixed, so each patch has to become its OWN full-screen frame —
    otherwise the meter reads whatever patch happens to sit under it, plus
    gutters, plus registration frames.

    Same thresholded BAYER_8 as build_index_raster, so a level here and the
    corresponding grid patch are the identical pattern at the identical
    coverage — the two routes stay comparable.
    """
    threshold = round((eighths / 8.0) * 64)
    tile = np.where(BAYER_8 < threshold, BLACK_IDX, WHITE_IDX).astype(np.uint8)
    reps_y = -(-panel_h // 8)  # ceil, then crop: panels need not be 8-multiples
    reps_x = -(-panel_w // 8)
    return np.tile(tile, (reps_y, reps_x))[:panel_h, :panel_w]


def fullscreen_sequence(display) -> list[tuple[str, str, float | None, np.ndarray]]:
    """Every measurement field as a full-panel raster: (name, kind, coverage, idx).

    Six flat inks then the seven ramp levels. Black and white appear once, as
    inks — ramp 0/8 and 8/8 would duplicate them exactly.
    """
    h, w = display.panel_h, display.panel_w
    n_ink = int(display.palette_measured_rgb.shape[0])
    out: list[tuple[str, str, float | None, np.ndarray]] = [
        (INK_NAMES[i], "ink", None, np.full((h, w), i, dtype=np.uint8)) for i in range(n_ink)
    ]
    out += [(f"ramp {k}/8", "ramp", k / 8.0, fullscreen_ramp_raster(w, h, k)) for k in RAMP_EIGHTHS]
    return out


def actual_black_fraction(idx: np.ndarray, patch: Patch) -> float:
    """Measured-from-the-raster black coverage — what the panel will really show."""
    region = idx[patch.y : patch.y + patch.h, patch.x : patch.x + patch.w]
    return float(np.count_nonzero(region == BLACK_IDX) / region.size)


def index_raster_to_png(idx: np.ndarray, display) -> Image.Image:
    """Palette indices → an RGB image painted in the display's measured inks.

    Painting in ``palette_measured_rgb`` (rather than the punchy preview
    palette) is what makes the round-trip exact: every pixel already sits on
    a palette anchor, so nearest-ink quantisation returns the ink it came
    from.
    """
    rgb = np.rint(display.palette_measured_rgb).clip(0, 255).astype(np.uint8)[idx]
    return Image.fromarray(rgb, mode="RGB")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--model", default="bigme_f7", choices=sorted(DISPLAY_REGISTRY), help="screen model"
    )
    ap.add_argument("--out", default="build/colorcal", help="output directory")
    ap.add_argument("--cols", type=int, default=0, help="grid columns (0 = auto)")
    ap.add_argument("--rows", type=int, default=0, help="grid rows (0 = auto)")
    ap.add_argument("--min-gutter", type=int, default=8, help="minimum gap between patches, px")
    ap.add_argument("--no-ramp", action="store_true", help="ink anchors only, no tone ramp")
    args = ap.parse_args(argv)

    display = DISPLAY_REGISTRY[args.model]
    panel_w, panel_h = display.panel_w, display.panel_h
    n_ink = int(display.palette_measured_rgb.shape[0])

    # Auto grid: 5×3 suits a 5:3 panel and yields 15 cells for 15 patches.
    cols = args.cols or 5
    rows = args.rows or 3

    patches = plan_patches(
        n_ink=n_ink,
        include_ramp=not args.no_ramp,
        panel_w=panel_w,
        panel_h=panel_h,
        cols=cols,
        rows=rows,
        min_gutter=args.min_gutter,
    )
    idx = build_index_raster(patches, panel_w, panel_h)
    png = index_raster_to_png(idx, display)
    panel_bytes = display.indices_to_panel_bytes(idx)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / f"colorcal_{args.model}"
    png.save(stem.with_suffix(".png"))
    stem.with_suffix(".bin").write_bytes(panel_bytes)

    mm = _panel_mm(args.model, panel_w, panel_h)
    px_mm = mm[0] / panel_w if mm else None
    manifest = {
        "model": args.model,
        "panel_w": panel_w,
        "panel_h": panel_h,
        "panel_mm": [round(mm[0], 1), round(mm[1], 1)] if mm else None,
        "px_per_mm": round(1 / px_mm, 2) if px_mm else None,
        "ink_names": list(INK_NAMES[:n_ink]),
        "palette_measured_rgb": display.palette_measured_rgb.tolist(),
        "patches": [
            {
                **asdict(p),
                # The intended fraction is the design; the actual is what the
                # raster really contains. They should agree exactly — if a
                # future layout change breaks that, the reading script uses
                # the actual value and the discrepancy is visible here.
                "actual_black_fraction": round(actual_black_fraction(idx, p), 6),
                "center_px": list(p.center),
                "patch_mm": round(p.w * px_mm, 1) if px_mm else None,
            }
            for p in patches
        ],
    }
    stem.with_suffix(".json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"Model         : {args.model} ({panel_w}×{panel_h})")
    if mm:
        print(f"Panel size    : {mm[0]:.0f} × {mm[1]:.0f} mm")
    side = patches[0].w
    print(f"Patches       : {len(patches)} on a {cols}×{rows} grid, {side} px square", end="")
    print(f" ({side * px_mm:.0f} mm)" if px_mm else "")
    print(f"Wrote         : {stem.with_suffix('.png')}")
    print(f"                {stem.with_suffix('.json')}")
    print(f"                {stem.with_suffix('.bin')}")
    print()
    print("Next: upload the .png, set the screen's preset to `calibration_raw`,")
    print("pin it with 'show next', then run tools/color_read.py against the .json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
