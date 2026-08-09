# What the measurements say, and what to do about it

> **RETRACTED (LUT sections below).** Every LUT comparison in this document was
> measured with `hue_cutoff_deg=30, neutral_chroma=12`. Production ships
> `95, 8` (`presets.py`), and those two numbers ARE the hue-aware gate. At the
> shipped values `hue_aware` is bit-identical to `euclidean` on skin colours, and
> the large blue shift reported below does not occur: production measures −5.61
> db* (general) and −4.77 (faces), not −9.46.
>
> Re-run at the correct parameters, `oklab` does give the least blue skin
> (−1.15 vs −5.61) — vindicating the original human observation that "all but
> oklab make lips blue". But shown side by side on the panel, the human verdict
> was that **production looks better**: oklab reads "too light, overlighted",
> and skin looks "more human" with the shipped config.
>
> That matches whole-image ΔE (17.16 shipped vs 18.27 oklab) and contradicts the
> blue-shift metric this document optimises. **The ΔE column predicted
> preference; the blue-shift column did not.** No LUT change is recommended.
>
> What survives untouched: the ink primaries, gamut, contrast, black-ink
> non-neutrality, the dot-gain curve and the Yule-Nielsen model — none of which
> involve the hue gate.

Working notes from characterising the Bigme F7 panel with an X-Rite ColorMunki
Photo (ArgyllCMS `spotread`, reflective 45°/0°). Raw readings live alongside this
file; the tools are `tools/color_*.py`.

## The headline: skin goes blue because of `hue_aware`

The complaint that started this work was that faces render with a blue cast under
every dither setting except oklab. That is now measured, and the cause is narrower
than "the colour space".

Blue shift is how far warm skin moves along the blue-yellow axis (negative =
bluer than it should be), predicted from the fitted model with everything except
the LUT held fixed:

| LUT | blue shift | LUT | blue shift |
|---|---:|---|---:|
| oklab | −1.72 | cam16ucs_hue_aware | −9.11 |
| cam16ucs | −2.73 | **hue_aware** *(production)* | **−9.46** |
| euclidean_weighted | −3.92 | oklab_hue_aware | −10.09 |
| euclidean | −4.03 | hue_aware_weighted | −10.19 |

The pattern is the **`hue_aware` modifier**, not the space it is applied to:

| base | plain | + hue_aware |
|---|---:|---:|
| euclidean | −4.03 | −9.46 |
| oklab | −1.72 | −10.09 |
| cam16ucs | −2.73 | −9.11 |

Adding it multiplies the shift in every space. That is consistent with the known
defect that its gate tests the *drifted* value's hue rather than the source
pixel's, so warm tones misfire and recruit blue ink.

### Confirmed on photographs, where it is worse

Swatch results have misled in this project before, so this was re-run on
portraits, predicting perceived colour by locally averaging ink coverage (the eye
integrates dots) and applying the model per position. Δb\* on skin pixels:

| portrait | skin px | hue_aware | oklab | cam16ucs | euclidean |
|---|---:|---:|---:|---:|---:|
| Robert De Niro | 33.5 % | **−16.38** | +1.56 | +0.69 | −0.33 |
| Anna Unterberger | 15.6 % | **−19.53** | −8.26 | −8.50 | −9.72 |
| Wayuu woman | 30.2 % | **−7.05** | −0.91 | −1.09 | −2.01 |

Same ordering on all three, at every blur radius, and **stronger** than the
swatch estimate of −9.46.

### What it costs elsewhere

Changing the LUT moves every image, so:

| metric | hue_aware (now) | oklab | cam16ucs |
|---|---:|---:|---:|
| skin ΔE | 15.17 | 14.67 | **12.39** |
| skin \|Δhue\| | 46.77 | 57.27 | **36.33** |
| skin blue shift | −9.46 | **−1.72** | −2.73 |
| greys ΔE | 12.25 | 14.65 | **11.74** |
| gamut ΔE | 50.34 | 47.62 | **45.27** |

**`cam16ucs` is better or equal on every stable metric** while nearly matching
oklab on blue shift. The apparent exception (greys \|Δhue\|: 90 vs 65) is not
meaningful — at near-zero chroma the hue *angle* is numerically unstable, so
greys ΔE and Δchroma are the columns that count.

### And it is better on whole images, not only faces

A global LUT change needs a global check. Whole-image mean ΔE over the 12 readable
test images:

| | hue_aware (now) | oklab | cam16ucs | euclidean |
|---|---:|---:|---:|---:|
| mean over 12 images | 19.34 | 18.95 | **17.37** | 17.31 |

`cam16ucs` beats production on **11 of 12** images (the exception is the Wikipedia
logo, by 0.08). The gains are not confined to faces — the RGB corner gradient
improves 40.22 → 33.84 and the Albi panorama 20.69 → 16.55.

This is also what disqualifies **oklab** as a global default, which the skin
metric alone would have chosen: despite the best blue shift, it is *worse* than
production on the Dürer hare, Berlin Wall, Fitz Roy, the B&W forest road, the
grayscale bar and the Wikipedia logo. It fixes skin by spending accuracy
everywhere else.

### Measured greys overturn cam16ucs — the answer is `euclidean`

The LUT recommendation above rested on the model. Measuring what a requested
*neutral grey* actually becomes changed it. Mid-range, production algorithm:

