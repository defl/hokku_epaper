#!/usr/bin/env python3
"""Draw the comparison figures from measurements already on disk.

Everything here reads what ``cam_compare`` and ``gamut_clamp`` already produced.
It renders nothing new and touches no panel, so the pictures cannot disagree
with the numbers they illustrate.

Plots are drawn with OpenCV primitives because matplotlib is not installed in
this environment, and adding it for two charts is a worse trade than fifty lines
of line-drawing.

    python tools/cam_figures.py --outdir build/camcal
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from cam_compare import source_canvas
from color_model import load_records
from color_validate_photos import lab_img, srgb_img_to_xyz
from gamut_clamp import chroma_ceiling, reachable_lab
from hokku.screens.registry import DISPLAY_REGISTRY

FONT = cv2.FONT_HERSHEY_SIMPLEX
INK = (25, 25, 25)
PAPER = 250
ARMS = (
    ("none", "no corrections"),
    ("drc+lut+palette", "all corrections"),
    ("palette+nblack", "palette + neutral black"),
)


def _label(img: np.ndarray, text: str, height: int = 40, scale: float = 0.6) -> np.ndarray:
    bar = np.full((height, img.shape[1], 3), PAPER, np.uint8)
    cv2.putText(bar, text, (10, height - 12), FONT, scale, INK, 2, cv2.LINE_AA)
    return np.vstack([bar, img])


def _row(tiles: list[tuple[str, np.ndarray]], width: int) -> np.ndarray:
    panels = []
    for text, im in tiles:
        h = int(width * im.shape[0] / im.shape[1])
        scaled = cv2.resize(im, (width, h), interpolation=cv2.INTER_AREA)
        panels.append(np.pad(_label(scaled, text), ((0, 0), (5, 5), (0, 0)), constant_values=PAPER))
    return np.hstack(panels)


def _window(mask: np.ndarray, size: tuple[int, int], shape: tuple[int, int]) -> tuple[int, ...]:
    """A crop box of `size` centred on `mask`'s centre of mass, clipped to `shape`.

    Regions are found from the SOURCE image rather than hand-typed coordinates,
    so the crops still land on the right thing if the test image ever changes.
    """
    ys, xs = np.nonzero(mask)
    cy, cx = (int(ys.mean()), int(xs.mean())) if len(ys) else (shape[0] // 2, shape[1] // 2)
    w, h = size
    x0 = int(np.clip(cx - w // 2, 0, shape[1] - w))
    y0 = int(np.clip(cy - h // 2, 0, shape[0] - h))
    return x0, y0, x0 + w, y0 + h


def tone_chart(rows, image: str, black_l: float, white_l: float, arms=ARMS) -> np.ndarray:
    """Source L* against measured panel L*, one line per arm."""
    w, h, pad = 760, 560, 70
    canvas = np.full((h, w, 3), PAPER, np.uint8)
    px = lambda v: int(pad + v / 100.0 * (w - 2 * pad))  # noqa: E731
    py = lambda v: int(h - pad - v / 100.0 * (h - 2 * pad))  # noqa: E731

    for v in range(0, 101, 20):
        cv2.line(canvas, (px(v), py(0)), (px(v), py(100)), (222, 222, 222), 1)
        cv2.line(canvas, (px(0), py(v)), (px(100), py(v)), (222, 222, 222), 1)
        cv2.putText(
            canvas, str(v), (px(v) - 10, py(0) + 22), FONT, 0.45, (110, 110, 110), 1, cv2.LINE_AA
        )
        cv2.putText(
            canvas, str(v), (px(0) - 34, py(v) + 5), FONT, 0.45, (110, 110, 110), 1, cv2.LINE_AA
        )

    for v in np.arange(0, 100, 6):  # y = x, dashed: perfect reproduction
        cv2.line(canvas, (px(v), py(v)), (px(v + 3), py(v + 3)), (175, 175, 175), 1, cv2.LINE_AA)

    for lim, name in ((black_l, "panel black"), (white_l, "panel white")):
        cv2.line(canvas, (px(0), py(lim)), (px(100), py(lim)), (150, 190, 150), 1, cv2.LINE_AA)
        cv2.putText(canvas, name, (px(72), py(lim) - 7), FONT, 0.42, (90, 150, 90), 1, cv2.LINE_AA)

    colours = [(60, 60, 200), (200, 130, 40), (40, 150, 60)]
    for (arm, _name), colour in zip(arms, colours):  # _label is the module helper
        row = next((r for r in rows if r["image"].startswith(image) and r["arm"] == arm), None)
        if row is None:
            continue
        pts = [(px(s), py(g)) for _lo, _hi, s, g, _n in row["tone"]]
        for a, b in zip(pts, pts[1:]):
            cv2.line(canvas, a, b, colour, 3, cv2.LINE_AA)
        for p in pts:
            cv2.circle(canvas, p, 5, colour, -1, cv2.LINE_AA)

    for i, ((_arm, label), colour) in enumerate(zip(arms, colours)):
        y = pad + 18 + i * 26
        cv2.line(canvas, (px(4), y), (px(11), y), colour, 3, cv2.LINE_AA)
        cv2.putText(canvas, label, (px(13), y + 5), FONT, 0.5, INK, 1, cv2.LINE_AA)

    cv2.putText(canvas, "source L*", (w // 2 - 45, h - 18), FONT, 0.52, INK, 1, cv2.LINE_AA)
    canvas = _label(canvas, "Tone transfer, measured on glass", 44, 0.66)
    return canvas


def gamut_chart(ceiling: np.ndarray, hue_deg: float = 30.0) -> np.ndarray:
    """Reachable chroma against lightness at one hue — why a constant-L clamp fails."""
    w, h, pad = 760, 560, 70
    canvas = np.full((h, w, 3), PAPER, np.uint8)
    l_max, c_max = 80.0, 80.0
    px = lambda v: int(pad + v / l_max * (w - 2 * pad))  # noqa: E731
    py = lambda v: int(h - pad - v / c_max * (h - 2 * pad))  # noqa: E731

    for v in range(0, int(l_max) + 1, 20):
        cv2.line(canvas, (px(v), py(0)), (px(v), py(c_max)), (222, 222, 222), 1)
        cv2.putText(
            canvas, str(v), (px(v) - 10, py(0) + 22), FONT, 0.45, (110, 110, 110), 1, cv2.LINE_AA
        )
    for v in range(0, int(c_max) + 1, 20):
        cv2.line(canvas, (px(0), py(v)), (px(l_max), py(v)), (222, 222, 222), 1)
        cv2.putText(
            canvas, str(v), (px(0) - 34, py(v) + 5), FONT, 0.45, (110, 110, 110), 1, cv2.LINE_AA
        )

    hue_bin = int(hue_deg / 360.0 * ceiling.shape[1]) % ceiling.shape[1]
    l_bins = np.linspace(0, 100, ceiling.shape[0])
    pts = [(px(lv), py(c)) for lv, c in zip(l_bins, ceiling[:, hue_bin]) if lv <= l_max]
    poly = np.array([(px(0), py(0)), *pts, (px(l_bins[-1]), py(0))], np.int32)
    overlay = canvas.copy()
    cv2.fillPoly(overlay, [poly], (215, 235, 245))
    canvas = cv2.addWeighted(overlay, 0.9, canvas, 0.1, 0)
    for a, b in zip(pts, pts[1:]):
        cv2.line(canvas, a, b, (150, 120, 40), 3, cv2.LINE_AA)

    marks = [
        ((43.4, 61.4), (40, 40, 210), "source asks: L* 43, C 61"),
        ((43.4, 18.0), (150, 60, 190), "constant-L clamp: C 18"),
        ((28.7, 30.7), (40, 150, 60), "what the dither does: L* 29, C 31"),
        ((24.0, 42.8), (90, 90, 90), "pure red ink: L* 24, C 43"),
    ]
    for (lv, cv_), colour, text in marks:
        cv2.drawMarker(canvas, (px(lv), py(cv_)), colour, cv2.MARKER_CROSS, 20, 3, cv2.LINE_AA)
        cv2.circle(canvas, (px(lv), py(cv_)), 9, colour, 2, cv2.LINE_AA)
        cv2.putText(canvas, text, (px(lv) + 16, py(cv_) + 5), FONT, 0.47, colour, 1, cv2.LINE_AA)

    cv2.putText(canvas, "reachable", (px(30), py(8)), FONT, 0.55, (120, 95, 30), 2, cv2.LINE_AA)
    cv2.putText(canvas, "L*", (w // 2 - 10, h - 18), FONT, 0.52, INK, 1, cv2.LINE_AA)
    cv2.putText(canvas, "chroma", (14, pad - 24), FONT, 0.52, INK, 1, cv2.LINE_AA)
    return _label(
        canvas, f"What the panel can reach at hue {hue_deg:.0f} deg (red-orange)", 44, 0.66
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--outdir", type=Path, default=Path("build/camcal"))
    ap.add_argument(
        "--image",
        type=Path,
        default=Path("images/test/Wayuu_woman_with_sad_face_in_the_market_buying.jpg"),
    )
    ap.add_argument(
        "--arms",
        default=None,
        help="comma-separated arm labels to show, as they appear in comparison.json",
    )
    ap.add_argument("--prefix", default="fig", help="output filename prefix")
    args = ap.parse_args(argv)

    arms = ARMS
    if args.arms:
        pretty = {"none": "no corrections", "drc+lut+palette": "all corrections"}
        arms = tuple((a, pretty.get(a, a)) for a in args.arms.split(","))

    display = DISPLAY_REGISTRY[args.model]
    stem = args.image.stem[:24]
    src = source_canvas(args.image, display, "default_general")
    tiles = [("source image", src)]
    for arm, label in arms:
        path = args.outdir / f"{stem}__{arm}__measured.png"
        img = cv2.imread(str(path))
        if img is None:
            print(f"missing {path} — run cam_compare for arm {arm}")
            return 1
        tiles.append((str(label), cv2.cvtColor(img, cv2.COLOR_BGR2RGB)))

    # Full frame, cropped to the region every capture covers.
    x0, y0, x1, y1 = 140, 60, 1530, 1080
    full = _row([(t, im[y0:y1, x0:x1]) for t, im in tiles], 620)
    cv2.imwrite(str(args.outdir / f"{args.prefix}_full.png"), cv2.cvtColor(full, cv2.COLOR_RGB2BGR))

    slab = lab_img(srgb_img_to_xyz(src))
    chroma = np.hypot(slab[..., 1], slab[..., 2])
    hue = np.degrees(np.arctan2(slab[..., 2], slab[..., 1]))
    regions = {
        "shadows": slab[..., 0] < 10,
        "red garment": (chroma > 35) & (hue > -20) & (hue < 40),
        "skin": (hue > -40) & (hue < 70) & (chroma > 12) & (slab[..., 0] > 30),
    }
    details = []
    for name, mask in regions.items():
        bx0, by0, bx1, by1 = _window(mask, (460, 380), slab.shape[:2])
        row = _row([(f"{name}: {t}", im[by0:by1, bx0:bx1]) for t, im in tiles], 400)
        details.append(row)
    detail = np.vstack(
        [np.pad(r, ((6, 6), (0, 0), (0, 0)), constant_values=PAPER) for r in details]
    )
    cv2.imwrite(
        str(args.outdir / f"{args.prefix}_detail.png"), cv2.cvtColor(detail, cv2.COLOR_RGB2BGR)
    )

    rows = json.loads((args.outdir / "comparison.json").read_text(encoding="utf-8"))
    records = load_records(Path("docs/screens") / args.model / "measurements/data/campaign.jsonl")
    labs = reachable_lab(records)
    tone = tone_chart(rows, args.image.name[:16], 10.86, 66.94, arms)
    gamut = gamut_chart(chroma_ceiling(labs))
    charts = np.hstack(
        [np.pad(c, ((0, 0), (6, 6), (0, 0)), constant_values=PAPER) for c in (tone, gamut)]
    )
    cv2.imwrite(str(args.outdir / f"{args.prefix}_charts.png"), charts)

    for name in (
        f"{args.prefix}_full.png",
        f"{args.prefix}_detail.png",
        f"{args.prefix}_charts.png",
    ):
        print(f"  -> {args.outdir / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
