#!/usr/bin/env python3
"""Build a correction LUT that clamps requests into what the panel can reach.

Measured on glass: in the strongly red region of a test portrait the dither lays
down 87 % pure red ink and still only achieves chroma 30.7, against 42.8 for the
ink itself. The missing chroma is not timidity — it is 5.4 % of BLUE ink
scattered through the region, whose b* of -36 cancels red's +24 far out of
proportion to its coverage.

That blue is error diffusion doing what it is designed to do with an impossible
request. The source asks for chroma 61.4, the panel tops out at 42.8, and the
43 % that cannot be represented does not simply vanish: it accumulates in the
diffusion buffer until it is large enough to flip a pixel to whatever ink is
nearest the accumulated error, which for an over-saturated red is the opposite
side of the hue circle. The ink budget is then spent cancelling itself.

The fix is not to dither differently but to stop asking. Map every request into
the reachable set first, and the residual the dither has to diffuse is small
enough that it never flips a pixel to a hue-opposite ink.

The reachable set comes from the campaign's own Yule-Nielsen model rather than
from the palette table: what a viewer sees is the local MIXTURE of inks, so the
gamut is the YN image of the coverage simplex, which is considerably larger than
the six ink points and a completely different shape.

    python tools/gamut_clamp.py --model huessen_epf1301 --out build/camcal/clamp_lut.npy
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from color_model import D65, INK_NAMES, fit_n, load_records, primaries, yn_mix
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_streaming import rgb_to_lab

LUT_STEPS = 33
L_BINS = 51
HUE_BINS = 72
KNEE = 0.90  # fraction of the reachable chroma below which nothing is touched
SIMPLEX_SAMPLES = 40_000
SAMPLE_SEED = 20260906
RAY_MAX = 3.0  # scan this far past the request, so "deep inside" is distinguishable

_M_XYZ2RGB = np.array(
    [
        [3.2406, -1.5372, -0.4986],
        [-0.9689, 1.8758, 0.0415],
        [0.0557, -0.2040, 1.0570],
    ]
)


def lab_to_rgb(lab: np.ndarray) -> np.ndarray:
    """(...,3) CIELAB under D65 -> (...,3) sRGB 0..255, clipped."""
    fy = (lab[..., 0] + 16.0) / 116.0
    fx = fy + lab[..., 1] / 500.0
    fz = fy - lab[..., 2] / 200.0
    f = np.stack([fx, fy, fz], axis=-1)
    eps = 216 / 24389
    t = np.where(f**3 > eps, f**3, (116 * f - 16) / (24389 / 27))
    xyz = t * D65
    lin = np.clip(xyz @ _M_XYZ2RGB.T, 0.0, 1.0)
    srgb = np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * lin ** (1 / 2.4) - 0.055)
    return np.clip(srgb * 255.0, 0.0, 255.0)


def reachable_lab(records: list[dict], samples: int = SIMPLEX_SAMPLES) -> np.ndarray:
    """Lab of a dense sample of realisable ink mixtures.

    The corners, every pair, a coarse grid of triples, and a Dirichlet cloud. The
    structured part matters more than the random part: a panel's gamut hull is
    made of its two- and three-ink edges, and uniform Dirichlet sampling barely
    visits them.
    """
    prim = primaries(records)
    missing = [k for k in INK_NAMES if k not in prim]
    if missing:
        raise SystemExit(f"campaign is missing ink measurements: {missing}")
    prim_mat = np.array([prim[k]["xyz"] for k in INK_NAMES])
    n_yn, _ = fit_n([r for r in records if r.get("source") != "ink"], prim, spectral=False)

    n_inks = len(INK_NAMES)
    mixes = [np.eye(n_inks)]

    steps = np.linspace(0.0, 1.0, 17)
    for i in range(n_inks):
        for j in range(i + 1, n_inks):
            m = np.zeros((len(steps), n_inks))
            m[:, i] = steps
            m[:, j] = 1.0 - steps
            mixes.append(m)

    tri = np.array(
        [(a, b) for a in np.linspace(0, 1, 9) for b in np.linspace(0, 1, 9) if a + b <= 1]
    )
    for i in range(n_inks):
        for j in range(i + 1, n_inks):
            for k in range(j + 1, n_inks):
                m = np.zeros((len(tri), n_inks))
                m[:, i] = tri[:, 0]
                m[:, j] = tri[:, 1]
                m[:, k] = 1.0 - tri[:, 0] - tri[:, 1]
                mixes.append(m)

    rng = np.random.default_rng(SAMPLE_SEED)
    mixes.append(rng.dirichlet(np.full(n_inks, 0.4), size=samples))

    frac = np.vstack(mixes)
    xyz = yn_mix(frac, prim_mat, n_yn)
    t = np.asarray(xyz, dtype=float) / 100.0 / D65
    f = np.where(t > 216 / 24389, np.cbrt(np.maximum(t, 0)), (24389 / 27 * t + 16) / 116)
    return np.stack(
        [116 * f[:, 1] - 16, 500 * (f[:, 0] - f[:, 1]), 200 * (f[:, 1] - f[:, 2])], axis=-1
    )


def chroma_ceiling(labs: np.ndarray) -> np.ndarray:
    """(L_BINS, HUE_BINS) maximum reachable chroma, filled and smoothed.

    Empty cells are filled from their neighbours rather than left at zero: a gap
    in the sampling would otherwise read as "this panel cannot show that colour"
    and crush a hue the panel is perfectly capable of.
    """
    chroma = np.hypot(labs[:, 1], labs[:, 2])
    hue = np.degrees(np.arctan2(labs[:, 2], labs[:, 1])) % 360
    li = np.clip((labs[:, 0] / 100.0 * (L_BINS - 1)).astype(int), 0, L_BINS - 1)
    hi = np.clip((hue / 360.0 * HUE_BINS).astype(int), 0, HUE_BINS - 1)

    ceiling = np.zeros((L_BINS, HUE_BINS))
    np.maximum.at(ceiling, (li, hi), chroma)

    for _ in range(6):
        if (ceiling > 0).all():
            break
        padded = np.pad(ceiling, ((1, 1), (0, 0)), mode="edge")
        padded = np.pad(padded, ((0, 0), (1, 1)), mode="wrap")  # hue is circular
        neigh = np.stack(
            [padded[a : a + L_BINS, b : b + HUE_BINS] for a in range(3) for b in range(3)]
        )
        filled = neigh.max(axis=0)
        ceiling = np.where(ceiling > 0, ceiling, filled)

    # Dilated, never mean-smoothed. This is a maximum envelope and a gamut hull is
    # sharply peaked at the ink hues, so averaging is the wrong operator: it pulled
    # the ceiling at yellow to 60.6 when yellow ink alone reaches 71.3, and the
    # clamp then crushed colours the panel can actually print. A 3x3 max also
    # buys back what bilinear interpolation loses across a one-bin peak, and errs
    # generous, which is the safe direction — an over-tight ceiling desaturates
    # real colour, an over-loose one merely leaves a little error to diffuse.
    padded = np.pad(ceiling, ((1, 1), (0, 0)), mode="edge")
    padded = np.pad(padded, ((0, 0), (1, 1)), mode="wrap")  # hue is circular
    return np.stack(
        [padded[a : a + L_BINS, b : b + HUE_BINS] for a in range(3) for b in range(3)]
    ).max(axis=0)


def lookup_ceiling(ceiling: np.ndarray, lab: np.ndarray) -> np.ndarray:
    """Bilinear ceiling lookup for (...,3) Lab."""
    hue = np.degrees(np.arctan2(lab[..., 2], lab[..., 1])) % 360
    lf = np.clip(lab[..., 0] / 100.0 * (L_BINS - 1), 0, L_BINS - 1)
    hf = hue / 360.0 * HUE_BINS
    l0 = np.clip(np.floor(lf).astype(int), 0, L_BINS - 2)
    h0 = np.floor(hf).astype(int) % HUE_BINS
    h1 = (h0 + 1) % HUE_BINS
    dl, dh = (lf - l0)[..., None][..., 0], hf - np.floor(hf)
    top = ceiling[l0, h0] * (1 - dh) + ceiling[l0, h1] * dh
    bot = ceiling[l0 + 1, h0] * (1 - dh) + ceiling[l0 + 1, h1] * dh
    return top * (1 - dl) + bot * dl


def cusp_lightness(ceiling: np.ndarray) -> np.ndarray:
    """(HUE_BINS,) the L* at which each hue reaches its maximum chroma.

    On this panel the red-orange cusp sits at L* 24 while the yellow cusp sits at
    L* 65 — the gamut is a wedge whose widest point moves with hue, which is
    exactly why a single global strategy cannot work.
    """
    l_values = np.linspace(0.0, 100.0, L_BINS)
    return l_values[np.argmax(ceiling, axis=0)]


def build_cusp_lut(
    records: list[dict],
    steps: int = LUT_STEPS,
    knee: float = KNEE,
    rays: int = 192,
    blend: float = 1.0,
):
    """RGB->RGB LUT that maps out-of-gamut requests TOWARD THE CUSP, not sideways.

    The chroma-only clamp in ``build_lut`` holds lightness fixed and drops chroma
    until the request is legal. Measured on glass that was a disaster: red
    coverage fell from 87 % to 33 % and the region's chroma from 30.7 to 6.2,
    because this panel's saturated red is DARK. Its red-orange cusp is at L* 24,
    so a request at L* 43 has a ceiling of only 18 — cutting sideways throws the
    colour away.

    Error diffusion, left alone, does better than that: it lands at L* 29 chroma
    31, having implicitly traded lightness for saturation. This makes that trade
    explicit and deliberate.

    Every request is moved along the ray from a neutral anchor toward the request
    itself, and the ray is followed only as far as the gamut allows. Hue is
    preserved exactly; lightness and chroma give way in a proportion set by
    ``blend``, which is the whole design:

      blend 0  anchor at the request's OWN lightness. The ray is horizontal, so
               only chroma moves. Lightness is preserved exactly and a
               strongly out-of-gamut red goes pale — measured on glass, a red
               wall lands at the right L* and reads beige.
      blend 1  anchor at the hue's cusp lightness. Lightness is traded freely for
               saturation. The same wall keeps its red and lands 26 L* too dark.

    Both extremes were measured and both are wrong in opposite directions, which
    is what makes this a dial rather than a choice of algorithm. It is a judgement
    about what a photograph should give up, so it belongs to whoever is looking at
    the panel, not to a metric — every metric tried here preferred one extreme.
    """
    ceiling = chroma_ceiling(reachable_lab(records))
    cusp_l = cusp_lightness(ceiling)

    vals = np.linspace(0.0, 255.0, steps)
    rr, gg, bb = np.meshgrid(vals, vals, vals, indexing="ij")
    grid = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)
    lab = np.asarray(rgb_to_lab(grid), dtype=np.float64)

    chroma = np.hypot(lab[:, 1], lab[:, 2])
    hue = np.degrees(np.arctan2(lab[:, 2], lab[:, 1])) % 360
    cusp_at_hue = cusp_l[np.clip((hue / 360.0 * HUE_BINS).astype(int), 0, HUE_BINS - 1)]
    anchor_l = lab[:, 0] + blend * (cusp_at_hue - lab[:, 0])

    # How far along the anchor->request ray the gamut extends. Scanned rather than
    # solved: the boundary is a sampled table, not a closed form, and a scan
    # cannot fall into the wrong root the way a solver can on a wedge.
    #
    # The scan deliberately runs PAST the request (t > 1). Stopping at the request
    # made every colour look like it sat exactly on the boundary, so the knee
    # fired on all of them and the LUT moved 100 % of the grid — including
    # colours the panel prints perfectly well. Going further is what distinguishes
    # "just inside" from "deep inside".
    t = np.linspace(RAY_MAX, 0.0, rays)
    reach = np.zeros_like(chroma)
    for frac in t:
        l_t = anchor_l + frac * (lab[:, 0] - anchor_l)
        c_t = frac * chroma
        probe = np.stack([l_t, c_t * np.cos(np.radians(hue)), c_t * np.sin(np.radians(hue))], -1)
        inside = c_t <= lookup_ceiling(ceiling, probe)
        reach = np.where((reach == 0.0) & inside, frac, reach)
    reach = np.maximum(reach, 1e-3)

    # Position of the request as a fraction of the distance to the boundary:
    # below 1 is inside, above 1 is outside. The knee acts on that, so colours
    # well within the gamut are untouched and those outside stay ordered instead
    # of piling onto the boundary surface.
    u = 1.0 / reach
    u_out = np.where(u <= knee, u, knee + (1.0 - knee) * np.tanh((u - knee) / (1.0 - knee)))
    scale = np.minimum(u_out / np.maximum(u, 1e-9), 1.0)

    out_l = anchor_l + scale * (lab[:, 0] - anchor_l)
    out_c = scale * chroma
    out_lab = np.stack(
        [out_l, out_c * np.cos(np.radians(hue)), out_c * np.sin(np.radians(hue))], axis=-1
    )
    lut = lab_to_rgb(out_lab).reshape(steps, steps, steps, 3).astype(np.float32)

    moved = float(np.mean(scale < 0.999))
    return lut, ceiling, moved, float(np.mean((chroma - out_c)[scale < 0.999]))


def build_minde_lut(records: list[dict], steps: int = LUT_STEPS, knee: float = KNEE):
    """RGB->RGB LUT mapping each request to its NEAREST reachable colour at its hue.

    The cusp-ray map lost to plain error diffusion because the ray from a neutral
    anchor happens to pass close to where diffusion already lands. Geometry says
    that is not the best available point: for the test image's red at L* 43
    chroma 61, the nearest legal colour is the cusp itself at L* 20 chroma 43 —
    a Lab distance of 30, against 34 for what the dither reaches unaided.

    So this minimises distance to the gamut surface directly, hue held exactly,
    with lightness and chroma both free. It is HPMINDE, and its known weakness is
    that many distinct requests collapse onto the same boundary point and band;
    on a panel with a 41 %-of-sRGB gamut that may be a price worth paying, which
    is a question for the glass rather than for an opinion.
    """
    ceiling = chroma_ceiling(reachable_lab(records))

    vals = np.linspace(0.0, 255.0, steps)
    rr, gg, bb = np.meshgrid(vals, vals, vals, indexing="ij")
    grid = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)
    lab = np.asarray(rgb_to_lab(grid), dtype=np.float64)

    chroma = np.hypot(lab[:, 1], lab[:, 2])
    hue = np.degrees(np.arctan2(lab[:, 2], lab[:, 1])) % 360
    hue_bin = np.clip((hue / 360.0 * HUE_BINS).astype(int), 0, HUE_BINS - 1)

    l_axis = np.linspace(0.0, 100.0, L_BINS)
    ceil_at_hue = ceiling[:, hue_bin].T  # (N, L_BINS) reachable chroma per candidate L*
    cand_c = np.minimum(chroma[:, None], ceil_at_hue)
    dist2 = (l_axis[None, :] - lab[:, 0:1]) ** 2 + (cand_c - chroma[:, None]) ** 2
    best = np.argmin(dist2, axis=1)
    idx = np.arange(len(lab))
    near_l = l_axis[best]
    near_c = cand_c[idx, best]

    # Only colours actually outside are moved, and then only past the knee, so a
    # request the panel can already print is left exactly alone.
    inside = chroma <= lookup_ceiling(ceiling, lab)
    over = np.where(inside, 0.0, 1.0)
    span = np.maximum(chroma - near_c, 1e-6)
    pull = knee + (1.0 - knee) * np.tanh(span / np.maximum(chroma, 1e-6))
    frac = over * np.minimum(pull, 1.0)

    out_l = lab[:, 0] + frac * (near_l - lab[:, 0])
    out_c = chroma + frac * (near_c - chroma)
    out_lab = np.stack(
        [out_l, out_c * np.cos(np.radians(hue)), out_c * np.sin(np.radians(hue))], axis=-1
    )
    lut = lab_to_rgb(out_lab).reshape(steps, steps, steps, 3).astype(np.float32)
    moved = float(np.mean(frac > 1e-3))
    return lut, ceiling, moved, float(np.mean((chroma - out_c)[frac > 1e-3]))


def build_lut(display, records: list[dict], steps: int = LUT_STEPS, knee: float = KNEE):
    """(steps,steps,steps,3) float32 RGB->RGB LUT clamping chroma into the gamut."""
    ceiling = chroma_ceiling(reachable_lab(records))

    vals = np.linspace(0.0, 255.0, steps)
    rr, gg, bb = np.meshgrid(vals, vals, vals, indexing="ij")
    grid = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)
    lab = np.asarray(rgb_to_lab(grid), dtype=np.float64)

    chroma = np.hypot(lab[:, 1], lab[:, 2])
    ceil = lookup_ceiling(ceiling, lab)
    knee_c = knee * ceil

    # Soft knee rather than a hard cut. A hard clamp flattens every colour past
    # the boundary onto the same value, which turns a bright saturated area into
    # a flat patch; tanh keeps them ordered while bounding them.
    span = np.maximum(ceil - knee_c, 1e-6)
    over = np.maximum(chroma - knee_c, 0.0)
    new_chroma = np.where(chroma <= knee_c, chroma, knee_c + span * np.tanh(over / span))

    scale = np.where(chroma > 1e-6, new_chroma / np.maximum(chroma, 1e-6), 1.0)
    out_lab = lab.copy()
    out_lab[:, 1] *= scale
    out_lab[:, 2] *= scale
    lut = lab_to_rgb(out_lab).reshape(steps, steps, steps, 3).astype(np.float32)

    touched = float(np.mean(chroma > knee_c))
    reduced = float(np.mean((chroma - new_chroma)[chroma > knee_c])) if touched else 0.0
    return lut, ceiling, touched, reduced


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--knee", type=float, default=KNEE)
    ap.add_argument(
        "--blend",
        type=float,
        default=1.0,
        help="cusp mode only: 0 preserves lightness and lets colour go pale, "
        "1 trades lightness freely for saturation. Both extremes measured wrong "
        "in opposite directions; pick by looking.",
    )
    ap.add_argument(
        "--mode",
        choices=("cusp", "minde", "chroma"),
        default="cusp",
        help="cusp: move toward the hue's cusp lightness, trading L* for chroma "
        "(measured better on glass). chroma: hold L* and cut chroma — kept so the "
        "failure it caused stays reproducible.",
    )
    ap.add_argument("--data", type=Path, default=None)
    args = ap.parse_args(argv)

    display = DISPLAY_REGISTRY[args.model]
    data = args.data or Path("docs/screens") / args.model / "measurements/data/campaign.jsonl"
    records = load_records(data)
    if args.mode == "cusp":
        lut, ceiling, touched, reduced = build_cusp_lut(records, knee=args.knee, blend=args.blend)
    elif args.mode == "minde":
        lut, ceiling, touched, reduced = build_minde_lut(records, knee=args.knee)
    else:
        lut, ceiling, touched, reduced = build_lut(display, records, knee=args.knee)
    print(f"  mode: {args.mode}" + (f"  blend={args.blend}" if args.mode == "cusp" else ""))

    print(f"  reachable chroma ceiling: max {ceiling.max():.1f}, median {np.median(ceiling):.1f}")
    for name, l_star in (("shadows", 20.0), ("midtones", 45.0), ("highlights", 65.0)):
        row = ceiling[int(l_star / 100 * (L_BINS - 1))]
        print(
            f"    at L* {l_star:4.0f} ({name:10s}) chroma ceiling {row.min():5.1f}..{row.max():5.1f}"
        )
    print(f"  grid entries moved: {100 * touched:.1f} %, mean chroma removed {reduced:.1f}")
    cusp = cusp_lightness(ceiling)
    for h, name in ((30, "red-orange"), (100, "yellow"), (270, "blue")):
        b = int(h / 360 * HUE_BINS)
        print(
            f"    cusp at hue {h:3d} ({name:10s}): L* {cusp[b]:5.1f}, chroma {ceiling[:, b].max():5.1f}"
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    np.save(args.out, lut)
    print(f"  LUT {lut.shape} -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