| config | grey cast | | config | grey cast |
|---|---:|---|---|---:|
| **euclidean** | **3.56** | | cam16ucs | 5.55 |
| hue_aware | 3.64 | | bw | 6.36 |
| oklab_hue_aware | 4.91 | | **oklab** | **9.12** |
| cam16ucs_hue_aware | 5.26 | | | |

Two things fall out. Plain **cam16ucs costs grey neutrality** (5.55 vs 3.64), so
it was buying skin at the price of neutrals. And plain **oklab is catastrophic on
greys** — 9.12, worse than black-and-white-only, landing at a\* −8.0 because it
recruits the most chromatic ink of any option. Greys go green. That is the LUT the
skin metric alone would have picked.

`bw` having the *worst* cast of the simple options, and it being blue (b\* −5.17),
is the black ink's own violet bias showing through undiluted — which is exactly
what the hue-aware machinery exists to cancel, and why it wins on greys while
losing badly on skin.

The ranking is identical using only freshly-measured patches (23 of 39 were
inherited from byte-identical rasters), so the dedup did not manufacture it.

**Recommendation: `euclidean` for `general` and `faces`.**

| metric | hue_aware (now) | cam16ucs | euclidean |
|---|---:|---:|---:|
| grey cast (measured) | 3.64 | 5.55 | **3.56** |
| whole-image ΔE, 12 photos | 19.34 | 17.37 | **17.31** |
| skin ΔE | 15.17 | 12.39 | **12.38** |
| skin \|Δhue\| | 46.77 | 36.33 | **32.11** |
| skin blue shift | −9.46 | **−2.73** | −4.03 |

Best or tied-best on four of five, and its one loss is still less than half of
production's blue shift. Notably it is also the *simplest* option — plain CIELAB
nearest-neighbour, no hue machinery and no perceptual colour space. Every more
elaborate alternative measured worse.

**Validate on real glass before shipping** — the non-grey evidence is still
model-based, with the tonal chain switched off, and it scores colour, not texture.

## What did NOT turn out to matter: the palette anchors

`palette_measured_rgb` came from a third-party table (`epdoptimize`), not this
glass, and is wrong by mean ΔE 13 after white-normalising (worst: red, 24). It
looked like the root cause, and it was recommended as the first fix.

That was wrong. Substituting measured anchors changes almost nothing:

| set | current | measured (white-normalised) |
|---|---:|---:|
| skin | 15.17 | 14.32 |
| greys | 12.25 | 12.23 |
| gamut | 50.34 | 50.48 |

All inside the model's own ~2.5 ΔE residual, and the normalised anchors make
greys clearly worse (\|Δhue\| 65 → 89). Absolute (un-normalised) anchors are worse
everywhere — unsurprisingly, since they tell the quantiser its brightest ink is a
mid grey.

Why: aggregate error is dominated by **gamut limits** no anchor choice can move.
The gamut set scores ΔE 50 whatever palette is used.

## The panel itself

| | |
|---|---|
| paper white | Y 37.7 %, L\* 67.8 |
| black | Y 1.18 % |
| contrast | 31.9 : 1 |
| gamut area | 55 % of sRGB, 18.5 % of the visible locus |
| repeat-to-repeat noise | 0.1–0.4 ΔE |

Black ink is **not neutral** — a\* +10.8, b\* −10.6, a violet-blue — so a grey
dithered from black and white drifts steadily off-neutral as it darkens.

Chromaticity flatters the panel: red reaches its chromaticity only at Y 4.5 % and
blue at Y 7.6 %, against a 37.7 % white. The usable *volume* is far smaller than
the xy outline suggests, which is why gamut mapping is a design decision rather
than something measurement settles.

## The model

Yule-Nielsen fitted to every patch (each carries its exact ink histogram, since we
generate the rasters):

```
n = 1.62        mean ΔE76 2.49   (linear-mixing baseline 5.23)
                -> removes 52 % of the error
```

Per config, `n` independently reproduces the ordered-vs-diffused split found
earlier by a different route:

| config | n | config | n |
|---|---:|---|---:|
| bayer (ordered) | 1.90 | atkinson | 1.61 |
| stucki_serp | 1.74 | fs_cam16ucs_hue_aware | 1.47 |
| floyd_steinberg | 1.69 | atkinson_hue_aware | 1.45 |

A 2.49 ΔE residual against a 0.3 ΔE noise floor says the model is still missing
something — most likely that `n` varies with coverage rather than being constant
across the arch. Spectral fitting (spectra are now recorded) and per-config `n`
are the next refinements.

## Rig notes that cost real time

- **The dark calibration expires after exactly one hour.** Not physics — a
  hardcoded constant, `DCALTOUT` in ArgyllCMS `spectro/munki_imp.c`, compared
  against elapsed time with nothing measured. The *white* calibration lasts 24 h,
  so the dial rotation is not about the tile.
- Passing a reading proves the calibration is valid *now*, not that any life
  remains. Starting a cycle on a nearly-expired calibration wastes the cycle;
  `tools/f7_calibrate.py` records the time and the runner refuses a stale one.
- Intermittent `Communications failure` from the ColorMunki is normal (~2 per 114
  reads) and self-clearing. Do not treat one as fatal.
- The 6 mm aperture spans only ~3.8 Bayer periods at this pixel pitch, which
  sounds marginal, but simulating all 64 sub-tile phases bounds the sampling bias
  at ~0.004 coverage — an order of magnitude below the effects being measured.
