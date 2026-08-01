# Searching the dither settings for better presets

An investigation into why warm mid-tones (lips, skin) pick up blue ink, and an
attempt to find better settings for the three pipelines — general, B&W and
faces — by searching the existing configuration space.

**Outcome: one adoptable change (B&W), one confirmed documentation error, and a
tempting recommendation that failed validation.**

The blue-lips artifact is real and reproducible on **flat colour fields**, where
OKLAB/CAM16-UCS reduce it 6× (§1, §4a). That result does not transfer. Two
freely-licensed portraits with genuinely saturated lipstick were added to close
the corpus gap, and with them in place the shipped pipeline measures **0.64 %**
blue on real lipstick at full panel resolution while the proposed "fix" measures
3.09 % — about 5× worse, and 10–26 % worse overall (§4e). A uniform colour field
is the pathological case for error diffusion; real lips have texture and
gradient, which break the cascade before it runs.

Also established: `hue_aware` does nothing for this artifact despite
[dithering.md §5a](dithering.md) presenting it as the fix (§2).

Everything here is reproducible with `tools/dither_search.py` — no hardware, no
measurements.

---

## 1. The failure, reproduced

A flat patch of lip colour, dithered with the production renderer, and the
resulting ink histogram inspected. Flat patches matter: the failure is an
error-diffusion *cascade* ([dithering.md §5a](dithering.md)), so a single-pixel
nearest-ink query cannot show it.

Blue-ink percentage, Floyd–Steinberg, huessen_epf1301 palette, with the real
tonal chain (adaptive saturation + DRC) applied:

| swatch | sRGB | `euclidean` | `hue_aware` | `oklab` | `cam16ucs` |
|---|---|---:|---:|---:|---:|
| lips deep | (150, 60, 70) | 30.6 % | 30.6 % | **5.2 %** | 5.3 % |
| lips mid | (190, 90, 95) | 27.9 % | 27.9 % | **0.0 %** | 0.0 % |
| lips pale | (215, 130, 130) | 13.5 % | 13.5 % | **0.0 %** | 0.0 % |

So the field observation — *"all but oklab make lips blue"* — is correct, and
the effect is large: roughly **6× less blue ink** under OKLAB on genuine lip
colour.

Note the tonal chain makes it **worse**, not better. Without prep the same
swatches give 22.4 / 14.9 / 6.1 %; the production chain raises that to
30.6 / 27.9 / 13.5 %. Adaptive saturation pushes these colours further from the
palette, and the residual grows accordingly.

## 2. `hue_aware` does not do what the docs claim

[dithering.md §5a](dithering.md) presents the hue-aware LUT as *the* fix for this
exact artifact, "preventing the error-cascade hue-swaps". Measured, it is
**bit-identical to `euclidean`** on every swatch above, and identical across the
whole photo corpus too.

The geometry says why:

```
lips deep hue = 19.1°     blue ink hue = 291.8°     |Δh| = 87.3°
default hue_cutoff_deg = 95°   ->   blue is ALLOWED
```

Blue sits *just inside* the default gate for warm tones. But tightening the gate
barely moves it, which is the real finding:

| `hue_cutoff_deg` | blue cells in the 32³ LUT | cells differing from 95° |
|---:|---:|---:|
| 95° | 9215 / 32768 | — |
| 85° | 9217 | 23 |
| 60° | 9217 | 250 |
| 40° | 7992 | 2960 |

**The gate evaluates the hue of the value being looked up, not the hue of the
source pixel.** Error diffusion walks the accumulated value into genuinely
bluish territory; at that point blue *is* the hue-appropriate ink and the gate
correctly permits it. Hue gating cannot prevent a cascade whose defining feature
is that it changes the hue before the lookup happens.

Tracing the walk for `(150,60,70)`: red → red → **white** at 70 % drift. It is
the repeated white picks that accumulate the cold residual.

OKLAB and CAM16-UCS help for a different reason: they change the *distance
geometry*, so the residual never heads toward blue to begin with.

> **Documentation correction needed.** §5a should not claim the hue-aware LUT
> fixes the warm-skin cascade. It does not, at any usable cutoff.

## 3. The search

`tools/dither_search.py` renders the real test corpus through the production
`ImageRenderer`, scores each candidate, and ranks them. No new settings are
invented — every candidate is expressible in `config.json` today.

```
python tools/dither_search.py --all --corpus swatches --stage 2 --refine 3   # the real run
python tools/dither_search.py --all --corpus photos  --stage 2              # photo corpus (see §5)
python tools/dither_search.py --profile faces --corpus swatches
```

