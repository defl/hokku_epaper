#!/usr/bin/env python3
"""Photograph candidate renders off the glass, one capture per candidate.

Material for a judging session. Each candidate is rendered, pushed to the bench
panel, photographed with the calibrated rig, and converted back to colorimetry,
so what a human later compares is a measurement of the real panel rather than a
simulation of it.

**Capture per candidate, compose trials afterwards.** The obvious design — one
panel refresh per pairwise trial, two candidates side by side at half width — is
the wrong one twice over. It shrinks each picture to 600 px, compromising exactly
the detail judgements that matter most, and it makes the panel time scale with
the number of *comparisons* rather than the number of *candidates*. Capturing
each candidate once at full panel size instead means N captures support up to
N(N-1)/2 possible trials, so an 80-trial session needs roughly 50 refreshes, not
160, and every trial is judged at full resolution.

Resumable by design: a candidate whose output already exists is skipped — after
re-rendering it and checking the stored frame is that exact render, since a tag
names an arm and two plans can use one name for different renders — and one
failure does not abandon the run. Unattended panel time is the scarce resource
here — roughly 60-90 s per candidate — and a crash forty candidates in should
cost forty seconds of re-work, not forty minutes.

    python tools/judge_capture.py --plan build/camcal/session1_plan.json \\
        --port COM4 --bench-flip180
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from glob import escape as glob_escape
from pathlib import Path

import cv2
import numpy as np

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

import cam_rig
from correction_ab import push
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.image_config import ImageConfig, image_config_from_dict_strict
from hokku.webserver.image_renderer import open_image_for_render
from hokku.webserver.orientation import Orientation
from prepare_variants import autocontrast_as
from production import render_kwargs
from render_bank import render_seed, renderer_for, to_visible

M_XYZ2RGB = np.array(
    [[3.2406, -1.5372, -0.4986], [-0.9689, 1.8758, 0.0415], [0.0557, -0.2040, 1.0570]]
)


def xyz_to_srgb(xyz: np.ndarray) -> np.ndarray:
    lin = np.clip(xyz / 100.0 @ M_XYZ2RGB.T, 0, 1)
    s = np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * lin ** (1 / 2.4) - 0.055)
    return np.clip(s * 255, 0, 255).astype(np.uint8)


class Rig:
    """The calibrated camera path, loaded once.

    Holds the colour fit and the two flat frames the photometric separation
    needs. Glare on this panel is additive (the lamination reflects the light
    tent) at about 5 % of a white panel, so a flat-field divide alone cannot
    remove it — both frames are required, not optional.
    """

    def __init__(self, outdir: Path):
        fit = json.loads((outdir / "colour_fit.json").read_text(encoding="utf-8"))
        self.coeffs = np.array(fit["coeffs"])
        cam_rig.check_fit_sanity(self.coeffs, float(fit["white_y"]), float(fit["black_y"]))
        white = cam_rig.load_linear(outdir / "flat_white_shot.jpg")
        black = cam_rig.load_linear(outdir / "flat_black_shot.jpg")
        self.illum, self.glare = cam_rig.photometric_fields(
            white.raw.rgb, black.raw.rgb, float(fit["white_y"]), float(fit["black_y"])
        )
        self.loo_de = float(fit.get("loo_mean_de", float("nan")))

    def to_colorimetry(self, shot: cam_rig.LinearShot, expected: np.ndarray, display):
        """Photograph -> sRGB image in panel coordinates, plus registration info."""
        corrected = cam_rig.apply_photometric(shot.raw.rgb, self.illum, self.glare)
        homography, info = cam_rig.content_homography(shot.jpeg_bgr, expected, verbose=False)
        rect = cam_rig.rectify_linear(corrected, cam_rig.scale_homography(homography), display)
        return xyz_to_srgb(cam_rig.apply_camera_fit(rect / 100.0, self.coeffs)), info


def load_plan(path: Path) -> list[dict]:
    """Candidates as {tag, image, config}. Configs are stored as strict dicts.

    Strict parsing on the way in, so a plan written by one tool and run hours
    later by another cannot silently drift from the fields the renderer expects.
    """
    plan = json.loads(path.read_text(encoding="utf-8"))
    for entry in plan["candidates"]:
        # Plans written before `prepare_autocontrast` existed rendered with PIL's
        # per-channel stretch, so that is what they must replay as. An arm that
        # switched the stage off did it through the bench-only patch below, which
        # still applies on top.
        entry["config"].setdefault("prepare_autocontrast", "per_channel")
        entry["config"] = image_config_from_dict_strict(entry["config"])
    return plan["candidates"]


def display_for(entry: dict, base):
    """The Display to render this candidate through, honouring a per-arm LUT.

    The gamut-correction LUT is a property of the Display, not of ImageConfig, so
    an arm that changes it needs its own Display. The variant must carry a
    distinct model_id and be in DISPLAY_REGISTRY: `_cached_correction_lut`
    memoises on the id and re-resolves the display from the registry, so a
    variant sharing the base id would be handed the base LUT out of cache and
    render identically to production while appearing to work.
    """
    lut = entry.get("lut")
    if not lut:
        return base
    import copy  # noqa: PLC0415 — only the LUT-variant path needs these

    from hokku.screens.registry import DISPLAY_REGISTRY  # noqa: PLC0415

    model_id = f"{base.model_id}__{entry['config_tag']}"
    if model_id not in DISPLAY_REGISTRY:
        var = copy.copy(base)
        var.model_id = model_id
        var.correction_lut_path = Path(lut)
        DISPLAY_REGISTRY[model_id] = var
    return DISPLAY_REGISTRY[model_id]


def render_entry(entry: dict, display) -> tuple[np.ndarray, np.ndarray]:
    """The ink raster (panel orientation) and its expected RGB frame (visible).

    Deterministic for a given entry: the dither noise is seeded per image and
    config, so re-rendering reproduces a capture's frame exactly — which is what
    lets a resumed run check a stored capture instead of trusting its filename.
    """
    cfg: ImageConfig = entry["config"]
    image = Path(entry["image"])
    render_display = display_for(entry, display)
    # "photo_first" enhances the photograph without its letterbox bars — the
    # fix under test in tools/letterbox.py — and "bars": "black" paints them
    # black instead of white. Anything else is production's order and colour.
    if entry.get("prepare") == "photo_first":
        from letterbox import photo_first_renderer  # noqa: PLC0415

        bar_ink = 0 if entry.get("bars") == "black" else 1
        renderer = photo_first_renderer(render_display, bar_ink=bar_ink)
    else:
        renderer = renderer_for(render_display)
    # Production's loader, so the glass shows what the Foyer screen would: it
    # applies the EXIF rotation that a bare Image.open ignores. Without it, 21
    # library photos went onto the panel sideways or upside down.
    # "autocontrast": "preserve_tone" / "off" swaps production's per-channel
    # autocontrast for the duration of this render (tools/prepare_variants.py).
    with (
        autocontrast_as(entry.get("autocontrast")),
        open_image_for_render(image) as img,
    ):
        # Same seed the metric bank used when it scored this candidate, so the
        # frame on the glass is the frame that was measured — the pipeline's
        # dither noise is otherwise drawn from an unseeded global RNG and every
        # render differs. Also makes a recapture reproduce the identical frame.
        np.random.seed(render_seed(image, cfg) % (2**32))
        # A plan built from production's classifier carries the decision's crop
        # threshold and face boxes (tools/production.py); render with them, as
        # render_worker.render_one does. Older plans carry none and render as before.
        idx_panel = renderer.render_indices(
            img.copy(),
            cfg,
            Orientation.LANDSCAPE,
            display.panel_w,
            display.panel_h,
            **render_kwargs(entry),
        )
    expected = (
        np.rint(display.palette_measured_rgb)
        .clip(0, 255)
        .astype(np.uint8)[to_visible(idx_panel, display)]
    )
    return idx_panel, expected


def stored_matches(entry: dict, display, outdir: Path) -> bool:
    """True if the stored capture for this entry is of exactly this render.

    A tag names an arm, not a render: two plans can share a tag with different
    configs, and the first version of this tool reused a capture on name alone —
    which silently put two default-preset renders into a production plan.
    Re-rendering (about two seconds) and comparing against the stored expected
    frame is exact, and needs no metadata that older captures would lack.
    """
    stored = cv2.imread(str(outdir / f"{entry['tag']}__expected.png"))
    if stored is None:
        return False
    _idx, expected = render_entry(entry, display)
    return np.array_equal(stored, cv2.cvtColor(expected, cv2.COLOR_RGB2BGR))


def set_aside(tag: str, outdir: Path) -> int:
    """Move a stale capture's files to outdir/superseded, keeping them."""
    dest = outdir / "superseded"
    dest.mkdir(exist_ok=True)
    moved = 0
    for path in outdir.glob(f"{glob_escape(tag)}__*"):
        if path.is_file():
            path.replace(dest / path.name)
            moved += 1
    return moved


