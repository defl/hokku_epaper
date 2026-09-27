# The rendering campaign: what the eye said

A closed investigation into whether the panel's image pipeline could be made to
render photographs better than it already does, judged blind off real glass.

**The answer is no.** Twenty-one alternative renderings were photographed off the
panel and rated blind against the settings the server actually runs. Not one beat
them. Three were significantly worse. This document records the method, the
numbers, the bugs the exercise uncovered, the one lead it could not close, and
the claims it had to retract — so that none of it has to be re-derived.

For the *spectrophotometer* campaign that produced the panel model this rests on
(1733 readings, Yule–Nielsen n = 1.38), see
[measurements/findings.md](measurements/findings.md).

## Method

Each candidate was rendered at full panel resolution, pushed to a bench panel
over USB, refreshed, and photographed with a calibrated camera rig. Nothing was
judged from a simulation.

| stage | |
|---|---|
| render | full 1200×1600, through the shipped pipeline, not a stand-in |
| push | `frame` upload over USB to the bench Huessen, ~30 s refresh, settle |
| capture | Raspberry Pi + ov5647, pinned exposure, RAW/DNG |
| correct | two-flat photometric separation (illumination + ~5–6 % additive glare) |
| register | content homography, then rectify to the panel grid |
| colour | camera→Lab fit, validated leave-one-out |
| judge | blind grouped rating pages, 5-point scale, opaque filenames, per-photograph arm shuffling |

The rating pages never name an arm, shuffle position per photograph, and stamp
the session so an export cannot be decoded against the wrong page.

## What the ratings can and cannot support

Three properties of the data constrain every claim below, and all three were
measured rather than assumed.

**Absolute levels drift between evenings.** The same baseline renders scored a
mean of 2.32 on one page and 2.67 on another; "ok or better" moved from 45 % to
78 %. Nothing is pooled on absolute score. Every comparison in this document is a
*paired difference within one photograph*, which holds the picture and the
sitting fixed.

**Rating noise is ±0.55 of a five-point step**, from repeat trials. Only about
**33 %** of the observed spread between versions is real signal.

**The achievable ceiling is 77.7 %, not 100 %.** A fitted objective reached
64.8–66.2 % held-out sign accuracy against that ceiling — genuinely predictive,
but not a substitute for looking.

## The result

443 ratings · 62 photographs · 4 evenings · 21 alternative renderings.

Each rendering against the deployed configuration, paired within a photograph:

| rendering | n | better | worse | same | mean | p |
|---|---:|---:|---:|---:|---:|---:|
| `b50_t70` (LUT chroma trim 0.70) | 19 | 6 | 3 | 10 | **+0.26** | 0.51 |
| `b100_t85` (full gamut correction) | 18 | 4 | 3 | 11 | +0.11 | 1.00 |
| `b50_t85` (**resolution control**) | 19 | 2 | 0 | 17 | +0.11 | 0.50 |
| `b25_t85` | 19 | 1 | 0 | 18 | +0.05 | 1.00 |
| `b00_t85` (no gamut correction) | 19 | 1 | 1 | 17 | +0.00 | 1.00 |
| `b50_t100` (no chroma trim) | 17 | 2 | 3 | 12 | −0.06 | 1.00 |
| `rich` | 14 | 1 | 3 | 10 | −0.07 | 0.63 |
| `saturate` | 13 | 3 | 6 | 4 | −0.08 | 0.51 |
| `scale_chroma` | 13 | 2 | 5 | 6 | −0.08 | 0.45 |
| `cam16` (palette LUT) | 12 | 4 | 4 | 4 | −0.17 | 1.00 |
| `contrast_up` | 20 | 2 | 4 | 14 | −0.20 | 0.69 |
| `clahe_low` | 12 | 3 | 4 | 5 | −0.25 | 1.00 |
| `clahe_off` | 20 | 6 | 11 | 3 | −0.30 | 0.33 |
| `darker` | 17 | 1 | 6 | 10 | −0.35 | 0.13 |
| `desat` | 14 | 3 | 6 | 5 | −0.36 | 0.51 |
| `contrast_dn` | 19 | 1 | 6 | 12 | −0.37 | 0.13 |
| `gamma_hi` | 17 | 2 | 7 | 8 | −0.47 | 0.18 |
| `weighted` (palette LUT) | 13 | 3 | 7 | 3 | −0.54 | 0.34 |
| `oklab` (palette LUT) | 26 | 3 | 14 | 9 | −0.62 | **0.013** |
| `stucki` (diffusion kernel) | 14 | 0 | 7 | 7 | −0.79 | **0.016** |
| `calm` | 16 | 1 | 8 | 7 | −0.81 | **0.039** |

Significantly better than deployed: **none**. Significantly worse: `oklab`,
`stucki`, `calm`. The deployed configuration is at or tied for the top on 39 of
71 judged photographs.

## What the gamut-correction LUT round found

The correction LUT is a property of the `Display`, not of `ImageConfig`, so no
config knob can reach it; it needed its own round of 210 captures over 30
photographs weighted towards out-of-gamut content.

**The control worked.** `b50_t85` has the same blend and chroma trim as the
deployed LUT but half the grid resolution. It matched the deployed rendering on
17 of 19 photographs, which is what makes the rest of the column readable — a
difference elsewhere is the setting, not the resampling.

**Correction blend does nothing the eye can see.** Blend 0.00, 0.25, 0.50 and
1.00 all tie within ±0.11. The project plan had called this "the single most
consequential setting". It is not.

**Chroma trim does something, and it is content-dependent.** See below.

## The one lead this campaign could not close

