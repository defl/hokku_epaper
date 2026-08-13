#!/usr/bin/env python3
"""Blind side-by-side judging, recorded as data a metric can be scored against.

Every quality conclusion in this project has ultimately been settled by a human
looking at the glass, and three of them were reached by a metric first and
overturned afterwards. The way out is not a better metric guessed at in advance:
it is a set of verdicts to score candidate metrics AGAINST, so the metric earns
its authority instead of asserting it.

That only works if the verdicts are clean, which is where the existing
`f7_ab_compare.py` cannot help. It prints which side ships today and then says
"the claim is the left is bluer". Fine for confirming one hypothesis; fatal as
label data, because the verdicts would encode the hypothesis and any metric
fitted to them would look excellent and predict nothing.

So, deliberately:

  - **Blind.** Sides are assigned by seeded coin flip and never printed. The
    prompt says nothing about what differs or what to look for. A difference that
    only appears once pointed at is not a difference worth shipping.
  - **"Can't tell" is a first-class answer.** Ties are the most informative
    trials there are — a metric must predict a SMALL value where you saw nothing.
    Forcing a binary choice injects coin flips into exactly the region where
    candidate metrics are separated.
  - **Repeats.** A fraction of trials come back later with sides re-randomised.
    Your agreement with yourself is the ceiling on what any metric can score, and
    without it "the metric is bad" cannot be told from "these look identical".
  - **Catch trials.** A few pairs differ blatantly. Missing them means the
    session was fatigued and the data should be thrown away, which is worth
    knowing before it is analysed rather than after.

The tonal chain stays bypassed, as in `f7_ab_compare`: production runs
autocontrast, CLAHE and DRC ahead of the dither, and including them would
confound every arm with stages nothing here is testing.

Two phases, because the trial list must be reviewable before any panel time is
spent, and because the plan file holds the side mapping that the run must not
reveal:

  python tools/ab_session.py plan --out build/colorcal/ab_plan.json
  python tools/ab_session.py run  --plan build/colorcal/ab_plan.json --port COM10
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import random
import sys
import time
from pathlib import Path

import numpy as np
import serial
from PIL import Image

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from color_measure_f7 import catch_console, upload_with_retry
from color_model import INK_NAMES, load_records
from color_under_illuminant import (
    ambient_palette_srgb,
    cmfs_at,
    illuminant_at,
    ink_spectra,
)
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_config import DitherConfig
from hokku.webserver.dither_streaming import dither

# The shipping general-photo dither, read off presets.py rather than remembered.
# hue_cutoff_deg and neutral_chroma ARE the hue-aware gate: at the shipped 95/8
# `hue_aware` is bit-identical to `euclidean` on skin, while at the 30/12 used by
# an earlier analysis it is not. Three wrong conclusions came from that gap, so
# these are never defaulted to anything else.
PRODUCTION = DitherConfig("atkinson", "hue_aware", True, 95.0, 8.0)

# Content chosen so each arm meets the failure mode it is most likely to expose.
# A LUT change that helps skin can wreck neutrals, which is exactly how the
# cam16ucs recommendation died, so no arm is judged on portraits alone.
CONTENT = {
    "skin_deniro": "images/test/Robert_De_Niro_KVIFF_portrait.jpg",
    "skin_unterberger": "images/test/Actress_Anna_Unterberger-2.jpg",
    "skin_wayuu": "images/test/Wayuu_woman_with_sad_face_in_the_market_buying.jpg",
    "neutral_bar": "images/test/grayscale_linear_bar_1200x300.png",
    "neutral_forest": "images/test/Forest_road_Slavne_2017_BW_G9.jpg",
    "gamut_rgb": "images/test/RGB_corner_gradient_bilinear_1200.png",
    "gamut_albi": "images/test/Albi_Panorama_Sunset_Panini_General.heif",
    "detail_hare": "images/test/Albrecht_Duerer_Hare_1502_Google_Art_Project.jxl",
    "line_logo": "images/test/Wikipedia-logo-v2-webp.webp",
}


def crop_to(img: Image.Image, w: int, h: int) -> Image.Image:
    """Centre-crop to the target aspect, then resize — never squash.

    Both halves must show the SAME pixels or the comparison means nothing, so the
    crop is computed once per trial and shared.
    """
    tw = w / h
    if img.width / img.height > tw:
        new_w = int(img.height * tw)
        img = img.crop(((img.width - new_w) // 2, 0, (img.width + new_w) // 2, img.height))
    else:
        new_h = int(img.width / tw)
        img = img.crop((0, (img.height - new_h) // 2, img.width, (img.height + new_h) // 2))
    return img.resize((w, h), Image.Resampling.LANCZOS)


def _variant_display(base, suffix: str, palette: np.ndarray):
    """A Display carrying a substitute palette, registered so the LUT cache finds it.

    Two non-obvious requirements, both learned the hard way:

    ``model_id`` MUST differ. Every palette LUT is memoised on it
    (``_cached_hue_aware_lut(model_id, ...)``), so a variant keeping the base id
    would be handed the base LUT straight out of cache — the arm would render
    identically to production while appearing to work.

    The variant MUST also be in ``DISPLAY_REGISTRY``. The cached builders take a
    model_id and re-resolve the Display from the registry rather than closing
    over the object passed in, so an unregistered variant raises KeyError deep
    inside the LUT build.
    """
    var = copy.copy(base)
    var.palette_measured_rgb = np.asarray(palette, dtype=np.float32)
    var.model_id = f"{base.model_id}__{suffix}"
    DISPLAY_REGISTRY[var.model_id] = var
    return var


def _measured_palettes(ambient_path: Path, data_path: Path) -> tuple[np.ndarray, np.ndarray]:
    """(under D65, under the measured room light) — both from OUR spectra.

    Both are computed the same way on purpose. Production's palette comes from a
    third-party table (epdoptimize) whose white sits at L* 79.3 against this
    glass's real 67.96, so pitting an ambient-derived palette against production
    would vary the illuminant AND swap the whole provenance of the numbers at the
    same time, then report the sum as an illuminant effect. Comparing
    measured-D65 against measured-ambient changes exactly one thing.
    """
    recs = load_records(data_path)
    wl, inks = ink_spectra(recs)
    cmf = cmfs_at(wl)
    amb = [
        json.loads(x) for x in ambient_path.read_text(encoding="utf-8").splitlines() if x.strip()
    ][-1]
    if not amb.get("median_spectrum"):
        raise SystemExit(f"{ambient_path} has no spectrum — measure it with color_ambient.py")
    d65 = illuminant_at("D65", wl)
    amb_spd = np.asarray(amb["median_spectrum"], dtype=float)
    return (
        ambient_palette_srgb(inks, INK_NAMES, d65, cmf),
        ambient_palette_srgb(inks, INK_NAMES, amb_spd, cmf),
    )


def build_arms(display, args) -> dict:
    """name -> (render callable, human description). Production is arm zero."""
    tone = json.loads(Path(args.tone_curves).read_text(encoding="utf-8"))
    closed = np.array(tone["closed_loop"]["lut"], dtype=np.uint8)
    naive = np.array(tone["naive_open_loop"]["lut"], dtype=np.uint8)

    def plain(cfg, disp=None):
        return lambda src: dither(src.copy(), cfg, disp or display)

    def toned(lut):
        # Applied per channel before the dither, which is where a transfer curve
        # belongs: after it, the pixel is already an ink index and nothing is left
        # to correct.
        return lambda src: dither(lut[src.copy()], PRODUCTION, display)

    arms = {
        "production": (plain(PRODUCTION), "atkinson/hue_aware 95/8 serpentine — ships today"),
        "tone_closed": (toned(closed), "production + closed-loop tone curve"),
        "tone_naive": (toned(naive), "production + naive open-loop tone curve"),
        "euclidean": (
            plain(DitherConfig("atkinson", "euclidean", True, 95.0, 8.0)),
            "plain CIELAB",
        ),
        "oklab": (plain(DitherConfig("atkinson", "oklab", True, 95.0, 8.0)), "OKLAB nearest"),
        "cam16ucs": (plain(DitherConfig("atkinson", "cam16ucs", True, 95.0, 8.0)), "CAM16-UCS"),
        "bw": (plain(DitherConfig("atkinson", "bw", True, 95.0, 8.0)), "two inks — CATCH TRIAL"),
    }
    if args.ambient and Path(args.ambient).exists():
        pal_d65, pal_amb = _measured_palettes(Path(args.ambient), Path(args.data))
        arms["pal_d65"] = (
            plain(PRODUCTION, _variant_display(display, "meas_d65", pal_d65)),
            "our measured palette under D65",
        )
        arms["pal_ambient"] = (
            plain(PRODUCTION, _variant_display(display, "meas_amb", pal_amb)),
            "our measured palette under the measured room light",
        )
    return arms


# Explicit pairs rather than "everything against production", because the
# illuminant question needs both sides to share a provenance — see
# _measured_palettes. Everything else is judged against what ships today.
CONTRASTS = [
    ("tone_closed", "production", ["skin_deniro", "neutral_bar", "gamut_albi", "detail_hare"]),
    ("tone_naive", "production", ["skin_deniro", "neutral_bar", "gamut_albi", "detail_hare"]),
    ("tone_closed", "tone_naive", ["neutral_bar", "gamut_albi"]),
    # The one arm where production is NOT the reference: both sides are our own
    # measured palette, differing only in the illuminant it was computed under.
    ("pal_ambient", "pal_d65", ["skin_unterberger", "gamut_rgb", "neutral_forest", "gamut_albi"]),
    # Known prior verdicts — a human already rejected all three on sight. Any
    # candidate metric must reproduce that, so these calibrate it.
    ("euclidean", "production", ["skin_deniro", "neutral_bar"]),
    ("oklab", "production", ["skin_wayuu", "neutral_bar", "detail_hare"]),
    ("cam16ucs", "production", ["skin_unterberger", "gamut_rgb"]),
    ("bw", "production", ["gamut_rgb", "skin_deniro"]),
]


def plan(args) -> int:
    display = DISPLAY_REGISTRY[args.model]
    arms = build_arms(display, args)
    rng = random.Random(args.seed)  # noqa: S311 — trial ordering, not secrecy

    trials = []
    for arm_a, arm_b, keys in CONTRASTS:
        if arm_a not in arms or arm_b not in arms:
            print(f"  skipping {arm_a} vs {arm_b} — arm unavailable (no ambient measurement?)")
            continue
        for key in keys:
            trials.append(
                {
                    "arm_a": arm_a,
                    "arm_b": arm_b,
                    "content": key,
                    "kind": "catch" if "bw" in (arm_a, arm_b) else "main",
                }
            )

    rng.shuffle(trials)

    # Repeats go in AFTER the shuffle and land in the back half, so a repeat never
    # sits next to its original where memory rather than vision would answer it.
    n_rep = max(1, round(len(trials) * args.repeat_frac))
    for t in rng.sample([t for t in trials if t["kind"] == "main"], n_rep):
        rep = dict(t, kind="repeat", repeat_of=trials.index(t))
        trials.insert(rng.randrange(len(trials) // 2, len(trials) + 1), rep)

    for i, t in enumerate(trials):
        t["trial"] = i
        # The coin flip lives here and is never shown by `run`. Re-rolled per
        # trial, so a repeat gets an independent assignment and agreeing with
        # yourself cannot be an artefact of the sides having stayed put.
        t["a_side"] = "left" if rng.random() < 0.5 else "right"
        t["image"] = CONTENT[t["content"]]

    out = {
        "model": args.model,
        "seed": args.seed,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "arm_descriptions": {k: v[1] for k, v in arms.items()},
        "trials": trials,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")

    print(f"{len(trials)} trials -> {args.out}")
    print(f"  main   {sum(1 for t in trials if t['kind'] == 'main')}")
    print(f"  repeat {sum(1 for t in trials if t['kind'] == 'repeat')}  (your noise ceiling)")
    print(f"  catch  {sum(1 for t in trials if t['kind'] == 'catch')}")
    print(f"\n~{len(trials) * 45 / 60:.0f} min of panel time, plus judging.")
    print("Do NOT read the plan file — it contains which side is which.")
    return 0


def run(args) -> int:
    plan_doc = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    display = DISPLAY_REGISTRY[plan_doc["model"]]
    arms = build_arms(display, args)
    W, H = display.panel_w, display.panel_h
    half = W // 2

    out = Path(args.out)
    done = set()
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    done.add(json.loads(line)["trial"])
                except (json.JSONDecodeError, KeyError):
                    continue
    todo = [t for t in plan_doc["trials"] if t["trial"] not in done]
    if not todo:
        print("every trial already judged")
        return 0
    print(f"{len(todo)} trials left of {len(plan_doc['trials'])}\n")

    s = catch_console(args.port, args.console_timeout)
    if s is None:
        print("console never answered — long-press the power button and retry")
        return 1

    try:
        with out.open("a", encoding="utf-8") as fh:
            for n, t in enumerate(todo, 1):
                img = Image.open(t["image"]).convert("RGB")
                src = np.array(crop_to(img, half, H), dtype=np.uint8)

                left_arm = t["arm_a"] if t["a_side"] == "left" else t["arm_b"]
                right_arm = t["arm_b"] if t["a_side"] == "left" else t["arm_a"]
                raster = np.zeros((H, W), dtype=np.uint8)
                raster[:, :half] = arms[left_arm][0](src)
                raster[:, half:] = arms[right_arm][0](src)
                if args.divider:
                    c = half - args.divider // 2
                    raster[:, c : c + args.divider] = 0

                data = display.indices_to_panel_bytes(raster)
                print(f"[{n}/{len(todo)}] uploading...", flush=True)
                if not upload_with_retry(s, data, f"trial {t['trial']}", attempts=6, gap_s=8.0):
                    print("  upload failed — skipping, it will come back on resume")
                    continue

                # Nothing here says what differs, which side ships, or what to
                # look at. That silence is the instrument.
                print(f"\n  Trial {n}/{len(todo)}.  Which half looks better?")
                verdict = ""
                while verdict not in ("l", "r", "?"):
                    verdict = (
                        input("    [l]eft  [r]ight  [?] can't tell  (q to stop): ").strip().lower()
                    )
                    if verdict == "q":
                        print("stopped — everything judged so far is on disk")
                        return 0
                reason = input("    why (optional): ").strip()

                chose = {"l": "left", "r": "right", "?": "tie"}[verdict]
                rec = {
                    "trial": t["trial"],
                    "kind": t["kind"],
                    "arm_a": t["arm_a"],
                    "arm_b": t["arm_b"],
                    "content": t["content"],
                    "chose_side": chose,
                    # Resolved here rather than at scoring time so the file is
                    # self-contained and a lost plan cannot orphan the verdicts.
                    "winner": (
                        "tie" if chose == "tie" else (left_arm if chose == "left" else right_arm)
                    ),
                    "reason": reason,
                    "raster_sha1": hashlib.sha1(data, usedforsecurity=False).hexdigest()[:16],
                    "t_unix": time.time(),
                }
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
    except KeyboardInterrupt:
        print("\ninterrupted — judged trials are already on disk")
    finally:
        try:
            s.close()
        except (serial.SerialException, OSError):
            pass
    print(f"\nverdicts -> {out}")
    return 0


def serve(args) -> int:
    """Judge trials without a terminal: verdicts arrive through a control file.

    Same experiment as ``run``, different input channel. It exists because the
    judging and the driving can be two different people (or a person and an
    agent), and because the serial console CANNOT be reopened per trial — it
    "only lives a few seconds per wake" (see color_measure_f7.catch_console), so
    the campaign held one session for a whole cycle and so does this. A
    trial-per-invocation design would spend its first minutes re-catching a
    console that may never come back.

    The status file deliberately carries the trial number and nothing else. No
    arm names, no side assignment — so whoever is driving stays as blind as the
    person judging, and cannot leak a hint while relaying the question.
    """
    plan_doc = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    display = DISPLAY_REGISTRY[plan_doc["model"]]
    arms = build_arms(display, args)
    W, H = display.panel_w, display.panel_h
    half = W // 2

    out, control, status = Path(args.out), Path(args.control), Path(args.status)
    done = set()
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    done.add(json.loads(line)["trial"])
                except (json.JSONDecodeError, KeyError):
                    continue
    todo = [t for t in plan_doc["trials"] if t["trial"] not in done]
    if not todo:
        status.write_text(json.dumps({"state": "finished"}), encoding="utf-8")
        print("every trial already judged")
        return 0

    s = catch_console(args.port, args.console_timeout)
    if s is None:
        status.write_text(json.dumps({"state": "no_console"}), encoding="utf-8")
        print("console never answered — long-press the power button and retry")
        return 1

    try:
        with out.open("a", encoding="utf-8") as fh:
            for n, t in enumerate(todo, 1):
                img = Image.open(t["image"]).convert("RGB")
                src = np.array(crop_to(img, half, H), dtype=np.uint8)
                left_arm = t["arm_a"] if t["a_side"] == "left" else t["arm_b"]
                right_arm = t["arm_b"] if t["a_side"] == "left" else t["arm_a"]

                raster = np.zeros((H, W), dtype=np.uint8)
                raster[:, :half] = arms[left_arm][0](src)
                raster[:, half:] = arms[right_arm][0](src)
                if args.divider:
                    c = half - args.divider // 2
                    raster[:, c : c + args.divider] = 0

                data = display.indices_to_panel_bytes(raster)
                print(f"[{n}/{len(todo)}] trial {t['trial']}: uploading...", flush=True)
                if not upload_with_retry(s, data, f"trial {t['trial']}", attempts=6, gap_s=8.0):
                    print("  upload failed — will retry on resume")
                    continue

                status.write_text(
                    json.dumps(
                        {
                            "state": "awaiting_verdict",
                            "trial": t["trial"],
                            "index": n,
                            "total": len(todo),
                        }
                    ),
                    encoding="utf-8",
                )
                print(f"  on glass — awaiting verdict for trial {t['trial']}", flush=True)

                deadline = time.monotonic() + args.verdict_timeout
                verdict = None
                while time.monotonic() < deadline:
                    if control.exists():
                        try:
                            c = json.loads(control.read_text(encoding="utf-8"))
                        except json.JSONDecodeError:
                            time.sleep(args.poll_s)  # a half-written file, not an error
                            continue
                        # Matching on the trial id is what stops a stale verdict
                        # from the previous trial being applied to this one.
                        if c.get("trial") == t["trial"] and c.get("verdict") in ("l", "r", "?"):
                            verdict = c
                            break
                    time.sleep(args.poll_s)
                if verdict is None:
                    print("  timed out waiting for a verdict — stopping cleanly")
                    break

                chose = {"l": "left", "r": "right", "?": "tie"}[verdict["verdict"]]
                fh.write(
                    json.dumps(
                        {
                            "trial": t["trial"],
                            "kind": t["kind"],
                            "arm_a": t["arm_a"],
                            "arm_b": t["arm_b"],
                            "content": t["content"],
                            "chose_side": chose,
                            "winner": (
                                "tie"
                                if chose == "tie"
                                else (left_arm if chose == "left" else right_arm)
                            ),
                            "reason": verdict.get("reason", ""),
                            "raster_sha1": hashlib.sha1(data, usedforsecurity=False).hexdigest()[
                                :16
                            ],
                            "t_unix": time.time(),
                        }
                    )
                    + "\n"
                )
                fh.flush()
                # Never echo the winner: the driver must not learn the mapping.
                print(f"  recorded trial {t['trial']}", flush=True)
    except KeyboardInterrupt:
        print("\ninterrupted — judged trials are on disk")
    finally:
        try:
            s.close()
        except (serial.SerialException, OSError):
            pass
    status.write_text(json.dumps({"state": "stopped"}), encoding="utf-8")
    print(f"\nverdicts -> {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--model", default="bigme_f7")
    common.add_argument("--tone-curves", default="build/colorcal/tone_curves.json")
    common.add_argument("--ambient", default="build/colorcal/ambient.jsonl")
    common.add_argument("--data", default="docs/screens/bigme_f7/measurements/data/campaign.jsonl")

    p = sub.add_parser("plan", parents=[common])
    p.add_argument("--out", default="build/colorcal/ab_plan.json")
    p.add_argument("--seed", type=int, default=20260811)
    p.add_argument("--repeat-frac", type=float, default=0.2)

    r = sub.add_parser("run", parents=[common])
    r.add_argument("--plan", default="build/colorcal/ab_plan.json")
    r.add_argument("--out", default="build/colorcal/ab_verdicts.jsonl")
    r.add_argument("--port", default="COM10")
    r.add_argument("--console-timeout", type=float, default=900.0)
    r.add_argument("--divider", type=int, default=2)

    v = sub.add_parser("serve", parents=[common])
    v.add_argument("--plan", default="build/colorcal/ab_plan.json")
    v.add_argument("--out", default="build/colorcal/ab_verdicts.jsonl")
    v.add_argument("--control", default="build/colorcal/ab_control.json")
    v.add_argument("--status", default="build/colorcal/ab_status.json")
    v.add_argument("--port", default="COM10")
    v.add_argument("--console-timeout", type=float, default=900.0)
    v.add_argument("--divider", type=int, default=2)
    v.add_argument("--poll-s", type=float, default=1.0)
    v.add_argument("--verdict-timeout", type=float, default=1800.0)

    args = ap.parse_args(argv)
    return {"plan": plan, "run": run, "serve": serve}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
