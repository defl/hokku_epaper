#!/usr/bin/env python3
"""Calibrate the camera rig's colour against the panel.

A photograph is not a measurement. Between the ink and a number sit the bench
lamp, the sensor's own spectral sensitivities, and — if the JPEG is used — white
balance, a colour matrix and a tone curve. Capture is therefore RAW, so the last
three are simply absent rather than modelled.

What remains is fittable. Show the panel patches whose CIELAB a colorimeter
already measured during the campaign, photograph them, and solve for the mapping
from one to the other. Afterwards any photograph of this panel can be read as
"what the ColorMunki would have said", under D65, which is the only form in which
a photograph can be compared against a source image.

Three frames are captured, not one: white and black as well as the chart. The
panel sits in a light tent, so a veil of reflected wall is added to everything,
and an additive term cannot be divided out by a flat field. Two frames of known
reflectance separate illumination from glare — see cam_rig.photometric_fields.

The fit is valid for exactly the lighting, geometry and capture settings it was
built under, so it records them and should be rebuilt whenever the lamp moves.

    python tools/cam_calibrate.py colour --port COM4 --bench-flip180
    python tools/cam_calibrate.py colour --reuse-shot     # re-solve, no panel time
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

import cam_rig
from color_model import INK_NAMES, delta_e76, lab, load_records, primaries
from dng import summarise as dng_summarise
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_config import DitherConfig
from hokku.webserver.dither_streaming import dither
from panel_arms import to_panel
from send_frame import open_device, send_frame

DEFAULT_DIR = Path("build/camcal")

# Patches are laid out as a grid because the rig sees only the middle of the
# panel: a full-panel patch would cost one 19-second refresh per colour, and the
# campaign has 1505 of them. Cropping a tile out of the dither of a flat field is
# the same measurement — error diffusion on a uniform request reaches the same
# steady state everywhere — so a grid buys dozens of patches per refresh.
CHART_COLS = 12
CHART_ROWS = 10
SAMPLE_FRACTION = 0.6
DITHER_MARGIN = 48  # dithered lead-in cropped away, so every tile is steady-state
CHART_SEED = 20260905
MIN_FIT_PATCHES = 15
MARKER_MIN, MARKER_MAX = 6, 14  # registration dots, small enough to fit a cell margin


def push_visual(vis: np.ndarray, display, port: str, flip180: bool, timeout: float) -> bool:
    """Send a VISUAL-orientation index raster to the panel."""
    panel = to_panel(vis, display)
    if flip180:
        panel = np.rot90(panel, k=2)
    data = display.indices_to_panel_bytes(panel)
    print(f"opening {port} ({display.model_id})...", flush=True)
    s = open_device(port, display.model_id, timeout_s=timeout, interactive=False)
    if s is None:
        return False
    try:
        return send_frame(s, data, "calibration chart")
    finally:
        s.close()


def _pipeline_patches(records: list[dict]) -> list[dict]:
    """One entry per distinct (rgb, config) request, with the median measured XYZ."""
    groups: dict[tuple, list[dict]] = {}
    for r in records:
        if r.get("source") != "pipeline" or not r.get("rgb") or not r.get("config"):
            continue
        groups.setdefault((tuple(r["rgb"]), r.get("config_name")), []).append(r)
    out = []
    for (rgb, _cfg), rs in groups.items():
        xyz = np.median(np.array([r["xyz_d65_pct"] for r in rs], dtype=float), axis=0)
        out.append(
            {
                "kind": "pipeline",
                "rgb": list(rgb),
                "config": rs[0]["config"],
                "xyz": xyz.tolist(),
                "label": f"rgb{tuple(rgb)}",
            }
        )
    return out


def _ink_patches(records: list[dict]) -> list[dict]:
    prim = primaries(records)
    return [
        {
            "kind": "ink",
            "ink_index": i,
            "xyz": [float(v) for v in prim[name]["xyz"]],
            "label": f"ink_{name}",
        }
        for i, name in enumerate(INK_NAMES)
        if name in prim
    ]


WARM_B_STAR = 25.0  # b* above which the fit has consistently been worst


def _farthest_point(
    labs: np.ndarray, seeds: np.ndarray, want: int, pool_idx: np.ndarray
) -> list[int]:
    """Greedy farthest-point selection of `want` indices out of `pool_idx`."""
    if want <= 0 or len(pool_idx) == 0:
        return []
    sub = labs[pool_idx]
    dist = (
        np.min(np.linalg.norm(sub[:, None, :] - seeds[None, :, :], axis=-1), axis=1)
        if len(seeds)
        else np.full(len(sub), np.inf)
    )
    chosen: list[int] = []
    for _ in range(min(want, len(sub))):
        k = int(np.argmax(dist))
        chosen.append(int(pool_idx[k]))
        dist = np.minimum(dist, np.linalg.norm(sub - sub[k], axis=-1))
    return chosen


def select_patches(records: list[dict], count: int, warm_fraction: float = 0.3) -> list[dict]:
    """The six inks, a spread over the whole gamut, and extra weight on the warm end.

    Farthest-point rather than a regular RGB grid: this panel's gamut is 41 % of
    sRGB and badly anisotropic, so an even grid in request space collapses into a
    few blobs once it lands on glass. The fit needs its patches spread over where
    the RESULTS are, not over where the requests were.

    An even spread is still not what this fit wants, though. Its error is not
    even: patches above b* 25 have run at roughly three times the error of
    everything else, in every configuration tried, including under two different
    lamps and after illuminant retargeting — which points at the sensor's own
    spectral response rather than anything about the light. A polynomial cannot
    be told about that, but it can be given more places to bend, and the warm end
    is also where skin lives, which is what the whole exercise is about.
    """
    inks = _ink_patches(records)
    pool = _pipeline_patches(records)
    if not pool:
        raise SystemExit("no reproducible pipeline patches in the dataset")

    labs = np.array([lab(p["xyz"]) for p in pool])
    seeds = np.array([lab(p["xyz"]) for p in inks]) if inks else labs[:1]
    budget = max(count - len(inks), 0)
    n_warm = round(budget * warm_fraction)

    warm_idx = np.nonzero(labs[:, 2] > WARM_B_STAR)[0]
    warm = _farthest_point(labs, seeds, n_warm, warm_idx)
    rest_seeds = np.vstack([seeds, labs[warm]]) if warm else seeds
    rest = _farthest_point(labs, rest_seeds, budget - len(warm), np.arange(len(pool)))
    return inks + [pool[k] for k in warm + rest]


def _tile(patch: dict, display, tile_w: int, tile_h: int) -> np.ndarray:
    """One cell's palette-index raster, dithered exactly as the campaign's was."""
    if patch["kind"] == "ink":
        return np.full((tile_h, tile_w), patch["ink_index"], dtype=np.uint8)
    m = DITHER_MARGIN
    canvas = np.zeros((tile_h + 2 * m, tile_w + 2 * m, 3), dtype=np.uint8)
    canvas[:, :] = tuple(patch["rgb"])
    return dither(canvas, DitherConfig(**patch["config"]), display)[m : m + tile_h, m : m + tile_w]


def build_chart(
    patches: list[dict],
    display,
    area: tuple[int, int, int, int],
    cols: int = CHART_COLS,
    rows: int = CHART_ROWS,
    dots: int = 3,
) -> tuple[np.ndarray, list[dict]]:
    """(visual index raster, per-cell placement) for as many patches as fit.

    ``area`` is the panel rectangle the grid is packed into. It matters because
    the rig sees roughly the middle half of the panel: spread over the whole
    panel, most cells land outside the view and are thrown away, which is how the
    first attempt ended up fitting 11 coefficients to 30 patches.

    The scattered dots exist for the registration, not the colour. A regular grid
    of flat tiles is the worst possible input to feature matching: every cell
    corner looks like every other one, and a homography shifted by exactly one
    cell fits nearly as well as the correct one. Dots at cell-dependent random
    positions make the constellation globally unique. They stay in the outer
    margin; sampling takes the middle 60 % of each cell and never sees them.
    ``dots`` therefore has to grow as cells get larger and fewer — a six-block
    frame needs an order of magnitude more of them than a 120-cell chart.
    """
    ax0, ay0, ax1, ay1 = area
    cell_w = (ax1 - ax0) // cols
    cell_h = (ay1 - ay0) // rows
    vis = np.full((display.visual_h, display.visual_w), 1, dtype=np.uint8)  # white ink
    rng = np.random.default_rng(CHART_SEED)
    placed = []

    for n, patch in enumerate(patches[: cols * rows]):
        col, row = n % cols, n // cols
        x0, y0 = ax0 + col * cell_w, ay0 + row * cell_h
        vis[y0 : y0 + cell_h, x0 : x0 + cell_w] = _tile(patch, display, cell_w, cell_h)

        sw, sh = int(cell_w * SAMPLE_FRACTION), int(cell_h * SAMPLE_FRACTION)
        sx, sy = x0 + (cell_w - sw) // 2, y0 + (cell_h - sh) // 2
        # Bands, not rejection sampling: with a 60 % sample box the margin is a
        # fifth of a cell on each side, and asking for a random position that
        # misses the box on at least one axis has no solution once cells get
        # small. Choosing the band first always terminates.
        left, top = sx - x0, sy - y0
        bands = {
            "top": (2, cell_w - MARKER_MAX - 2, 2, max(top - MARKER_MAX - 2, 3)),
            "bottom": (2, cell_w - MARKER_MAX - 2, top + sh + 2, cell_h - MARKER_MAX - 2),
            "left": (2, max(left - MARKER_MAX - 2, 3), 2, cell_h - MARKER_MAX - 2),
            "right": (left + sw + 2, cell_w - MARKER_MAX - 2, 2, cell_h - MARKER_MAX - 2),
        }
        names = list(bands)
        for _ in range(dots):
            bx0, bx1, by0, by1 = bands[names[int(rng.integers(len(names)))]]
            if bx1 <= bx0 or by1 <= by0:
                continue  # this cell is too small for a marker on that side
            size = int(rng.integers(MARKER_MIN, MARKER_MAX + 1))
            dx, dy = int(rng.integers(bx0, bx1)), int(rng.integers(by0, by1))
            vis[y0 + dy : y0 + dy + size, x0 + dx : x0 + dx + size] = 0 if rng.random() < 0.5 else 1

        placed.append({**patch, "cell": [col, row], "sample_rect": [sx, sy, sw, sh]})
    return vis, placed


def cmd_exposure(args) -> int:
    """Find the shutter that puts the panel's white just below clipping.

    Exposure has to be set from the brightest thing that will ever be measured,
    which is the panel showing white ink — not from a photograph of a portrait.
    Getting this backwards cost a whole calibration round: a shutter chosen on a
    dark portrait left the white frame 98 % clipped, every bright chart patch
    pinned at 255, and the colour fit stuck at 12 dE for reasons that looked like
    a modelling failure and were nothing of the sort.

    Clipping is unrecoverable and silent. A patch at 255 does not say how far
    past 255 it was, so no amount of later correction can put the information
    back, and the fit simply reports a large residual with no clue as to why.
    """
    display = DISPLAY_REGISTRY[args.model]
    args.outdir.mkdir(parents=True, exist_ok=True)

    if not args.reuse_shot:
        white = np.full((display.visual_h, display.visual_w), 1, dtype=np.uint8)
        if not push_visual(white, display, args.port, args.bench_flip180, args.console_timeout):
            print("upload failed")
            return 1

    print(f"  target: raw p99.9 at {args.target:.2f} of full scale")
    best = None
    for shutter in args.shutters:
        path = args.outdir / f"exposure_{shutter}.jpg"
        shot = cam_rig.capture_linear(
            path, shutter_us=shutter, settle_ms=args.settle, verbose=False
        )
        raw = shot.raw
        peak = [float(np.percentile(raw.rgb[:, :, i], 99.9)) for i in range(3)]
        top = max(peak)
        print(
            f"  {shutter:>7} us  raw p99.9 R{peak[0]:5.3f} G{peak[1]:5.3f} B{peak[2]:5.3f}  "
            f"saturated {100 * raw.saturated_fraction:6.3f}%"
        )
        # A handful of saturated pixels are bezel glints at the frame edge, far
        # outside any chart cell; demanding literally zero rejects usable exposures.
        if top <= args.target and raw.saturated_fraction < 5e-4 and (best is None or top > best[1]):
            best = (shutter, top)

    if best is None:
        print("\n  every shutter tried still clips — add shorter ones to --shutters")
        return 1
    print(f"\n  pick {best[0]} us (brightest channel p99.9 at {best[1]:.2f} of full scale)")
    print(f"  set cam_rig.SHUTTER_US = {best[0]}")
    return 0


def cmd_primaries(args) -> int:
    """Photograph the six inks as large blocks and check them against the meter.

    This is the rig's baseline, and it answers a question no chart of dithered
    mixtures can: what does each INK actually look like on this glass, measured
    the same way a rendered picture is measured.

    That matters because "the reds look weak" has two completely different
    causes with the same appearance. Either the red ink is duller on glass than
    the campaign says, in which case no amount of pipeline work will fix it, or
    the ink is fine and the renderer is simply not laying much of it down, which
    is entirely fixable. Six solid blocks separate those two, and the ink
    coverage report below says which one is happening.

    Reuses the white, black and chart calibration from the last ``colour`` run,
    so it costs a single panel refresh.
    """
    display = DISPLAY_REGISTRY[args.model]
    outdir = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    shot_path = outdir / "primaries_shot.jpg"

    fit_path = outdir / "colour_fit.json"
    if not fit_path.exists():
        print(f"no colour fit at {fit_path} — run `colour` first")
        return 1
    fit = json.loads(fit_path.read_text(encoding="utf-8"))
    coeffs = np.array(fit["coeffs"])
    white_y, black_y = float(fit["white_y"]), float(fit["black_y"])
    floor = fit.get("error_floor", {})
    cam_rig.check_fit_sanity(coeffs, white_y, black_y)

    data = args.data or Path("docs/screens") / args.model / "measurements/data/campaign.jsonl"
    records = load_records(data)
    inks = _ink_patches(records)
    area = tuple(args.area)
    # Six huge flat blocks give a feature detector almost nothing to work with —
    # the first attempt found 20 candidate matches and failed to register. The
    # dot field has to scale with cell size, not stay at the count that suits a
    # 120-cell chart.
    vis, placed = build_chart(inks, display, area, cols=3, rows=2, dots=60)
    expected = np.rint(display.palette_measured_rgb).clip(0, 255).astype(np.uint8)[vis]
    cv2.imwrite(str(outdir / "primaries_expected.png"), cv2.cvtColor(expected, cv2.COLOR_RGB2BGR))

    if not args.reuse_shot:
        if not push_visual(vis, display, args.port, args.bench_flip180, args.console_timeout):
            print("upload failed")
            return 1
        shot = cam_rig.capture_linear(shot_path, settle_ms=args.settle, verbose=False)
        print(f"  primaries frame : {dng_summarise(shot.raw)}")

    white_shot = cam_rig.load_linear(outdir / "flat_white_shot.jpg")
    black_shot = cam_rig.load_linear(outdir / "flat_black_shot.jpg")
    shot = cam_rig.load_linear(shot_path)
    illum, glare = cam_rig.photometric_fields(
        white_shot.raw.rgb, black_shot.raw.rgb, white_y, black_y
    )
    corrected = cam_rig.apply_photometric(shot.raw.rgb, illum, glare)

    homography, _info = cam_rig.content_homography(shot.jpeg_bgr, expected)
    raw_h = cam_rig.scale_homography(homography)
    rect = cam_rig.rectify_linear(corrected, raw_h, display)
    mask = cam_rig.visible_mask(shot.raw.rgb.shape, raw_h, display)

    print(
        f"\n  {'ink':8s} | {'meter L*a*b*':>22s} | {'camera L*a*b*':>22s} | "
        f"{'dE':>5s} {'dL*':>6s} {'dC*':>6s} {'dh':>6s}"
    )
    print("  " + "-" * 84)
    rows = []
    for p in placed:
        sx, sy, sw, sh = p["sample_rect"]
        if not mask[sy : sy + sh, sx : sx + sw].all():
            print(f"  {p['label']:8s} | outside the camera's view")
            continue
        refl = rect[sy : sy + sh, sx : sx + sw].reshape(-1, 3).mean(axis=0) / 100.0
        got = lab(cam_rig.apply_camera_fit(refl[None, :], coeffs)[0])
        want = lab(p["xyz"])
        d_c = np.hypot(got[1], got[2]) - np.hypot(want[1], want[2])
        d_h = (
            np.degrees(np.arctan2(got[2], got[1])) - np.degrees(np.arctan2(want[2], want[1])) + 180
        ) % 360 - 180
        name = p["label"].replace("ink_", "")
        print(
            f"  {name:8s} | {want[0]:7.2f} {want[1]:7.2f} {want[2]:7.2f} | "
            f"{got[0]:7.2f} {got[1]:7.2f} {got[2]:7.2f} | "
            f"{delta_e76(got, want):5.2f} {got[0] - want[0]:+6.2f} {d_c:+6.2f} {d_h:+6.1f}"
        )
        rows.append(
            {
                "ink": name,
                "meter_lab": [float(v) for v in want],
                "camera_lab": [float(v) for v in got],
                "de76": float(delta_e76(got, want)),
                "chroma_meter": float(np.hypot(want[1], want[2])),
                "chroma_camera": float(np.hypot(got[1], got[2])),
            }
        )
    if floor:
        print(
            f"\n  rig error floor: dL* +/-{floor['dL_rms']:.1f}  dC* +/-{floor['dC_rms']:.1f}  "
            f"dh +/-{floor['dh_rms_deg']:.0f}deg"
        )
    out = outdir / "primaries.json"
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"  -> {out}")
    return 0


def cmd_colour(args) -> int:
    display = DISPLAY_REGISTRY[args.model]
    outdir = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    chart_png = outdir / "colour_chart_expected.png"
    shot_path = outdir / "colour_chart_shot.jpg"
    white_path = outdir / "flat_white_shot.jpg"
    black_path = outdir / "flat_black_shot.jpg"

    data = args.data or Path("docs/screens") / args.model / "measurements/data/campaign.jsonl"
    records = load_records(data)
    prim = primaries(records)
    white_y = float(prim["white"]["xyz"][1])
    black_y = float(prim["black"]["xyz"][1])
    print(f"  dataset : {data} ({len(records)} readings)")
    print(f"  panel   : white Y {white_y:.3f} %, black Y {black_y:.3f} % (measured)")
    patches = select_patches(records, CHART_COLS * CHART_ROWS)
    area = tuple(args.area)
    vis, placed = build_chart(patches, display, area)
    print(f"  chart   : {len(placed)} patches, {CHART_COLS}x{CHART_ROWS} inside {area}")

    expected = np.rint(display.palette_measured_rgb).clip(0, 255).astype(np.uint8)[vis]
    cv2.imwrite(str(chart_png), cv2.cvtColor(expected, cv2.COLOR_RGB2BGR))

    # Three frames, not one. White and black together separate the two things
    # that vary across this rig's frame — see cam_rig.photometric_fields — and
    # neither can be recovered from the chart itself.
    if not args.reuse_shot:
        flat = np.full((display.visual_h, display.visual_w), 1, dtype=np.uint8)
        dark = np.zeros((display.visual_h, display.visual_w), dtype=np.uint8)
        for raster, path, label in (
            (flat, white_path, "white"),
            (dark, black_path, "black"),
            (vis, shot_path, "chart"),
        ):
            if not push_visual(
                raster, display, args.port, args.bench_flip180, args.console_timeout
            ):
                print(f"upload failed ({label} frame)")
                return 1
            shot = cam_rig.capture_linear(path, settle_ms=args.settle, verbose=False)
            print(f"  {label:5s} frame : {dng_summarise(shot.raw)}")
            if shot.raw.saturated_fraction > 1e-3:
                print("    WARNING: saturated — shorten the shutter and re-run")

    white_shot = cam_rig.load_linear(white_path)
    black_shot = cam_rig.load_linear(black_path)
    chart_shot = cam_rig.load_linear(shot_path)
    meta = chart_shot.metadata

    illum, glare = cam_rig.photometric_fields(
        white_shot.raw.rgb, black_shot.raw.rgb, white_y, black_y
    )
    glare_pct = 100.0 * float(np.mean(glare / np.maximum(illum * white_y, 1e-9)))
    print(
        f"  illumination : spans {illum.min() / illum.max():.3f}..1.000 across the frame\n"
        f"  glare        : {glare_pct:.2f} % of a white panel, added everywhere"
    )
    corrected = cam_rig.apply_photometric(chart_shot.raw.rgb, illum, glare)

    # Registration runs on the JPEG and photometry on the raw; one homography
    # serves both once scaled, since they are the same view at 2x.
    homography, info = cam_rig.content_homography(chart_shot.jpeg_bgr, expected)
    raw_h = cam_rig.scale_homography(homography)
    rect = cam_rig.rectify_linear(corrected, raw_h, display)
    mask = cam_rig.visible_mask(chart_shot.raw.rgb.shape, raw_h, display)
    cv2.imwrite(
        str(outdir / "colour_chart_rectified.png"),
        cv2.cvtColor(np.clip(rect / white_y * 235, 0, 255).astype(np.uint8), cv2.COLOR_RGB2BGR),
    )

    used, cam_lin, meas_xyz = [], [], []
    for p in placed:
        sx, sy, sw, sh = p["sample_rect"]
        if not mask[sy : sy + sh, sx : sx + sw].all():
            continue  # this cell is outside what the camera sees
        cam_lin.append(rect[sy : sy + sh, sx : sx + sw].reshape(-1, 3).mean(axis=0))
        meas_xyz.append(p["xyz"])
        used.append(p)
    if len(used) < MIN_FIT_PATCHES:
        print(f"only {len(used)} patches inside the view — need {MIN_FIT_PATCHES}")
        return 1

    # Reflectance percent scaled to roughly 0..0.4, where the polynomial basis is
    # best conditioned. Both sides of the fit are now in the campaign's own units
    # rather than one side in camera counts.
    cam_lin = np.array(cam_lin) / 100.0
    meas_xyz = np.array(meas_xyz)
    coeffs = cam_rig.fit_camera_to_xyz(cam_lin, meas_xyz)
    feats = cam_rig.poly_features(cam_lin)
    cam_rig.check_fit_sanity(coeffs, white_y, black_y)

    # Leave-one-out, because an 11-term fit on a few dozen patches can look
    # excellent while having simply memorised them. The gap between these two
    # numbers is the honest error bar on every colour this rig reports later.
    train = np.array([delta_e76(lab(a), lab(b)) for a, b in zip(feats @ coeffs, meas_xyz)])
    loo, d_l, d_c, d_h = [], [], [], []
    for i in range(len(used)):
        keep = np.arange(len(used)) != i
        c, *_ = np.linalg.lstsq(feats[keep], meas_xyz[keep], rcond=None)
        got, want = lab(feats[i] @ c), lab(meas_xyz[i])
        loo.append(delta_e76(got, want))
        d_l.append(got[0] - want[0])
        d_c.append(np.hypot(got[1], got[2]) - np.hypot(want[1], want[2]))
        hg = np.degrees(np.arctan2(got[2], got[1]))
        hw = np.degrees(np.arctan2(want[2], want[1]))
        d_h.append((hg - hw + 180) % 360 - 180)
    loo = np.array(loo)

    # A single dE hides which axis the rig can actually be trusted on, and they
    # are wildly different here: lightness lands within a couple of L*, chroma is
    # nowhere near that. The split is what tells a later comparison which of its
    # own conclusions are measurement and which are noise, so it is stored with
    # the fit rather than recomputed by eye.
    axes = {
        "dL_rms": float(np.sqrt(np.mean(np.square(d_l)))),
        "dL_bias": float(np.mean(d_l)),
        "dC_rms": float(np.sqrt(np.mean(np.square(d_c)))),
        "dC_bias": float(np.mean(d_c)),
        "dh_rms_deg": float(np.sqrt(np.mean(np.square(d_h)))),
    }
    print(
        f"  error floor   : dL* rms {axes['dL_rms']:.2f}   dC* rms {axes['dC_rms']:.2f}   "
        f"dh rms {axes['dh_rms_deg']:.1f} deg"
    )

    print(f"  patches used  : {len(used)} of {len(placed)} (rest outside the view)")
    print(f"  fit residual  : mean {train.mean():.2f} dE, max {train.max():.2f}")
    print(
        f"  leave-one-out : mean {loo.mean():.2f} dE, median {np.median(loo):.2f}, "
        f"p90 {np.percentile(loo, 90):.2f}, max {loo.max():.2f}"
    )
    for i in np.argsort(-loo)[:5]:
        lab_str = np.round(lab(used[i]["xyz"]), 1).tolist()
        print(f"    {used[i]['label']:22s} LOO {loo[i]:5.2f} dE   measured L*a*b* {lab_str}")

    out = outdir / "colour_fit.json"
    out.write_text(
        json.dumps(
            {
                "model": args.model,
                "space": "linear_raw_reflectance_over_100",
                "white_y": white_y,
                "black_y": black_y,
                "coeffs": coeffs.tolist(),
                "n_patches": len(used),
                "residual_mean_de": float(train.mean()),
                "loo_mean_de": float(loo.mean()),
                "loo_p90_de": float(np.percentile(loo, 90)),
                "error_floor": axes,
                "capture_metadata": meta,
                "chart_area": list(area),
                "registration": info,
                "patches": [
                    {"label": p["label"], "xyz": p["xyz"], "loo_de": float(d)}
                    for p, d in zip(used, loo)
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"  fit -> {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("colour")
    c.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    c.add_argument("--port")
    c.add_argument("--bench-flip180", action="store_true")
    c.add_argument("--console-timeout", type=float, default=180.0)
    c.add_argument("--outdir", type=Path, default=DEFAULT_DIR)
    c.add_argument("--settle", type=int, default=2000)
    c.add_argument("--data", type=Path, default=None)
    c.add_argument(
        "--area",
        type=int,
        nargs=4,
        metavar=("X0", "Y0", "X1", "Y1"),
        default=(152, 61, 1518, 1068),
        help="panel rectangle to pack the chart into — set it to what the camera "
        "actually sees (printed by any registration as 'panel seen'), or cells "
        "outside the view are wasted",
    )
    c.add_argument(
        "--reuse-shot",
        action="store_true",
        help="re-solve from the existing colour_chart_shot.jpg, no panel time",
    )
    c.set_defaults(func=cmd_colour)

    m = sub.add_parser("primaries")
    m.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    m.add_argument("--port")
    m.add_argument("--bench-flip180", action="store_true")
    m.add_argument("--console-timeout", type=float, default=180.0)
    m.add_argument("--outdir", type=Path, default=DEFAULT_DIR)
    m.add_argument("--settle", type=int, default=2000)
    m.add_argument("--data", type=Path, default=None)
    m.add_argument(
        "--area",
        type=int,
        nargs=4,
        metavar=("X0", "Y0", "X1", "Y1"),
        default=(152, 61, 1518, 1068),
    )
    m.add_argument("--reuse-shot", action="store_true")
    m.set_defaults(func=cmd_primaries)

    e = sub.add_parser("exposure")
    e.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    e.add_argument("--port")
    e.add_argument("--bench-flip180", action="store_true")
    e.add_argument("--console-timeout", type=float, default=180.0)
    e.add_argument("--outdir", type=Path, default=DEFAULT_DIR)
    e.add_argument("--settle", type=int, default=2000)
    e.add_argument("--target", type=float, default=0.85)
    e.add_argument(
        "--shutters",
        type=int,
        nargs="*",
        default=[40000, 28000, 20000, 14000, 10000, 7000, 5000],
    )
    e.add_argument("--reuse-shot", action="store_true", help="panel already shows white")
    e.set_defaults(func=cmd_exposure)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
