# What the measurements say, and what to do about it

Final write-up of the Bigme F7 colour campaign: **1700 readings over 20 sessions**,
X-Rite ColorMunki Photo via ArgyllCMS `spotread`, reflective 45°/0°, one unit, the
meter clamped in a single position for the entire campaign.

Every number below is regenerated from the committed dataset by
`tools/color_findings.py`. Run it to check this document rather than trusting it —
the numbers here were wrong once, in a way described at the end, precisely because
they were written by hand from whatever subset existed on the day.

Data and schema: [`data/`](data/). Tools: `tools/color_*.py`.

## What actually changed in the product

One thing, and it was a bug rather than a preference:

- **The dynamic-range compressor targeted a range the panel cannot show.** It
  derived its lightness anchors from `palette_measured_rgb`, a third-party table
  whose white sits at L\* 79.3. This glass tops out at **L\* 67.96**. The DRC was
  compressing every image into a range 37 % wider than the display, so both ends
  clipped — on one test portrait, half the image collapsed into flat black. Fixed
  by `drc_anchor_l` on the display spec ([`display.py`](../../../../python/hokku/screens/bigme_f7/display.py)),
  with the renderer falling back to the old behaviour when a screen has not been
  measured. Confirmed better on glass, side by side. The anchors are applied in
  both the CIELAB and OKLAB compressors, which matters more than it did when the
  fix was written: `main` has since moved the general and black-and-white
  pipelines' DRC into OKLAB, so a fix to only one space would now be silently
  inactive on most photos.

