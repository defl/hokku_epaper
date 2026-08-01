# Searching the dither settings for better presets

An investigation into why warm mid-tones (lips, skin) pick up blue ink, and an
attempt to find better settings for the three pipelines — general, B&W and
faces — by searching the existing configuration space.

**The headline result is negative, and the reason is the interesting part:** the
search finds no better settings, because the test corpus does not contain the
failure it is meant to fix. Two claims in [dithering.md](dithering.md) also turn
out to be wrong. Both are documented below with the evidence.

Everything here is reproducible with `tools/dither_search.py` and the scripts
described in §2 — no hardware, no measurements.

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
python tools/dither_search.py --all --stage 1 --canvas 640x480
python tools/dither_search.py --profile faces          # full two-stage
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

## 4. Results — no improvement found

Stage 1, 640×480 canvas, huessen_epf1301:

| profile | baseline | best found | change |
|---|---|---|---|
| general | `atkinson_hue_aware` 59.641 | `atkinson+hue_aware` 59.653 | none |
| B&W | `floyd_steinberg_bw` 40.225 | `atkinson+bw` 39.841 | **1.0 % better** |
| faces | `atkinson_hue_aware` 61.018 | `atkinson+euclidean` 61.064 | none |

The only real result is B&W: **Atkinson beats Floyd–Steinberg** with the `bw`
LUT (39.841 vs 40.222, ~1 %). Small but consistent, and free to adopt.

For general and faces the current presets are already at the optimum of this
search space. Worse, `oklab` and `cam16ucs` rank *below* the CIELAB LUTs on
faces (`lips_blue` 1.74 % vs 1.22 %) — the exact opposite of §1.

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

1. **Adopt the B&W change.** `atkinson` + `bw` LUT, ~1 % better and free.
2. **Trust the synthetic-swatch evidence for the blue-lips artifact**, not the
   photo corpus. It is 6× and unambiguous; the corpus simply cannot see it.
3. **Fix the corpus before re-running the search.** It needs images with
   saturated red lips, and ideally a synthetic swatch sheet spanning the warm
   high-chroma region. Until then a photo-corpus search cannot answer the
   question that prompted it.
4. **Correct [dithering.md §5a](dithering.md)** — the hue-aware LUT is not the
   fix it is described as.
5. The palette anchors these decisions rest on are themselves unverified — see
   [color_calibration.md](color_calibration.md). Measuring them makes every
   number above more trustworthy, including the LUT comparison, since the LUTs
   are built from `palette_measured_rgb`.

## 7. Caveats

- Rendered at 640×480 rather than full panel, for search speed. The cascade is
  resolution-sensitive, so absolute values differ at full size; rankings held on
  the spot checks that were run, but this has not been swept.
- Scoring weights are a judgement call, not a measurement. A different weighting
  reorders the tables; the *corpus* finding in §5 does not depend on them.
- Everything is measured against `palette_measured_rgb`, whose provenance is
  weak (see §6.5). This compares configurations against a model of the panel,
  not against the panel.