Trim 0.70 scored the best mean of all 21 arms, but at p = 0.51 that is nothing on
its own. Split by how much of each photograph the panel physically cannot
reproduce, it stops looking like noise:

| | mean | better | worse |
|---|---:|---:|---:|
| hardest half (most out-of-gamut) | **+0.70** | 5 | **0** |
| easiest half | −0.22 | 1 | 3 |

Spearman ρ = **−0.493**, p = **0.032**, n = 19: pulling chroma back helps where
the picture is out of gamut and hurts where it is not. The sign flips cleanly at
the median.

It replicates across evenings and mechanisms. The most out-of-gamut photograph in
the library (a red wall, rank 1 of 30) was rescued from *bad* to *ok* by
`scale_chroma` on one evening and from *terrible* to *ok* by trim 0.70 three
evenings later. On the earlier page the same interaction reads ρ = −0.845 (n = 6).

**And the honest caveat: drop that one photograph and it falls to ρ = −0.393,
p = 0.106.** The direction survives; the significance does not. The evidence is
one dramatic case plus a consistent weak trend, and the split was found by
looking at the data rather than predicted in advance.

Closing it needs photographs at the extreme end, which were thin here: roughly
15 of the most out-of-gamut photographs against three or four trim levels, about
35 minutes of panel time. Left undone deliberately.

This is also the campaign's answer to the question it was started to ask. No
single global configuration can win, because the right amount of chroma depends
on the picture — which is the first direct evidence for treating images
individually rather than through three presets.

## What changed in the code

The deployed configuration won, but `presets.py` had drifted away from it over
months of tuning through the web UI. **The repository was describing a
configuration that had just lost to the one actually running.** Since the
`DEFAULT_*_IMAGE_CONFIG` values seed only a fresh install, this affected new
installs rather than the running appliance — quietly, which is worse.

- **General and face defaults now match the judged configuration exactly.**
- **CLAHE is back on (1.75) for both.** An earlier glass A/B over a handful of
  photographs had turned it off; the campaign put that arm in front of 20
  photographs and it lost (worse on 11, better on 6). The larger blind evidence
  wins, and the older argument is kept beside the value it still applies to.
- **Chroma boosts sit at neutral.** On faces this was real rather than cosmetic:
  `color_enhance` applies only when adaptive saturation is off, which in the face
  pipeline it is — so 1.25 was a live chroma boost on skin, in the pipeline
  roughly four out of five library photographs take.
- **The black-and-white default was deliberately not reconciled.** Not one of the
  62 judged photographs was near-greyscale, so that pipeline has no evidence
  either way. Only one change was made there, on its own merits: `color_enhance`
  1.05 → 1.0, which was a live 5 % chroma boost in the one preset whose
  description promises colour boosting is off.
- **The dropdown alternates were left alone.** The campaign says nothing about
  them and moving them would have invalidated their descriptions.

## Bugs this exercise uncovered

**The metric bank cached the wrong thing for LUT arms.** The cache key was
`(image, config, crop)` — but the correction LUT is a property of the *display*,
so every LUT variant shared a key with the baseline and the cache returned
whichever was measured first, reporting success. Any LUT comparison made before
this fix is void. Pinned by a test.

**`chroma_contrast_ratio` exploded on greyscale.** Its denominator is source
chroma *spread*, floored only at 1e-6, so a near-neutral photograph produced 639
against a normal range under 1. Weighted into the objective it invented roughly
970 rating points and drove a reported mean gain of +15. Now floored properly.

**Capture tags could name two different photographs.** Tags were built from a
22-character filename stem; four collided, and colliding captures overwrite each
other silently. Now suffixed with a hash of the full name.

**The fitted reference used the wrong crop.** `reference_for()` cropped at 0
while production crops at 0.14, so the fitted target was a different framing from
the render being scored.

## Claims that were made and withdrawn

Recorded because each was believed for a while and each was wrong.

- **"Colour-magnitude split, p = 0.013."** Confounded by arm identity — the small
  band was mostly one arm and the large band mostly another. Within-arm splits
  were inconsistent at n = 6–10.
- **"The constrained search beats the deployed configuration."** +0.33 on one
  page, −0.11 on the next. Neither significant.
- **"ΔE magnitude screens candidates worth photographing."** Magnitude does not
  predict the verdict at all: ρ = +0.03 over 233 judged pairs. This undercut the
  screening premise the capture plans had been built on.
- **"The palette-LUT arms isolate colour."** They do not. A palette LUT changes
  which ink represents a colour *and* the quantisation error that error diffusion
  then spreads, so the grain moves too — measurably 10× more than the tonal arms
  and 3–4× more than the arm that changed the diffusion kernel outright. A
  verdict of "worse" on those arms cannot say which cause did it, and this data
  cannot separate them.

## The apparatus

Code is in [`tools/camcal/`](../../../tools/camcal/README.md) plus
`tools/render_bank.py`, `tools/param_search.py`, `tools/judge_capture.py` and
`tools/judge_pick.py`. Captures and fitted artefacts are gigabytes of DNG under
the gitignored `build/camcal/` and are reproducible from the panel; the rig
calibration and the rating exports are not reproducible and are small.

Rating exports are archived beside their session as `*_export.json`, because
every browser download arrives with the same filename and one round's notes were
lost to that before it was noticed.

## Open, and deliberately not pursued

- **Chroma trim against out-of-gamut content**, above — the one live lead.
- **The neon metrics measure the wrong thing.** They measure chroma *loss*; the
  defect they were meant to catch is probably better defined as single-chromatic-ink
  coverage within a region.
- **Per-image configuration.** The interaction above is the argument for it, and
  the server already has both delivery paths (per-image overrides for the known
  library, a classifier branch for new images).