Nothing else is recommended. In particular **no dither LUT change**, which is not
where this work expected to land — see [The LUT question](#the-lut-question).

## The panel

| ink | reads | L\* | a\* | b\* | Y % | spread ΔE |
|---|---:|---:|---:|---:|---:|---:|
| black | 42 | 10.18 | +10.75 | −10.56 | 1.15 | 0.269 |
| white | 42 | 67.96 | −3.81 | −2.20 | 37.91 | 0.289 |
| yellow | 41 | 66.96 | −13.66 | +73.09 | 36.58 | 0.247 |
| red | 41 | 25.17 | +40.00 | +29.36 | 4.47 | 0.429 |
| blue | 40 | 33.08 | +6.60 | −41.58 | 7.57 | 0.393 |
| green | 40 | 34.00 | −24.96 | +7.72 | 8.01 | 0.510 |

**Contrast is 33.0 : 1** — white Y 37.91 %, black Y 1.15 %. That is the physical
ceiling; no amount of rendering produces a brighter white or a deeper black than
the ink.

**Gamut is 46 % of sRGB, 26 % of the visible locus** (xy hull area 0.05165).

> Both figures previously published here — 55 % of sRGB and 18.5 % of the visible
> locus — were wrong, and provably so without remeasuring anything: the two
> references differ by a fixed ratio, so 55 % of sRGB *is* 32 % of the locus and
> the pair could never both be right. They were computed at different times from
> different subsets. `color_findings.py` now derives both from one hull area.

**Black ink is not neutral** — a\* +10.75, b\* −10.56, chroma 15.1, a violet-blue.
A grey dithered from black and white therefore drifts off-neutral as it darkens,
and this is the origin of most of the grey-cast behaviour further down.

**Chromaticity flatters this panel.** The saturated inks are also the dark ones:

| ink | Y % where its chromaticity lives |
|---|---:|
| red | 4.47 |
| blue | 7.57 |
| green | 8.01 |
| yellow | 36.58 |

Against a 37.91 % white, red is reachable only at a twelfth of paper luminance.
The usable *volume* is far smaller than the xy outline suggests, which is why
gamut mapping is a design decision and not something measurement settles.

## How much of any of this is real

The campaign re-measured all six solid inks at the start and end of every session.
Those brackets are the error bars.

| ink | brackets | mean ΔE to that ink's mean | worst |
|---|---:|---:|---:|
| black | 35 | 0.290 | 0.686 |
| white | 35 | 0.267 | 0.651 |
| yellow | 34 | 0.245 | 0.681 |
| red | 34 | 0.412 | 1.188 |
| blue | 34 | 0.374 | 0.832 |
| green | 34 | 0.501 | 1.096 |

**Noise floor: mean 0.348 ΔE, p95 0.785.** Within a session, open-to-close drift
is 0.458 ΔE mean over 90 pairs. It is non-directional and uncorrelated with
elapsed time, which is what identifies it as panel refresh repeatability rather
than instrument drift — the instrument was recalibrated every session and the
brackets do not step at session boundaries.

**Any conclusion in this document resting on a difference below ~0.4 ΔE is inside
the noise.** Several candidate improvements died on exactly this.

### Dedup is sound

Many planned patches dither to a byte-identical raster; those inherited an earlier
reading instead of being re-displayed (~54 % of the dense gamut phase). Where the
same raster was nonetheless measured independently more than once — **4705 such
pairs** — the readings agree to **mean 0.497 ΔE, median 0.421**. That is the noise
floor, so inheritance costs nothing measurable.

Control patches are excluded from inheritance in both directions. A control that
silently reused an old reading would report zero drift by construction.

## Tone response: the pipeline's linearity assumption is false

The dither assumes inks mix linearly by area. Measured across 64 levels of
black-on-white:

| nominal | measured Y % | linear Y % | effective coverage | dot gain | ΔL\* |
|---:|---:|---:|---:|---:|---:|
| 0.125 | 30.4 | 33.2 | 0.202 | +0.077 | 2.3 |
| 0.250 | 24.0 | 28.6 | 0.377 | +0.127 | 4.4 |
| 0.375 | 17.6 | 24.1 | 0.551 | +0.176 | 7.2 |
| **0.500** | **11.7** | **19.5** | **0.713** | **+0.213** | **10.6** |
| 0.625 | 9.3 | 14.9 | 0.778 | +0.153 | 9.0 |
| 0.750 | 6.3 | 10.3 | 0.861 | +0.111 | 8.4 |
| 0.875 | 3.7 | 5.7 | 0.931 | +0.056 | 6.2 |

A 50 % dither prints like **71 %** coverage — ΔL\* 10.6, far above any
just-noticeable threshold. The curve is the classic dot-gain arch, peaking
mid-tone and closing at both ends, so no gain or exposure shift corrects it.

**This does not mean the pipeline needs a dot-gain correction.** Error diffusion
is a closed loop: it measures its own accumulated error against the palette and
compensates as it goes, so end-to-end the rendered mid-tones already land close.
The arch is a property of *open-loop coverage*, and it matters for the model
below, not for the shipped renderer. Applying an inverse-transfer LUT on top of
error diffusion would double-correct.

## The model

Yule-Nielsen fitted to every patch, each carrying its exact ink histogram (we
generate the rasters, so coverage is known rather than estimated):

```
n = 1.51        mean ΔE76 1.72   over 975 mixed patches
                linear-mixing baseline 5.74  ->  removes 70 % of the error
```

Per config, `n` reproduces the ordered-versus-diffused split found earlier by a
different route — ordered dithers clump ink and print heavier:

| config | n | ΔE | patches |
|---|---:|---:|---:|
| atkinson_serp_bw | 1.63 | 2.68 | 12 |
| fs_cam16ucs_hue_aware | 1.47 | 2.15 | 97 |
| atkinson_hue_aware | 1.47 | 2.23 | 577 |
| atkinson_oklab_hue_aware | 1.46 | 2.44 | 72 |

A 1.72 ΔE residual against a 0.35 ΔE noise floor means the model is still missing
something real — most likely that `n` varies with coverage rather than being
constant across the arch. Spectral fitting (spectra are recorded for every
reading) and coverage-dependent `n` are the obvious next refinements.

## Grey neutrality by LUT

What a *requested neutral* actually becomes, over every true-neutral patch:

| config | n | cast (chroma) | a\* | b\* |
|---|---:|---:|---:|---:|
| **atkinson_serp_euclidean** | 13 | **6.26** | +0.47 | −3.65 |
| atkinson_serp_hue_aware *(production's LUT + algorithm)* | 13 | 6.31 | +0.52 | −3.84 |
| atkinson_serp_oklab_hue_aware | 13 | 6.87 | −1.11 | −2.46 |
| atkinson_serp_cam16ucs_hue_aware | 13 | 7.56 | −0.98 | −1.92 |
| atkinson_serp_cam16ucs | 13 | 7.79 | −1.08 | −1.69 |
| *bw variants* | 13 ea | 8.11 – 8.41 | +0.9…+1.5 | −6.4…−6.7 |
| **atkinson_serp_oklab** | 13 | **10.11** | −3.94 | −0.42 |

Three things fall out.

**`euclidean` and production are tied** — 6.26 versus 6.31, a difference of 0.05
against a 0.35 noise floor. There is no grey argument for switching.

**Plain `oklab` is the worst option measured**, at 10.11 — worse than
black-and-white-only. It lands at a\* −3.94: greys go green, because it recruits
the most chromatic ink available. This is the LUT that the skin-tone metric alone
would have selected.

**The `bw` variants are strongly blue** (b\* ≈ −6.5), which is the black ink's own
violet bias showing through undiluted. That is exactly what the hue-aware
machinery exists to cancel.

## The LUT question

The work started from a real complaint: faces looked blue under every setting
except oklab. A long chain of measurement appeared to confirm it, identify
`hue_aware` as the cause, and recommend a replacement — three times, converging on
three different answers. **All three were wrong, for one reason.**

Every LUT comparison was run at `hue_cutoff_deg=30, neutral_chroma=12`. Production
ships **`95, 8`** ([`presets.py`](../../../../python/hokku/webserver/presets.py)),
and those two numbers *are* the hue-aware gate. At the shipped values `hue_aware`
is **bit-identical to `euclidean` on skin colours**. The entire effect being
characterised, and every fix proposed for it, belonged to a configuration that has
never shipped.

Re-run correctly, the blue shift on skin is −5.61 (general) and −4.77 (faces), not
the −9.46 originally reported. `oklab` does give the least blue skin (−1.15),
vindicating the original human observation. But shown side by side on the glass,
the verdict was that **production looks better**: oklab reads "too light,
overlighted", and skin looks "more human" with the shipped config. That matches
whole-image ΔE (**17.16** production versus **18.27** oklab) and contradicts the
blue-shift metric the earlier analysis was optimising.

**The ΔE column predicted human preference; the blue-shift column did not.**

Four candidate improvements were put on glass over this campaign. Three —
`cam16ucs`, `euclidean`, `oklab` — were rejected on sight by a human. The one that
survived was the DRC bug. The pattern is worth keeping: *preference* arguments
lost every time, and the *correctness* argument won.

### What was never in doubt

The palette anchors, gamut, contrast, black-ink non-neutrality, dot-gain curve and
Yule-Nielsen fit involve no hue gate and were unaffected by the error above.

Separately, replacing `palette_measured_rgb` with measured anchors — which looked
like the obvious first fix — changes almost nothing (skin ΔE 15.17 → 14.32, greys
12.25 → 12.23, gamut 50.34 → 50.48), all inside the model's own residual.
Aggregate error is dominated by **gamut limits no anchor choice can move**.

## Rig notes that cost real time

- **The dark calibration expires after exactly one hour.** Not physics — a
  hardcoded `DCALTOUT` in ArgyllCMS `spectro/munki_imp.c`, compared against
  elapsed time with nothing measured. The *white* calibration lasts 24 h, so the
  dial rotation is not about the tile. This sets the whole campaign's rhythm:
  ~55 usable minutes per human-attended cycle.
- Passing a reading proves a calibration is valid *now*, not that any life
  remains. `tools/f7_calibrate.py` records the time; the runner refuses to start a
  cycle it cannot finish.
- **`Communications failure` right after a successful calibration is a wedged USB
  endpoint, not a dial problem.** Re-seating the dial does not fix it; unplugging
  and replugging the instrument does. Diagnosed the slow way.
- Intermittent `Communications failure` mid-run is normal (~2 per 114 reads) and
  self-clearing. Do not treat one as fatal — but do not classify it as calibration
  expiry either, since spotread's own banner contains the word "calibration" and
  matching on it halved every cycle for a while.
- The 6 mm aperture spans only ~3.8 Bayer periods at this pixel pitch, which
  sounds marginal; simulating all 64 sub-tile phases bounds the sampling bias at
  ~0.004 coverage, an order of magnitude below the effects being measured.

## What this dataset is now for

Coverage is complete: 1434 of 1436 planned patches (the 2 uncollected are drift
controls, dropped deliberately). Every reading carries its full 380–730 nm
spectrum.

The dense gamut phase — 729 patches spanning the reachable volume — was collected
to support a **3-D correction LUT**, mapping requested sRGB to the coverage that
actually produces it. That build has not been started. It is entirely offline: no
panel, no meter, no calibration window.

The same campaign has since been run on the
[Huessen EPF1301](../../huessen_epf1301/measurements/findings.md) — same tools,
same protocol, 1733 readings, directly comparable. Its tone response is
meaningfully more linear than this panel's (roughly half the dot gain), while
this panel is the more stable of the two on red and green.
