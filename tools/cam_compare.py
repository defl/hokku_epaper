#!/usr/bin/env python3
"""Photograph what the panel actually shows, and say how it differs from the source.

The colour campaign produced three corrections, applying all of them made
pictures look worse, and nothing so far could say which one did the damage or in
what way. Simulation cannot settle it — the whole question is whether the model
built from those measurements describes the glass. So this measures the glass:
render an arm, put it on the panel, photograph it, convert the photograph into
what a colorimeter would have said, and compare that against the source image
pixel for pixel.

Three things are reported, in increasing order of usefulness:

  * how far the panel lands from the source overall (dE00) — the headline, and
    the least informative number, because a 41 %-of-sRGB gamut guarantees a large
    one no matter how good the pipeline is;
  * the DECOMPOSITION into lightness, chroma and hue error — which is what "looks
    bad" actually resolves to, and what distinguishes a washed-out picture from a
    crushed one from one with a colour cast;
  * the tone transfer curve, source L* against panel L*, which shows crushing and
    clipping directly and is usually where the answer is.

Skin gets its own numbers throughout. Whole-image error is dominated by the
gamut and moves very little between arms, while faces are where every quality
complaint in this project has actually come from.

    python tools/cam_compare.py --port COM4 --bench-flip180 --arms none,all
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
from color_model import INK_NAMES, fit_n, load_records, primaries, yn_mix
from color_validate_photos import lab_img, srgb_img_to_xyz
from correction_ab import HUMANS, preview_rgb, push, render
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_streaming import StreamingDither
from hokku.webserver.image_renderer import ImageRenderer, open_image_for_render
from hokku.webserver.orientation import Orientation
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS
from hokku_server import DEFAULT_BASE, fetch_original, image_config
from panel_arms import arm_label, campaign_data_path, parse_arm, to_visible

BLOCK = 8  # panel px averaged together before comparing — see block_mean()


def block_mean(arr: np.ndarray, k: int) -> np.ndarray:
    """Average non-overlapping k x k blocks. Input (H,W,C) -> (H//k, W//k, C).

    This is the eye's local integration, and it is the only honest scale at which
    a dither can be compared to a continuous-tone source: at full resolution
    every pixel is one of six inks, so a per-pixel difference measures the
    dithering rather than the colour. Non-overlapping blocks rather than a
    sliding box because both sides get the identical treatment and the result is
    an order of magnitude cheaper.
    """
    h, w = arr.shape[0] // k * k, arr.shape[1] // k * k
    return arr[:h, :w].reshape(h // k, k, w // k, k, -1).mean(axis=(1, 3))


def delta_e00(lab1: np.ndarray, lab2: np.ndarray) -> np.ndarray:
    """CIEDE2000 between two (...,3) CIELAB arrays."""
    l1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    l2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]
    c1, c2 = np.hypot(a1, b1), np.hypot(a2, b2)
    cbar = (c1 + c2) / 2
    g = 0.5 * (1 - np.sqrt(cbar**7 / (cbar**7 + 25.0**7 + 1e-12)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360

    dlp = l2 - l1
    dcp = c2p - c1p
    dhp = h2p - h1p
    dhp = np.where(dhp > 180, dhp - 360, np.where(dhp < -180, dhp + 360, dhp))
    dhp = np.where(c1p * c2p == 0, 0.0, dhp)
    dHp = 2 * np.sqrt(c1p * c2p) * np.sin(np.radians(dhp) / 2)

    lbar = (l1 + l2) / 2
    cbarp = (c1p + c2p) / 2
    hsum = h1p + h2p
    hdiff = np.abs(h1p - h2p)
    hbarp = np.where(
        c1p * c2p == 0,
        hsum,
        np.where(hdiff <= 180, hsum / 2, np.where(hsum < 360, (hsum + 360) / 2, (hsum - 360) / 2)),
    )
    t = (
        1
        - 0.17 * np.cos(np.radians(hbarp - 30))
        + 0.24 * np.cos(np.radians(2 * hbarp))
        + 0.32 * np.cos(np.radians(3 * hbarp + 6))
        - 0.20 * np.cos(np.radians(4 * hbarp - 63))
    )
    sl = 1 + (0.015 * (lbar - 50) ** 2) / np.sqrt(20 + (lbar - 50) ** 2)
    sc = 1 + 0.045 * cbarp
    sh = 1 + 0.015 * cbarp * t
    rt = (
        -2
        * np.sqrt(cbarp**7 / (cbarp**7 + 25.0**7 + 1e-12))
        * np.sin(np.radians(60 * np.exp(-(((hbarp - 275) / 25) ** 2))))
    )
    return np.sqrt(
        (dlp / sl) ** 2 + (dcp / sc) ** 2 + (dHp / sh) ** 2 + rt * (dcp / sc) * (dHp / sh)
    )


def source_canvas(
    image: Path, display, preset: str, crop_to_fill_threshold: float = 0.0
) -> np.ndarray:
    """The source image, cropped and fitted exactly as the renderer did. RGB uint8.

    Geometry comes from the renderer's own ``_prepare_canvas`` driven by the
    pixel-exact ``calibration_raw`` preset, so the crop, fit, letterbox and
    rotation match the render being compared against, while every tonal stage is
    at its identity. Reimplementing the crop here is how a comparison silently
    ends up measuring a half-pixel offset instead of colour.
    """
    preset_key = "calibration_raw"  # every tonal stage at identity; geometry only
    del preset  # geometry is preset-independent; named for call-site symmetry
    renderer = ImageRenderer(dither=StreamingDither(display), display=display)
    # Production's loader, not a bare Image.open: it applies the EXIF rotation
    # (21 library photos carry one, and without it the bench rendered them
    # sideways or upside down) and the same pre-shrink the server does.
    with open_image_for_render(Path(image)) as img:
        arr, _mask = renderer._prepare_canvas(
            img,
            PRESET_IMAGE_CONFIGS[preset_key],
            Orientation.LANDSCAPE,
            display.panel_w,
            display.panel_h,
            crop_to_fill_threshold,
        )
    return np.asarray(to_visible(np.asarray(arr), display), dtype=np.uint8)


def model_lab(idx_visual: np.ndarray, prim_mat: np.ndarray, n: float, k: int) -> np.ndarray:
    """What the campaign's model PREDICTS this raster looks like, block-averaged.

    Same Yule-Nielsen mixing the rest of the project uses; only the local
    integration differs, using the same blocks as everything else here so the
    prediction and the photograph are directly subtractable.
    """
    h, w = idx_visual.shape[0] // k * k, idx_visual.shape[1] // k * k
    cov = np.stack(
        [(idx_visual[:h, :w] == i).astype(np.float32) for i in range(len(INK_NAMES))], -1
    )
    frac = block_mean(cov, k)
    frac /= np.maximum(frac.sum(axis=-1, keepdims=True), 1e-9)
    flat = frac.reshape(-1, len(INK_NAMES))
    return lab_img(yn_mix(flat, prim_mat, n).reshape(frac.shape[0], frac.shape[1], 3))


def detail_ratio(src: np.ndarray, got: np.ndarray, keep: np.ndarray) -> tuple[float, float]:
    """How much of the source's local variation survives, in lightness and chroma.

    Added because a mean-chroma number cannot see banding, and banding is what
    ruins a picture. A minimum-dE gamut map raised measured chroma on a red wall
    from 11 to 33 and simultaneously flattened its shading into one slab: every
    out-of-gamut red collapsed onto the same boundary point. The metric said the
    arm had improved; the person looking at the panel said it had lost all
    detail, and they were right.

    Local variation is the mean absolute gradient. A ratio near 1 means modulation
    is preserved; well under 1 means the render is posterising something the
    source had gradation in.
    """

    def variation(plane: np.ndarray) -> float:
        gy = np.abs(np.diff(plane, axis=0))[:, :-1]
        gx = np.abs(np.diff(plane, axis=1))[:-1, :]
        m = keep[:-1, :-1]
        return float(np.mean((gy + gx)[m])) if m.any() else float("nan")

    src_c = np.hypot(src[..., 1], src[..., 2])
    got_c = np.hypot(got[..., 1], got[..., 2])
    l_ratio = variation(got[..., 0]) / max(variation(src[..., 0]), 1e-6)
    c_ratio = variation(got_c) / max(variation(src_c), 1e-6)
    return l_ratio, c_ratio


def describe(src: np.ndarray, got: np.ndarray, keep: np.ndarray, name: str) -> dict:
    """Decompose the error over the selected pixels and print it."""
    if keep.sum() < 500:
        print(f"  {name:14s} too few pixels")
        return {}
    s, g = src[keep], got[keep]
    de = delta_e00(s, g)
    dl = g[:, 0] - s[:, 0]
    cs, cg = np.hypot(s[:, 1], s[:, 2]), np.hypot(g[:, 1], g[:, 2])
    dc = cg - cs
    hs = np.degrees(np.arctan2(s[:, 2], s[:, 1]))
    hg = np.degrees(np.arctan2(g[:, 2], g[:, 1]))
    dh = (hg - hs + 180) % 360 - 180
    warm = cs > 8  # hue is meaningless on near-neutral pixels
    stats = {
        "n": int(keep.sum()),
        "de00_mean": float(de.mean()),
        "de00_p90": float(np.percentile(de, 90)),
        "dL_mean": float(dl.mean()),
        "dC_mean": float(dc.mean()),
        "dh_mean": float(np.average(dh[warm], weights=cs[warm]))
        if warm.sum() > 50
        else float("nan"),
        "db_mean": float((g[:, 2] - s[:, 2]).mean()),
    }
    print(
        f"  {name:14s} dE00 {stats['de00_mean']:5.1f} (p90 {stats['de00_p90']:5.1f})   "
        f"dL* {stats['dL_mean']:+6.1f}   dC* {stats['dC_mean']:+6.1f}   "
        f"dh {stats['dh_mean']:+6.1f}deg   db* {stats['db_mean']:+6.1f}"
    )
    return stats


def tone_curve(src: np.ndarray, got: np.ndarray, keep: np.ndarray) -> list[tuple]:
    """Mean panel L* per source-L* decile. Crushing and clipping show up here first."""
    s, g = src[keep][:, 0], got[keep][:, 0]
    edges = np.linspace(0, 100, 11)
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = (s >= lo) & (s < hi)
        if sel.sum() > 100:
            rows.append(
                (float(lo), float(hi), float(s[sel].mean()), float(g[sel].mean()), int(sel.sum()))
            )
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--port", required=True)
    ap.add_argument("--bench-flip180", action="store_true")
    ap.add_argument("--console-timeout", type=float, default=180.0)
    ap.add_argument("--outdir", type=Path, default=Path("build/camcal"))
    ap.add_argument("--arms", default="none,all")
    ap.add_argument("--images", nargs="*", type=Path, default=[Path(p) for p in HUMANS])
    ap.add_argument("--preset", default="default_general")
    ap.add_argument(
        "--server",
        nargs="?",
        const=DEFAULT_BASE,
        default=None,
        help="pull the render config AND any --server-image from a live Hokku server "
        "instead of using presets.py. A deployed appliance's config drifts from the "
        "repo defaults, and rendering with the wrong one measures the wrong pipeline.",
    )
    ap.add_argument(
        "--server-image",
        nargs="*",
        default=[],
        help="pool filenames to fetch from --server and measure, e.g. IMG_1234.JPEG",
    )
    ap.add_argument("--settle", type=int, default=2000)
    ap.add_argument("--block", type=int, default=BLOCK)
    ap.add_argument(
        "--reuse-shots", action="store_true", help="re-analyse existing photographs, no panel time"
    )
    args = ap.parse_args(argv)

    display = DISPLAY_REGISTRY[args.model]
    outdir = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)

    server_cfg = None
    if args.server:
        server_cfg = image_config(base=args.server)
        print(
            f"  live config : dither={server_cfg.dither.lut_name} "
            f"neutral_chroma={server_cfg.dither.neutral_chroma} "
            f"clahe={server_cfg.clahe_clip_limit} saturate={server_cfg.adaptive_saturate_space}"
        )
    if args.server_image:
        if not args.server:
            print("--server-image needs --server")
            return 1
        cache = outdir / "server_images"
        args.images = [fetch_original(n, cache, base=args.server) for n in args.server_image]
        print(f"  fetched {len(args.images)} image(s) from {args.server}")

    fit_path = outdir / "colour_fit.json"
    if not fit_path.exists():
        print(f"no colour fit at {fit_path} — run tools/cam_calibrate.py colour first")
        return 1
    fit = json.loads(fit_path.read_text(encoding="utf-8"))
    coeffs = np.array(fit["coeffs"])
    floor = fit.get("error_floor", {})
    print(f"  colour fit : {fit['n_patches']} patches, leave-one-out {fit['loo_mean_de']:.2f} dE")
    if floor:
        # Printed on every run because it is the line that decides which of the
        # numbers below mean anything. On this rig lightness is worth trusting
        # and chroma is barely better than a guess, and a dL* of 3 next to a
        # dC* of 4 are not remotely the same kind of statement.
        print(
            f"  error floor: dL* +/-{floor['dL_rms']:.1f}   dC* +/-{floor['dC_rms']:.1f}   "
            f"dh +/-{floor['dh_rms_deg']:.0f}deg  <- anything smaller than this is noise"
        )

    records = load_records(campaign_data_path(args.model))
    prim = primaries(records)

    # The same illumination and glare fields the fit was built on. They must come
    # from that calibration run and not be re-derived here, or the photographs
    # being compared would be corrected differently from the patches that defined
    # what the numbers mean.
    try:
        white_shot = cam_rig.load_linear(outdir / "flat_white_shot.jpg")
        black_shot = cam_rig.load_linear(outdir / "flat_black_shot.jpg")
    except RuntimeError:
        print("no white/black frames — run tools/cam_calibrate.py colour first")
        return 1
    illum, glare = cam_rig.photometric_fields(
        white_shot.raw.rgb, black_shot.raw.rgb, float(fit["white_y"]), float(fit["black_y"])
    )
    cam_rig.check_fit_sanity(coeffs, float(fit["white_y"]), float(fit["black_y"]))

    n_yn, _ = fit_n([r for r in records if r.get("source") != "ink"], prim, spectral=False)
    prim_mat = np.array([prim[k]["xyz"] for k in INK_NAMES])

    results = []
    for image in args.images:
        src_rgb = source_canvas(image, display, args.preset)
        src_xyz = srgb_img_to_xyz(src_rgb)
        src = lab_img(block_mean(src_xyz, args.block))
        chroma = np.hypot(src[..., 1], src[..., 2])
        hue = np.degrees(np.arctan2(src[..., 2], src[..., 1]))
        skin = (hue > -40) & (hue < 70) & (chroma > 12) & (src[..., 0] > 20)
        neutral = chroma < 5

        for arm in args.arms.split(","):
            label = arm_label(parse_arm(arm))
            stem = f"{image.stem[:24]}__{label}"
            shot_path = outdir / f"{stem}__shot.jpg"
            print(f"\n=== {image.name}   arm={label} " + "=" * 20)

            idx, arm_disp = render(args.model, image, arm, args.preset, cfg=server_cfg)
            expected = np.asarray(preview_rgb(idx, arm_disp, display))
            cv2.imwrite(
                str(outdir / f"{stem}__expected.png"), cv2.cvtColor(expected, cv2.COLOR_RGB2BGR)
            )
            if not args.reuse_shots:
                if not push(
                    idx, arm_disp, display, args.port, args.bench_flip180, args.console_timeout
                ):
                    print("upload failed")
                    return 1
                cam_rig.capture_linear(shot_path, settle_ms=args.settle, verbose=False)

            shot = cam_rig.load_linear(shot_path)
            corrected = cam_rig.apply_photometric(shot.raw.rgb, illum, glare)
            # Registration on the JPEG, photometry on the raw — one homography
            # serves both once scaled, since they are the same view at 2x.
            homography, _info = cam_rig.content_homography(shot.jpeg_bgr, expected)
            raw_h = cam_rig.scale_homography(homography)
            rect = cam_rig.rectify_linear(corrected, raw_h, display)
            mask = cam_rig.visible_mask(shot.raw.rgb.shape, raw_h, display)

            # /100 to match the units the fit was solved in: reflectance percent
            # scaled to roughly 0..0.4.
            got = lab_img(block_mean(cam_rig.apply_camera_fit(rect / 100.0, coeffs), args.block))
            seen = block_mean(mask[..., None].astype(np.float32), args.block)[..., 0] > 0.999

            print("  --- measured on glass, against the source image ---")
            overall = describe(src, got, seen, "everything")
            skin_stats = describe(src, got, seen & skin, "skin")
            neutral_stats = describe(src, got, seen & neutral, "neutrals")

            pred = model_lab(to_visible(idx, display), prim_mat, n_yn, args.block)
            print("  --- the campaign model's prediction, against the same photograph ---")
            agree = describe(pred, got, seen, "model vs cam")

            l_keep, c_keep = detail_ratio(src, got, seen)
            print(
                f"  detail kept    L* {l_keep:5.2f}x   chroma {c_keep:5.2f}x   "
                "(1.0 = source modulation preserved; low means banding)"
            )

            print("  --- tone transfer, source L* -> panel L* ---")
            for lo, hi, sm, gm, cnt in tone_curve(src, got, seen):
                bar = "#" * round(gm / 3)
                print(
                    f"    L* {lo:3.0f}-{hi:3.0f}  source {sm:5.1f} -> panel {gm:5.1f}  {bar} ({cnt})"
                )

            results.append(
                {
                    "image": image.name,
                    "arm": label,
                    "overall": overall,
                    "skin": skin_stats,
                    "neutrals": neutral_stats,
                    "model_vs_camera": agree,
                    "detail_l": l_keep,
                    "detail_c": c_keep,
                    "tone": tone_curve(src, got, seen),
                }
            )
            cv2.imwrite(
                str(outdir / f"{stem}__measured.png"),
                cv2.cvtColor(
                    np.clip(rect / float(fit["white_y"]) * 235, 0, 255).astype(np.uint8),
                    cv2.COLOR_RGB2BGR,
                ),
            )

    out = outdir / "comparison.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n  results -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