def capture_one(
    entry: dict,
    display,
    rig: Rig,
    outdir: Path,
    port: str,
    flip180: bool,
    timeout: float,
    settle: int,
) -> dict:
    tag = entry["tag"]
    shot_path = outdir / f"{tag}__shot.jpg"
    measured_path = outdir / f"{tag}__measured.png"

    idx_panel, expected = render_entry(entry, display)
    cv2.imwrite(str(outdir / f"{tag}__expected.png"), cv2.cvtColor(expected, cv2.COLOR_RGB2BGR))

    if not push(idx_panel, display, display, port, flip180, timeout, interactive=True):
        raise RuntimeError("panel upload failed")
    cam_rig.capture_linear(shot_path, settle_ms=settle, verbose=False)

    shot = cam_rig.load_linear(shot_path)
    measured, info = rig.to_colorimetry(shot, expected, display)
    cv2.imwrite(str(measured_path), cv2.cvtColor(measured, cv2.COLOR_RGB2BGR))
    return {"tag": tag, "inliers": info["inliers"], "matches": info["matches"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--port", required=True)
    ap.add_argument("--bench-flip180", action="store_true")
    ap.add_argument("--console-timeout", type=float, default=180.0)
    ap.add_argument("--outdir", type=Path, default=Path("build/camcal/session"))
    ap.add_argument("--calib", type=Path, default=Path("build/camcal"))
    ap.add_argument("--settle", type=int, default=2500)
    ap.add_argument("--limit", type=int, default=0, help="first N candidates (smoke test)")
    ap.add_argument("--redo", action="store_true", help="recapture even if output exists")
    ap.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=5,
        help="give up if this many captures fail before any has succeeded — a "
        "dead rig otherwise spends a full panel refresh per failure, silently",
    )
    args = ap.parse_args(argv)

    display = DISPLAY_REGISTRY[args.model]
    args.outdir.mkdir(parents=True, exist_ok=True)
    candidates = load_plan(args.plan)
    if args.limit:
        candidates = candidates[: args.limit]

    rig = Rig(args.calib)
    print(f"  rig loaded (colour fit LOO {rig.loo_de:.2f} dE)")
    print(f"  {len(candidates)} candidates -> {args.outdir}")

    done, failed, skipped, superseded = [], [], 0, 0
    started = time.time()
    for i, entry in enumerate(candidates, 1):
        out = args.outdir / f"{entry['tag']}__measured.png"
        if out.exists() and not args.redo:
            if stored_matches(entry, display, args.outdir):
                skipped += 1
                continue
            # Same tag, different render: an older plan's capture. Keep it, but
            # never let it stand in for this one.
            set_aside(entry["tag"], args.outdir)
            superseded += 1
            print(
                f"  [{i}/{len(candidates)}] {entry['tag']}: stored capture is of a "
                f"different render — set aside, recapturing",
                flush=True,
            )
        label = f"[{i}/{len(candidates)}] {entry['tag']}"
        try:
            info = capture_one(
                entry,
                display,
                rig,
                args.outdir,
                args.port,
                args.bench_flip180,
                args.console_timeout,
                args.settle,
            )
            done.append(info)
            elapsed = time.time() - started
            rate = elapsed / max(len(done), 1)
            # Remaining = candidates after this one that are not already on disk.
            # Subtracting both `skipped` and `i` double-counts the skips and goes
            # negative on a resumed run.
            left = (
                sum(
                    1
                    for later in candidates[i:]
                    if not (args.outdir / f"{later['tag']}__measured.png").exists()
                )
                * rate
            )
            print(
                f"  {label}: ok ({info['inliers']}/{info['matches']} inliers)"
                f"  {rate:.0f}s/ea, ~{left / 60:.0f} min left",
                flush=True,
            )
        except Exception as exc:
            # Keep going. A single bad registration or a dropped serial link must
            # not throw away the hours of panel time already banked.
            failed.append({"tag": entry["tag"], "error": f"{type(exc).__name__}: {exc}"})
            print(f"  {label}: FAILED {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc(limit=2)
            # ...but a run where nothing succeeds is not bad luck, it is a dead
            # rig, and every attempt still costs a full panel refresh. A camera
            # Pi that dropped off the network burned 55 refreshes over 27 minutes
            # before anyone looked, because each capture failed individually and
            # the loop dutifully carried on. Stop and say so instead.
            if len(failed) >= args.max_consecutive_failures and not done:
                print(
                    f"\n  ABORTING: {len(failed)} captures in a row failed and none "
                    f"has succeeded. The rig is not working — check the camera "
                    f"({cam_rig.DEFAULT_HOST}) and the panel before retrying.",
                    flush=True,
                )
                break

    summary = {
        "captured": done,
        "failed": failed,
        "skipped": skipped,
        "superseded": superseded,
        "seconds": time.time() - started,
    }
    (args.outdir / "capture_log.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(
        f"\n  captured {len(done)}, failed {len(failed)}, skipped {skipped}"
        f" in {summary['seconds'] / 60:.1f} min"
        + (f" ({superseded} stale capture(s) set aside and redone)" if superseded else "")
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