**Corpus split** uses the server's own classifiers (`ImageClassifier._check_grayscale`,
`OpenCVYuNetFaceDetector`) so each image lands in the pipeline that would really
handle it: 12 general, 3 B&W, 5 faces.

**Stages.** Stage 1 sweeps algorithm × LUT (3 × 8, plus the `bw` LUT for the B&W
profile) with everything else at the pipeline's current preset. Stage 2 refines
the stage-1 winners across saturation space, adaptive vivid, DRC space and
serpentine. The staging assumes algorithm/LUT dominates — reasonable here,
since the cascade is a geometry problem.

**Scoring** uses the repo's own `image_compare` so numbers stay comparable to
`test_dither_quality_metrics`, plus one addition. The existing metric set has a
hole: `neutral_blue_fraction` only inspects source-*neutral* pixels, while this
failure is in source-*saturated* warm ones. Nothing in the suite was looking at
it, which is how a default with 30 % blue lips passed review.

The addition is split by chroma, and the split is essential:

| metric | selection | what it sees |
|---|---|---|
| `warm_blue` | hue −40…70°, C\* > 20 | skin-dominated on any portrait |
| `lips_blue` | hue −40…70°, C\* > 38 | the artifact being complained about |

A single broad band is useless: skin outnumbers lips by orders of magnitude, so
a 6× difference on lips vanishes into the average.

Per-profile weights live in `PROFILE_WEIGHTS`. They are the opinion in this tool
and the thing to argue with.

## 4. Results

### 4a. First attempt, and why it was wrong

The first run swept **algorithm × LUT only** — 2 of roughly 20 available
dimensions — and concluded the presets were "already at the optimum". That
conclusion was an artifact of under-searching, and it is worth recording because
the omitted dimensions are exactly the ones that matter:

`adaptive_saturate_space` (off / cielab / **oklab**), `drc_l_space` and
`drc_chroma_space` (cielab / **oklab**), `adaptive_vivid`, `serpentine`, plus
`hue_cutoff_deg`, `neutral_chroma` and `dither_noise`.

§1 already pointed at them: the artifact got *worse* under the tonal chain, and
that chain **is** adaptive-saturate + DRC. Sweeping the dither while holding the
thing that amplifies the problem fixed was the wrong experiment.

Measured directly on lip swatches (dither + stripe prep, atkinson):

| config | mean blue on lips |
|---|---:|
| **production** — `hue_aware`, sat=cielab, drc=cielab | **9.39 %** |
| `oklab` LUT, sat=cielab, drc=cielab | 2.20 % |
| `oklab` LUT, sat=cielab, **drc=oklab** | **1.47 %** |

**6.4× better on existing settings.** The DRC space alone is worth a further
1.5× on top of the LUT change.

### 4b. Flat swatches need a sheet, not one image each

The first synthetic corpus rendered each colour as its own uniform image and
reported a suspicious `lips_blue` of 0.00 %. A uniform image has **zero dynamic
range**, and the pipeline's first step is `ImageOps.autocontrast` — which
stretches a single histogram value to arbitrary output. Nothing lip-coloured
survived to reach the dither.

Fixed by tiling every swatch into one sheet with explicit black and white anchor
tiles, so autocontrast has real endpoints to preserve, then taking metrics per
tile region. `swatch_sheet()` in the tool.

### 4c. Full search, swatch sheet

| profile | baseline | best found | change |
|---|---|---|---|
| general | `atkinson_hue_aware` 40.756 | `stucki+cam16ucs_hue_aware` sat=off vivid=1 **drc=oklab** serp=1 → 38.733 | **5.0 % better** |
| B&W | `floyd_steinberg_bw` 35.572 | `stucki+bw` sat=off **drc=oklab** → 35.197 | **1.1 % better** |
| faces | `atkinson_hue_aware` 65.153 | `stucki+cam16ucs` sat=off vivid=1 drc=cielab serp=0 → 62.383 | **4.3 % better** |

Consistent across all three:

- **Stucki wins everywhere**, over the current Atkinson (general, faces) and
  Floyd–Steinberg (B&W). Its wider two-row kernel spreads the residual further,
  so it accumulates less locally — which is precisely what drives the cascade.
- **CAM16-UCS (or OKLAB) for the colour profiles**, never the CIELAB LUTs.
- **`drc=oklab`** for general and B&W; faces is near-indifferent.

### 4d. Validation on real photos — the swatch winners do **not** transfer

