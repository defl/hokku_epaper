#!/usr/bin/env python3
"""Build the patch list for a large panel-characterisation campaign.

Deterministic and hardware-free: the same seed always yields the same patches in
the same order, so a campaign can be planned, reviewed and re-derived without a
device. ``color_campaign_run.py`` consumes this and does the measuring.

Every patch is a WHOLE-PANEL field, because the instrument is clamped in one
place and cannot be re-aimed (see ``color_measure_f7``). One patch therefore
costs one full panel refresh — about 56 s — and that is the budget everything
else is traded against.

Two ways a patch gets its pixels, and the distinction matters:

  ``bayer``   the 8x8 ordered matrix thresholded directly, giving an EXACTLY
              known black-ink area fraction (k/64). This is the reference: it
              measures the panel, with no pipeline in the loop.
  ``pipeline``the real ``dither()`` call on a flat colour, so it measures what
              the renderer actually delivers for a requested sRGB.

Patches are generated per phase and then shuffled WITHIN each phase. Instrument
and panel drift over an 8-hour run are slow, so measuring a factor's levels in
order would alias drift straight onto that factor; randomising decorrelates them
and the interleaved control patches measure what drift remains.

The tonal chain is deliberately NOT used. A flat colour through the full pipeline
hits autocontrast, which has no dynamic range to work with on a uniform field and
destroys the patch — the same trap that produced a bogus 0.00 % result in the
earlier dither search. Only ``dither()`` runs here.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_config import DitherConfig

# Phase 2 asks whether dot gain depends on the SPATIAL pattern, not just the ink
# fraction. Clustered dots have less perimeter per unit area than dispersed ones,
# so if the panel's ink spreads at all these must measure differently at matched
# coverage — which would make algorithm choice a colorimetric decision, not just
# a texture one.
ED_ALGORITHMS = ("floyd_steinberg", "atkinson", "stucki")

# Configs used to render colour patches. Chosen to span the axes the earlier
# dither search found mattered most (LUT colour space), not to be exhaustive.
COLOUR_CONFIGS: tuple[tuple[str, DitherConfig], ...] = (
    (
        "atkinson_hue_aware",
        DitherConfig("atkinson", "hue_aware", True, 30.0, 12.0),
    ),
    (
        "atkinson_oklab_hue_aware",
        DitherConfig("atkinson", "oklab_hue_aware", True, 30.0, 12.0),
    ),
    (
        "fs_cam16ucs_hue_aware",
        DitherConfig("floyd_steinberg", "cam16ucs_hue_aware", True, 30.0, 12.0),
    ),
)

GAMUT_CONFIG = COLOUR_CONFIGS[0]


INK_NAMES = ("black", "white", "yellow", "red", "blue", "green")

# How many times each solid ink is re-measured across the campaign. These are the
# PRIMARIES of the area-coverage model — every ink fraction in the dataset is a
# weight on them — so they are measured repeatedly and spread through the run
# rather than once at the start, making primary drift observable.
INK_REPEATS = 3


@dataclass
class Patch:
    """One measurable full-panel field."""

    uid: str
    phase: str
    source: str  # "bayer" | "ink" | "pipeline"
    label: str
    # bayer patches
    bayer_k: int | None = None  # black cells out of 64
    # solid-ink patches
    ink_index: int | None = None
    # pipeline patches
    rgb: tuple[int, int, int] | None = None
    config_name: str | None = None
    config: dict | None = None
    # bookkeeping
    is_control: bool = False
    meta: dict = field(default_factory=dict)


def _uid(n: int) -> str:
    return f"p{n:05d}"


# ── colour sets ───────────────────────────────────────────────────────────────


def skin_locus_rgb(n_target: int, seed: int) -> list[tuple[int, int, int]]:
    """Sample sRGB colours from the human-skin region of CIELAB.

    Skin occupies a narrow, well-documented band: lightness spans roughly
    L* 20-90 across the range of human complexions, hue sits in the orange
    sector, and chroma rises then falls with lightness. Sampling in Lab rather
    than RGB keeps the set perceptually even instead of clustering in the bright
    end the way an RGB grid would.

    Deliberately includes the deep end (L* < 35), which generic test charts
    under-sample and which is exactly where a 31.9:1 panel struggles most.
    """
    # Reproducibility, not secrecy: the seed must regenerate the identical campaign.
    rng = random.Random(seed)  # noqa: S311
    out: list[tuple[int, int, int]] = []
    seen: set[tuple[int, int, int]] = set()
    # (L*, a*, b*) ranges for the skin band; b* > a* throughout (orange, not red).
    guard = 0
    while len(out) < n_target and guard < n_target * 400:
        guard += 1
        lightness = rng.uniform(18.0, 90.0)
        # Chroma peaks in the mid-lightness range and falls off at both ends.
        peak = 1.0 - abs((lightness - 55.0) / 55.0)
        chroma = rng.uniform(6.0, 12.0 + 30.0 * max(peak, 0.0))
        hue_deg = rng.uniform(20.0, 68.0)  # orange sector: ruddy -> olive
        a = chroma * np.cos(np.radians(hue_deg))
        b = chroma * np.sin(np.radians(hue_deg))
        rgb = _lab_to_srgb8(lightness, a, b)
        if rgb is None:
            continue  # out of gamut
        if rgb in seen:
            continue
        seen.add(rgb)
        out.append(rgb)
    return sorted(out)


def _lab_to_srgb8(lightness: float, a: float, b: float) -> tuple[int, int, int] | None:
    """CIELAB (D65) -> 8-bit sRGB, or None if the colour is out of gamut."""
    fy = (lightness + 16.0) / 116.0
    fx, fz = fy + a / 500.0, fy - b / 200.0

    def finv(t: float) -> float:
        return t**3 if t**3 > 216 / 24389 else (116 * t - 16) * 27 / 24389

    white = np.array([0.95047, 1.0, 1.08883])
    xyz = np.array([finv(fx), finv(fy), finv(fz)]) * white
    m = np.array(
        [
            [3.2406, -1.5372, -0.4986],
            [-0.9689, 1.8758, 0.0415],
            [0.0557, -0.2040, 1.0570],
        ]
    )
    lin = m @ xyz
    if np.any(lin < -1e-4) or np.any(lin > 1.0 + 1e-4):
        return None  # genuinely outside sRGB — do not silently clip
    lin = np.clip(lin, 0.0, 1.0)
    s = np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * np.power(lin, 1 / 2.4) - 0.055)
    return tuple(int(v) for v in np.rint(s * 255))  # type: ignore[return-value]


def gray_ramp_rgb(n: int) -> list[tuple[int, int, int]]:
    """Neutral grays evenly spaced in L*, not in code value.

    Even steps in sRGB code value crowd the shadows perceptually; even steps in
    L* put the samples where the eye — and the dot-gain arch — actually vary.
    """
    out = []
    for i in range(n):
        lightness = 100.0 * i / (n - 1)
        rgb = _lab_to_srgb8(lightness, 0.0, 0.0)
        if rgb is not None:
            out.append(rgb)
    return out


def gamut_cube_rgb(steps: int) -> list[tuple[int, int, int]]:
    vals = [round(255 * i / (steps - 1)) for i in range(steps)]
    return [(r, g, b) for r in vals for g in vals for b in vals]


# ── phases ────────────────────────────────────────────────────────────────────


def build_patches(
    *,
    seed: int,
    gamut_steps: int,
    n_skin: int,
    control_every: int,
    campaign: int = 1,
) -> list[Patch]:
    # Reproducibility, not secrecy: the seed must regenerate the identical campaign.
    rng = random.Random(seed)  # noqa: S311
    counter = 0

    def nxt() -> str:
        nonlocal counter
        counter += 1
        return _uid(counter)

    phases: list[list[Patch]] = []

    if campaign == 2:
        phases.extend(_campaign2_phases(nxt, gamut_steps=gamut_steps, n_skin=n_skin, seed=seed))
        return _interleave(phases, nxt, rng, control_every)

    if campaign == 3:
        phases.extend(_merged_phases(nxt, gamut_steps=gamut_steps, n_skin=n_skin, seed=seed))
        return _interleave(phases, nxt, rng, control_every)

    # Phase 0 — the six solid inks, the model's primaries. Repeated and shuffled
    # through the campaign so a drifting primary shows up as spread between its
    # own repeats rather than silently biasing every fitted coverage.
    p0 = [
        Patch(
            uid=nxt(),
            phase="inks",
            source="ink",
            label=f"ink {INK_NAMES[i]} r{rep + 1}",
            ink_index=i,
        )
        for rep in range(INK_REPEATS)
        for i in range(len(INK_NAMES))
    ]
    phases.append(p0)

    # Phase 1 — fine tone response. Bayer k/64 gives an exactly known coverage at
    # every step, turning the 7-point arch measured earlier into a real curve.
    p1 = [
        Patch(uid=nxt(), phase="tone_fine", source="bayer", label=f"bayer {k}/64", bayer_k=k)
        for k in range(65)
    ]
    phases.append(p1)

    # Phase 2 — is dot gain pattern-dependent? Matched nominal coverage, different
    # spatial structure. Bayer is the clustered-ish reference; the error-diffusion
    # algorithms disperse differently, and serpentine changes the scan artefacts.
    p2: list[Patch] = []
    levels = gray_ramp_rgb(13)
    for k in range(1, 14):  # 13 matched coverages for the bayer arm
        p2.append(
            Patch(
                uid=nxt(),
                phase="algo_gain",
                source="bayer",
                label=f"bayer {k * 4}/64",
                bayer_k=k * 4,
            )
        )
    for algo in ED_ALGORITHMS:
        for serp in (False, True):
            cfg = DitherConfig(algo, "bw", serp, 30.0, 12.0)
            for rgb in levels:
                p2.append(
                    Patch(
                        uid=nxt(),
                        phase="algo_gain",
                        source="pipeline",
                        label=f"{algo}{'_serp' if serp else ''} gray{rgb[0]}",
                        rgb=rgb,
                        config_name=f"{algo}{'_serp' if serp else ''}_bw",
                        config=asdict(cfg),
                    )
                )
    phases.append(p2)

    # Phase 3 — skin. The priority: these are the colours whose errors people
    # actually notice, and the deep end is where this panel is weakest.
    p3: list[Patch] = []
    for rgb in skin_locus_rgb(n_skin, seed):
        for name, cfg in COLOUR_CONFIGS:
            p3.append(
                Patch(
                    uid=nxt(),
                    phase="skin",
                    source="pipeline",
                    label=f"skin {rgb} {name}",
                    rgb=rgb,
                    config_name=name,
                    config=asdict(cfg),
                )
            )
    phases.append(p3)

    # Phase 4 — gamut cube, one config, for a general correction LUT.
    name, cfg = GAMUT_CONFIG
    p4 = [
        Patch(
            uid=nxt(),
            phase="gamut",
            source="pipeline",
            label=f"gamut {rgb}",
            rgb=rgb,
            config_name=name,
            config=asdict(cfg),
        )
        for rgb in gamut_cube_rgb(gamut_steps)
    ]
    phases.append(p4)

    return _interleave(phases, nxt, rng, control_every)


# Campaign 1's algo_gain held the LUT at "bw" on the assumption that a neutral
# grey lands on black and white ink whatever the colour space. That is FALSE, and
# measurably so: at grey 119 with atkinson+serpentine,
#
#   bw                  black .447  white .553
#   hue_aware           black .260  white .486  red .088  green .166
#   oklab_hue_aware     black .216  white .466  red .053  green .265
#   cam16ucs_hue_aware  black .022  white .416  red .174  blue .066  green .322
#
# The hue-aware LUTs deliberately build neutrals out of chromatic ink — up to 56%
# of the panel. So campaign 1's gain curves describe the `bw` preset only, while
# `general` and `faces` ship `hue_aware`, whose gain is unmeasured. Worse, a single
# black-coverage curve is the wrong model when five inks are in play; the fit has
# to be multi-ink, which needs the per-LUT ink histograms this phase collects.
LUT_GAIN_LUTS = ("bw", "hue_aware", "oklab_hue_aware", "cam16ucs_hue_aware")


def _merged_phases(nxt, *, gamut_steps: int, n_skin: int, seed: int) -> list[list[Patch]]:
    """Everything worth measuring, de-duplicated, in descending order of value.

    Splitting this across two campaigns only makes sense when the first is
    already measured. Starting from scratch it is strictly worse: the coarse 5x5x5
    gamut is superseded by the dense one, and a second campaign pays for its own
    ink anchors and drift controls a second time.

    Phases run in the order listed, and the runner is resumable, so this also
    front-loads the answers: the tone curve, the per-LUT gain and the skin work
    all land before the 9x9x9 gamut sweep — which is the one phase that can be
    abandoned part-way without invalidating anything else.
    """
    phases: list[list[Patch]] = []
    levels = gray_ramp_rgb(13)

    # 1. Primaries — the basis everything else is expressed in.
    phases.append(
        [
            Patch(
                uid=nxt(),
                phase="inks",
                source="ink",
                label=f"ink {INK_NAMES[i]} r{rep + 1}",
                ink_index=i,
            )
            for rep in range(INK_REPEATS)
            for i in range(len(INK_NAMES))
        ]
    )

    # 2. Fine tone response — exact known coverage, no pipeline in the loop.
    phases.append(
        [
            Patch(uid=nxt(), phase="tone_fine", source="bayer", label=f"bayer {k}/64", bayer_k=k)
            for k in range(65)
        ]
    )

    # 3. Per-LUT gain on the SHIPPING algorithm. Highest value: general and faces
    #    both run hue_aware, whose gain campaign 1 never measured.
    p_lut: list[Patch] = []
    for lut in LUT_GAIN_LUTS:
        cfg = DitherConfig("atkinson", lut, True, 30.0, 12.0)
        p_lut += [
            Patch(
                uid=nxt(),
                phase="lut_gain",
                source="pipeline",
                label=f"atkinson_serp {lut} gray{rgb[0]}",
                rgb=rgb,
                config_name=f"atkinson_serp_{lut}",
                config=asdict(cfg),
            )
            for rgb in levels
        ]
    phases.append(p_lut)

    # 4. Pattern dependence: ordered vs diffused, and scan order.
    p_algo: list[Patch] = [
        Patch(
            uid=nxt(),
            phase="algo_gain",
            source="bayer",
            label=f"bayer {k * 4}/64",
            bayer_k=k * 4,
        )
        for k in range(1, 14)
    ]
    for algo in ED_ALGORITHMS:
        for serp in (False, True):
            cfg = DitherConfig(algo, "bw", serp, 30.0, 12.0)
            p_algo += [
                Patch(
                    uid=nxt(),
                    phase="algo_gain",
                    source="pipeline",
                    label=f"{algo}{'_serp' if serp else ''} gray{rgb[0]}",
                    rgb=rgb,
                    config_name=f"{algo}{'_serp' if serp else ''}_bw",
                    config=asdict(cfg),
                )
                for rgb in levels
            ]
    phases.append(p_algo)

    # 5. Is the LUT effect separable from the diffusion kernel?
    cfg_fs = DitherConfig("floyd_steinberg", "hue_aware", True, 30.0, 12.0)
    phases.append(
        [
            Patch(
                uid=nxt(),
                phase="lut_algo_cross",
                source="pipeline",
                label=f"fs_serp hue_aware gray{rgb[0]}",
                rgb=rgb,
                config_name="fs_serp_hue_aware",
                config=asdict(cfg_fs),
            )
            for rgb in levels
        ]
    )

    # 6. Skin — both sample sets, drawn from different seeds so they are distinct
    #    colours rather than a repeat of the same 60.
    skin_rgbs = skin_locus_rgb(60, seed) + skin_locus_rgb(n_skin, seed + 1)
    seen: set[tuple[int, int, int]] = set()
    skin_unique = [r for r in skin_rgbs if not (r in seen or seen.add(r))]
    phases.append(
        [
            Patch(
                uid=nxt(),
                phase="skin",
                source="pipeline",
                label=f"skin {rgb} {cname}",
                rgb=rgb,
                config_name=cname,
                config=asdict(ccfg),
            )
            for rgb in skin_unique
            for cname, ccfg in COLOUR_CONFIGS
        ]
    )

    # 7. Dense gamut LAST: the longest phase, and the only one that can be cut
    #    short without invalidating anything measured before it.
    name, cfg = GAMUT_CONFIG
    phases.append(
        [
            Patch(
                uid=nxt(),
                phase="gamut_dense",
                source="pipeline",
                label=f"gamut {rgb}",
                rgb=rgb,
                config_name=name,
                config=asdict(cfg),
            )
            for rgb in gamut_cube_rgb(gamut_steps)
        ]
    )
    return phases


def _campaign2_phases(nxt, *, gamut_steps: int, n_skin: int, seed: int) -> list[list[Patch]]:
    """Phases for the follow-up campaign.

    Two jobs campaign 1 could not do: measure gain for the LUTs actually shipped,
    and sample the gamut densely enough to invert into a real 3-D correction.
    """
    phases: list[list[Patch]] = []
    levels = gray_ramp_rgb(13)

    # LUT x gain. Algorithm pinned to atkinson+serpentine — the production choice —
    # so the LUT is the only thing varying. Bayer at matched coverage stays in as
    # the pipeline-free reference.
    p_lut: list[Patch] = [
        Patch(
            uid=nxt(),
            phase="lut_gain",
            source="bayer",
            label=f"bayer {k * 4}/64",
            bayer_k=k * 4,
        )
        for k in range(1, 14)
    ]
    for lut in LUT_GAIN_LUTS:
        cfg = DitherConfig("atkinson", lut, True, 30.0, 12.0)
        p_lut += [
            Patch(
                uid=nxt(),
                phase="lut_gain",
                source="pipeline",
                label=f"atkinson_serp {lut} gray{rgb[0]}",
                rgb=rgb,
                config_name=f"atkinson_serp_{lut}",
                config=asdict(cfg),
            )
            for rgb in levels
        ]
    phases.append(p_lut)

    # Does the LUT effect depend on the diffusion kernel, or is it separable?
    # One cross-check arm rather than a full factorial.
    cfg_fs = DitherConfig("floyd_steinberg", "hue_aware", True, 30.0, 12.0)
    phases.append(
        [
            Patch(
                uid=nxt(),
                phase="lut_algo_cross",
                source="pipeline",
                label=f"fs_serp hue_aware gray{rgb[0]}",
                rgb=rgb,
                config_name="fs_serp_hue_aware",
                config=asdict(cfg_fs),
            )
            for rgb in levels
        ]
    )

    # Dense gamut for a real 3-D inversion. Campaign 1's 5x5x5 is too coarse to
    # invert on a device this non-linear.
    name, cfg = GAMUT_CONFIG
    phases.append(
        [
            Patch(
                uid=nxt(),
                phase="gamut_dense",
                source="pipeline",
                label=f"gamut {rgb}",
                rgb=rgb,
                config_name=name,
                config=asdict(cfg),
            )
            for rgb in gamut_cube_rgb(gamut_steps)
        ]
    )

    # More skin, offset seed so these are NEW colours rather than a repeat set.
    phases.append(
        [
            Patch(
                uid=nxt(),
                phase="skin_dense",
                source="pipeline",
                label=f"skin {rgb} {cname}",
                rgb=rgb,
                config_name=cname,
                config=asdict(ccfg),
            )
            for rgb in skin_locus_rgb(n_skin, seed + 1)
            for cname, ccfg in COLOUR_CONFIGS
        ]
    )

    # Own primaries: a campaign without its own anchors cannot be tied to another.
    phases.append(
        [
            Patch(
                uid=nxt(),
                phase="inks",
                source="ink",
                label=f"ink {INK_NAMES[i]} r{rep + 1}",
                ink_index=i,
            )
            for rep in range(INK_REPEATS)
            for i in range(len(INK_NAMES))
        ]
    )
    return phases


def _interleave(phases, nxt, rng, control_every: int) -> list[Patch]:
    """Shuffle within each phase, then thread the drift controls through.

    Shared by every campaign: the controls ARE the drift measurement, and a
    campaign that skipped them could not be compared against any other session.
    """
    ordered: list[Patch] = []
    for group in phases:
        # Shuffle WITHIN each phase so slow drift cannot alias onto a factor.
        rng.shuffle(group)
        ordered.extend(group)

    controls = [
        ("ctl_white", 0),
        ("ctl_black", 64),
        ("ctl_mid", 32),
    ]

    def neutral_block() -> list[Patch]:
        return [
            Patch(
                uid=nxt(),
                phase="control",
                source="bayer",
                label=label,
                bayer_k=k,
                is_control=True,
            )
            for label, k in controls
        ]

    def ink_block(tag: str) -> list[Patch]:
        """All six solid inks. Used to bracket the campaign at both ends.

        Anchoring the start AND the end on the primaries is what lets this
        session be compared with any other one: without it, a shift in the rig
        between sessions is indistinguishable from a real difference in the
        panel.
        """
        return [
            Patch(
                uid=nxt(),
                phase="control",
                source="ink",
                label=f"ctl_ink_{INK_NAMES[i]}_{tag}",
                ink_index=i,
                is_control=True,
            )
            for i in range(len(INK_NAMES))
        ]

    out: list[Patch] = ink_block("open")
    for i, p in enumerate(ordered):
        if i % control_every == 0:
            out.extend(neutral_block())
        out.append(p)
    out.extend(neutral_block())
    out.extend(ink_block("close"))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("build/colorcal/campaign_spec.json"))
    ap.add_argument("--model", default="bigme_f7", choices=sorted(DISPLAY_REGISTRY))
    ap.add_argument("--seed", type=int, default=20260808)
    ap.add_argument(
        "--campaign",
        type=int,
        default=1,
        choices=(1, 2, 3),
        help="1 = original, 2 = follow-up only, 3 = MERGED union (use this from scratch)",
    )
    ap.add_argument("--gamut-steps", type=int, default=5, help="NxNxN RGB cube")
    ap.add_argument("--skin", type=int, default=60, help="distinct skin colours")
    ap.add_argument("--control-every", type=int, default=25)
    ap.add_argument("--seconds-per-patch", type=float, default=56.0)
    args = ap.parse_args(argv)

    patches = build_patches(
        seed=args.seed,
        gamut_steps=args.gamut_steps,
        n_skin=args.skin,
        control_every=args.control_every,
        campaign=args.campaign,
    )

    counts: dict[str, int] = {}
    for p in patches:
        counts[p.phase] = counts.get(p.phase, 0) + 1
    total_s = len(patches) * args.seconds_per_patch
    print(f"{len(patches)} patches")
    for phase, n in sorted(counts.items()):
        print(f"  {phase:12s} {n:5d}   {n * args.seconds_per_patch / 3600:5.2f} h")
    print(f"  {'TOTAL':12s} {len(patches):5d}   {total_s / 3600:5.2f} h")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "model": args.model,
                "seed": args.seed,
                "seconds_per_patch": args.seconds_per_patch,
                "patches": [asdict(p) for p in patches],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