The swatch-derived configs were then run head-to-head against the shipped
presets on the real photo corpus. Three candidates per pipeline: **A** production,
**B** exactly what the swatch search picked, **C** "conservative" — only the
changes robust across both methods (Stucki + CAM16-UCS/bw + `drc=oklab`,
saturation left alone).

| profile | variant | score | dE2000 | neutral_leak | warm % | lips % | vs production |
|---|---|---:|---:|---:|---:|---:|---|
| general | A production | 59.466 | 34.29 | 15.62 | 7.72 | 6.01 | — |
| | B swatch winner | 62.053 | 34.88 | 23.47 | 9.47 | 5.01 | **−4.4 % worse** |
| | C conservative | 64.142 | 34.88 | 23.35 | 10.13 | 5.70 | **−7.9 % worse** |
| B&W | A production | 40.222 | 28.14 | 3.02 | 0.00 | 0.00 | — |
| | B / C | 39.988 | 27.76 | 3.06 | 0.00 | 0.00 | **+0.6 % better** |
| faces | A production | 61.041 | 34.75 | 15.04 | 1.66 | 1.22 | — |
| | B swatch winner | 67.141 | 35.65 | 20.53 | 3.85 | 2.21 | **−10.0 % worse** |
| | C conservative | 72.575 | 36.34 | 23.18 | 4.98 | 3.19 | **−18.9 % worse** |

**The recommendation does not survive contact with real photographs.** On faces
it is nearly 19 % worse, and it makes the very artifact it was meant to fix
*worse*: `lips_blue` goes 1.22 % → 3.19 %.

The clearest regression is `neutral_leak`, 15 → 23 on both colour profiles. The
CAM16-UCS and OKLAB LUTs leak substantially more colour into near-neutral areas
on real images — skies, walls, grey clothing. The swatch sheet has exactly one
neutral tile, so it could not see that cost at all, while the photo corpus is
full of neutrals.

So the two corpora fail in *opposite* directions:

| corpus | can see | cannot see |
|---|---|---|
| swatch sheet | saturated warm colour, the cascade in flat areas | neutrals, texture, CLAHE behaving sanely |
| photo corpus | neutrals, texture, realistic tonal chain | saturated lips (none existed in it — §5) |

Optimising against either alone produces a config that is worse in the other's
blind spot. **Only the B&W result is robust**, winning on both.

### 4e. Corpus gap closed — the answer is now definitive

Two freely-licensed portraits with genuinely saturated lipstick were added to
`images/test/` (see its `CREDITS.md`): `Brunette_red_lipstick.jpg` — red lips
against a grey backdrop, dark hair and black clothing, so it carries the
saturated warm colour **and** the neutrals in one frame — and
`Applying_red_lipstick_model_Eve_Casini.jpg`, an extreme close-up at 58.7 % of
pixels above C\* 38 (versus 0.13 % for the best previous portrait).

With the blind spot closed, the verdict does not soften — it hardens:

| corpus / resolution | production | swatch winner | conservative |
|---|---:|---:|---:|
| faces, 640×480 | **58.257** | 65.321 (−12.1 %) | 73.491 (−26.2 %) |
| faces, **3200×1600** (full panel) | **62.067** | 68.539 (−10.4 %) | 74.972 (−20.8 %) |

And on the metric that motivated all of this — `lips_blue`, now measured on real
lipstick at full panel resolution:

| variant | lips_blue |
|---|---:|
| **production** | **0.64 %** |
| swatch winner | 1.88 % |
| conservative | 3.09 % |

**The shipped pipeline already handles real lipstick well, and the proposed
"fix" makes it ~5× worse.** Full panel resolution was checked specifically
because lips occupy far more pixels there, approaching the flat-field case —
it changes nothing.

The flat-swatch catastrophe (30 % blue) therefore does **not** occur in
photographs. A uniform colour field is the pathological case for error
diffusion; real lips carry texture, specular highlight and gradient, all of
which break the cascade before it can run.

### 4e. One contradiction, unresolved

The sheet prefers `adaptive_saturate=off`; the dither-only test in §4a prefers
`sat=cielab` and rates `off` far worse (4.74 % vs 1.47 %).

The difference is the PIL prep phase — autocontrast, gamma, CLAHE, unsharp mask
— which the sheet includes and the direct test does not. CLAHE on *flat tiles*
is a degenerate case: it manufactures local contrast where a real lip has
texture. So the sheet is trustworthy for the LUT and algorithm choice (both
robust across the two methods) and **not** trustworthy for the saturation
setting.

Resolving it needs test signals with realistic texture — either real photos
containing saturated lips, or synthetic patches with plausible noise/gradient.

## 5. Why the search cannot see the problem

The corpus does not contain saturated warm colour in any quantity that matters.
Share of pixels that are warm-hued at each chroma floor:

| image | C\* > 20 | C\* > 38 | C\* > 50 |
|---|---:|---:|---:|
| RGB_corner_gradient (synthetic) | 26.9 % | **23.9 %** | **20.6 %** |
| Robert_De_Niro_KVIFF_portrait | 29.7 % | 15.2 % | 0.5 % |
| Wayuu_woman_with_sad_face | 25.1 % | 10.2 % | 7.8 % |
| NewTux (cartoon) | 2.9 % | 2.8 % | 2.7 % |
| string_ensemble_concert | 10.8 % | 1.7 % | 0.1 % |
| **Actress_Anna_Unterberger** | 13.3 % | **0.13 %** | **0.00 %** |
| everything else | ≤ 1.5 % | 0.00 % | 0.00 % |

The `lips_blue` population is dominated by a **synthetic RGB gradient** and by
warm backgrounds and clothing. The one portrait most likely to have lipstick
contributes 0.13 %, and nothing at all above C\* > 50.

**There are no saturated red lips anywhere in the corpus.** So `lips_blue` on
photos is measuring warm gradients and jackets, and optimising it is optimising
noise. That also explains why the metrics never flagged this artifact: no test
image exhibits it.

This is the substantive finding. The search harness is sound; the corpus is not
fit for this question.

## 6. What to do with this

**Safe to adopt — one change only:**

1. **B&W: Stucki + `bw` LUT + `drc=oklab`.** The only result that wins on both
   corpora (+1.1 % on swatches, +0.6 % on photos), with no regression anywhere.

**Ruled out:**

2. **Do not switch the general or face LUTs to CAM16-UCS/OKLAB.** This is
   settled, not merely unproven (§4e): with real lipstick in the corpus and at
   full panel resolution it is 10–26 % worse overall, and it makes `lips_blue`
   ~5× worse (0.64 % → 3.09 %). The cost lands in `neutral_leak` (18 → 26).
   The 6.4× win exists only on flat colour fields, which do not occur in
   photographs.
3. **Leave `adaptive_saturate_space` alone** — the two methods disagree (§4e).

**What would actually settle it**, roughly in order of value:

4. **If blue lips are still seen in the field, look past the dither.** The
   pipeline measures at 0.64 % blue on real lipstick at full panel resolution,
   so the renderer is probably not the culprit. The likeliest remaining suspect
   is the palette itself: every LUT is built from `palette_measured_rgb`, and
   those anchors are unverified (item 6). If the panel's real inks differ from
   the assumed ones, the dither is optimising toward the wrong targets and no
   amount of LUT tuning fixes it.
5. **A neutral-leak-aware search**, if the LUT question is ever revisited. The
   regression shows the LUT choice trades warm-hue accuracy against neutral
   purity; that trade should be measured deliberately.
6. **Correct [dithering.md §5a](dithering.md)** — the hue-aware LUT is not the
   fix it is described as (§2). That finding is independent of all the above
   and stands on its own.
7. The palette anchors every number here rests on are unverified — see
   [color_calibration.md](color_calibration.md). The LUTs are built from
   `palette_measured_rgb`, so a CIELAB-vs-OKLAB comparison is partly a
   comparison of how each space tolerates *wrong anchors*. Measuring them makes
   the whole exercise more trustworthy.

Nothing here has been applied to the shipped presets.

> **Method note.** Two recommendations in this document were retracted after
> further measurement: "the presets are already optimal" (wrong — under-searched,
> §4a) and "switch the colour LUTs" (wrong — 6.4× on flat swatches, 10–26 %
> worse on photographs, §4d/§4e). Both were plausible, both were premature, and
> in both cases the error was generalising from a test signal that could not see
> the whole cost. The corpus gap is now closed and the LUT question is settled;
> treat the remaining open items as hypotheses with tests attached.

## 7. Caveats

- Rendered at 640×480 rather than full panel, for search speed. The cascade is
  resolution-sensitive, so absolute values differ at full size; rankings held on
  the spot checks that were run, but this has not been swept.
- Scoring weights are a judgement call, not a measurement. A different weighting
  reorders the tables; the *corpus* finding in §5 does not depend on them.
- Everything is measured against `palette_measured_rgb`, whose provenance is
  weak (see §6.5). This compares configurations against a model of the panel,
  not against the panel.
