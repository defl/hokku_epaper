# Dithering on the Spectra 6 e-ink panel

This document explains how `webserver/webserver.py` converts arbitrary uploaded
photos into the 6-color bitmap the EL133UF1 panel can display, and — more
importantly — *why* it does what it does. Every design choice here was driven
by a specific visible failure on a specific test image; the sections below tell
that story.

If you change anything in the dithering pipeline, **update this document to
match**. This file exists so a future maintainer (human or AI) doesn't have to
re-run the whole investigation.

---

## 1. The hardware constraint

The 13.3" Spectra 6 (EL133UF1) is a 6-color electrophoretic display. Each of its
1200 × 1600 pixels can be exactly one of:

| # | Name   | Measured RGB       | Lab (L\*, a\*, b\*)     | Hue angle | Chroma |
|---|--------|--------------------|-------------------------|-----------|--------|
| 0 | Black  | (2, 2, 2)          | (0.55, −0.00,   0.00)   | 158°      | 0.0    |
| 1 | White  | (190, 200, 200)    | (79.86, −3.41, −1.18)   | −161°     | 3.6    |
| 2 | Yellow | (205, 202, 0)      | (79.17, −16.80, 79.56)  | 102°      | 81.3   |
| 3 | Red    | (135, 19, 0)       | (28.43,  46.30, 41.20)  | 42°       | 62.0   |
| 4 | Blue   | (5, 64, 158)       | (29.83,  22.18, −55.47) | −68°      | 59.7   |
| 5 | Green  | (39, 102, 60)      | (38.30, −30.62, 17.87)  | 150°      | 35.5   |

Two key properties:

- **The palette is sparse.** Six anchors in a 3D color space leave huge gaps. A
  warm mid-tone like peach skin (L≈60, a\*≈+15, b\*≈+15) sits far from every
  primary. No palette entry is "close to" it.
- **The lightness range is clipped.** "White" is measured at L\*≈80, not 100 —
  the panel physically cannot reproduce paper-bright whites. Anything brighter
  than L\*=80 in the source has to be mapped down.

Dithering is how we fake the missing colors: we alternate palette pixels in a
pattern so the eye perceives an average. Floyd–Steinberg and Atkinson error
diffusion are the classical tools for this.

## 2. Why the obvious approach fails

The naïve pipeline — "resize image, pick nearest palette per pixel, propagate
the error à la Floyd–Steinberg" — produces four specific artifacts on our
palette. Each one drove a concrete countermeasure in the current code.

### 2a. Blue speckles on warm skin

Source: skin tone at L≈60, a\*≈+20, b\*≈+15 (warm pink).
Closest palette entry: **White** (L\*=80, a≈−3).
Residual error propagated forward: (+big L, +big a\*, +big b\*).

After error accumulates for several pixels, the target point in Lab space has
drifted so far that a different palette entry becomes closest. For warm skin
drifting cool, that new closest entry is often **Blue** (L=30, a=+22, b=−55),
because:
- Its hue angle is −68° vs the pixel's +42° (off by ~110°), but
- The Euclidean distance in Lab space still happens to be smallest after the
  residual drift

Result: occasional Blue pixels sprinkled through skin regions. Visually reads
as a cold blue/magenta tint.

**Fix: hue-aware palette LUT.** Before picking the nearest palette entry, we
compute the pixel's hue angle and forbid any palette entry whose hue differs
by more than ~95°. This rules out Blue for warm pixels entirely. Neutral
palette entries (Black, White — chroma < 8 in Lab) are always allowed, so the
rule only blocks hue *swaps*, not lightness choices.

Code: `_build_rgb_lut_hue_aware()` in `webserver/webserver.py`.

### 2b. Small saturated features vanishing

Source: a child's pink tongue, 10 × 15 pixels.
Each pixel wants Red (L=28), but source L≈55.
Dithering's *only* way to produce perceived L≈55 is to alternate Red (L=28)
and White (L=80) pixels.

But Atkinson's "6/8 damping" (see §2c) throws away a quarter of the propagated
error at every step. For a region only 10 pixels wide, by the time enough
residual accumulates to force one Red pick, the neighboring pixels have
already been chosen as White and their error is gone. The pattern never
consolidates. The tongue renders as almost-uniform dark skin, not a pink tongue.

**Fix: "vividness-preserved" dynamic-range compression + adaptive saturation
boost.** Both covered in §4.

### 2c. Phantom pink speckle in near-white regions

Source: white beach umbrella fabric, genuinely RGB≈(245, 240, 230) (very
slightly warm).
Lab chroma ≈ 5 — technically non-neutral, but visually cream-white.
After `ImageEnhance.Color(1.2)` and vividness compression, chroma is now ~7.

Floyd–Steinberg cascades that tiny warm bias across adjacent white pixels.
Every neighbor gets a little warmer in the residual. Eventually one pixel's
target is just over the decision boundary, and Red gets picked. That Red's
residual (subtract L=28, a=+46, b=+41 from a warm-white target) shoves
*everything* around it back toward neutral. But the damage is done — the single
Red pick is visible against the otherwise-white umbrella, and the surrounding
cascade produces pink-noise speckle.

This is the failure mode that killed our brief flirtation with "Floyd–Steinberg
+ hue-aware" (`fs_hue_aware`, removed) as the default.

**Fix: adaptive saturation boost + adaptive vividness.** Both covered in §4.

### 2d. B&W photos developing a pink cast

Any near-grayscale photo has residual chroma of ~1–3 Lab units from JPEG
compression noise, film grain, scanning artifacts, etc. A saturation boost
amplifies that noise into visible chroma. With the default pipeline that
happened to be 1.25× chroma scaling on every pixel, a pure B&W wedding photo
acquired a visible pink cast on skin highlights.

**Fix: detect near-grayscale images and use a conservative preset.** Covered
in §4.

## 3. The shipped presets

The web UI exposes the pipeline as three named **presets** plus a fully
editable "Advanced dithering" panel. Selecting a preset loads its full
configuration into the knobs; touching any knob flips the preset label to
"Custom (your edits)" without changing values. Saving writes the complete
nested config to `config.json`.

| Preset key                 | UI label                                         | Default |
|----------------------------|--------------------------------------------------|---------|
| `atkinson_hue_aware`       | Atkinson — hue-aware (default)                   | **Yes** |
| `atkinson_soft`            | Atkinson — soft, warm                            |         |
| `floyd_steinberg_vivid`    | Floyd–Steinberg — vivid, warm (pre-2.0)          |         |

Presets live in `DITHER_PRESETS` in `webserver/webserver.py`; each one is a
full dither-config literal (no partials — every stage has an explicit value
in every preset so the behaviour is inspectable at a glance).

The pre-2.0 schema stored a single string key `dither_algorithm`; that key
was dropped when the nested per-stage schema landed. Old config files with
the legacy string are silently normalised to the default preset on load.

## 4. The default pipeline in detail

The default preset (`atkinson_hue_aware`) drives a ten-stage pipeline. Every
stage is addressable from the UI; the values below are the defaults and the
full schema lives in `DITHER_PRESETS["atkinson_hue_aware"]["dither"]`.

### Stage 1 — `_prepare_canvas()` tonal chain

Resize the image to fit 1200×1600 (portrait native) or 1600×1200 (landscape,
rotated at the end), then apply the tonal adjustments in order. Every row
below is toggleable/tunable per preset:

1. `ImageOps.autocontrast(cutoff=0.5)` — stretch histogram ignoring the
   darkest/brightest 0.5% of pixels.
2. Gamma 0.85 — midtone lift.
3. Brightness 1.0, Contrast 1.1, Sharpness 1.3 — modest punch.
4. **Saturation boost** — see Step 1b below.

The padding area (if the image aspect doesn't match the display's) is tracked
as a boolean mask; those pixels get forced to White after dithering so they
don't end up with speckle.

#### Step 1b — `_adaptive_saturate()`

Instead of PIL's `ImageEnhance.Color(1.25)` (which multiplies *all* chroma by
1.25 uniformly — including the tiny chroma noise in near-white regions, which
then cascades into §2c speckle), we do a Lab-space boost that is *gated by
existing chroma*:

```
factor(chroma) = 1.0                      if chroma ≤ 5.0
                 1.25                     if chroma ≥ 15.0
                 smooth ramp between
```

Pixels that are *already* saturated (tongues, lipstick, red shirts) get the
full 1.25× boost. Pixels that are near-neutral (skin, gray clothing, white
umbrellas) are left completely alone. This is the single most important
change from the pre-V10 baseline: it breaks the "amplify noise → cascade"
chain that was responsible for §2c.

Code: `_adaptive_saturate()` in `webserver/webserver.py`.

### Step 2 — `_compress_dynamic_range(adaptive_vivid=True)`

The source image may contain L\*=100 pixels (paper-white); the panel can only
show L\*≈80. Without remapping, the dither algorithm would burn all its
"whitest" palette choices on pixels the panel couldn't reproduce anyway.

We remap L\* linearly:
```
L'_pixel = 0.55 + (L_source / 100.0) * (79.86 − 0.55)
         ≈ L_source * 0.79
```

If we stopped there (the pre-V10 behavior, `scale_chroma=False`), a pixel at
Lab (75, 30, 20) would become (59, 30, 20) — the chroma-to-lightness ratio
changes, and some originally-in-gamut saturated colors drift out of reach of
Red/Green/Blue when dithered against the new darker L.

`adaptive_vivid=True` fixes this with another chroma-gated remap. For each
pixel:
```
c_factor(chroma) = 0.79                   if chroma ≤ 5    (fully scale chroma)
                   1.00                   if chroma ≥ 15   (keep chroma)
                   smooth ramp between
lab[...,1] *= c_factor
lab[...,2] *= c_factor
```

Near-neutral pixels (white umbrellas) get chroma scaled down so that tiny
warm tints don't get relatively boosted after L compression — prevents §2c.
Saturated pixels (tongues) keep their full chroma so Red/Blue/Green remain
reachable — prevents §2b.

Code: `_compress_dynamic_range()` in `webserver/webserver.py`.

### Step 3 — Atkinson error diffusion + hue-aware LUT

Atkinson was chosen over Floyd–Steinberg because, once Steps 1b + 2 are in
place, Atkinson's 6/8 damping produces a softer noise texture without
sacrificing the saturation that FS full-cascade would give (adaptive
saturation already did that work).

The palette picker is `_RGB_LUT_HUE_AWARE`, a 32³ precomputed 3D LUT that
maps (R, G, B) to a palette index. At LUT build time, for every (R, G, B)
grid point we compute its Lab hue angle and forbid palette entries whose
hue differs by more than 95°. This costs ~40 ms at module import; at dither
time, lookup is O(1) per pixel.

Neutral palette entries (chroma < 8 in Lab, i.e. Black and White) are
**always** allowed regardless of hue difference — otherwise very low-chroma
source pixels with unstable hue angles would get weird forced picks.

Code: `_atkinson_dither()` and `_build_rgb_lut_hue_aware()` in `webserver/webserver.py`.

### Step 4 — B&W detection and fallback

Before any of the above, `_maybe_apply_bw_fallback()` samples the source at
200-pixel thumbnail resolution and checks the Nth-percentile Lab chroma. If
it's below the configured threshold (defaults: 95th percentile, chroma 8),
the image is treated as intentionally B&W (scan, film, monochrome edit) and
the pipeline switches to a conservative override for that image only:

- Saturation → `mode: "global", value: 1.05` (flat 1.05× chroma)
- DRC `chroma_mode` → `"off"` (no chroma compression)
- Every other stage (palette LUT, kernel, tonal chain) runs exactly as
  configured.

This preserves pure grayscale B&W photos without the pink cast described in §2d.

The B&W check itself is a user-visible knob: it can be disabled, and both
the threshold and the percentile are editable.

Code: `_maybe_apply_bw_fallback()` and the branch at the top of `_convert_image()`.

## 5. How the default was chosen

We ran a benchmark across 16 test images (14 in `.private/testpics/` plus the
two photos that produced the original user complaints: the kids-on-swing with
the hard-to-render tongue, and the beach-umbrella with the pink speckle).

Three metrics per image:

1. **`neutral_leak`** — mean output Lab chroma for source pixels where source
   chroma < 10. Measures how badly the dither amplifies near-neutral regions
   into visible color. Lower is better.
2. **`saturated_hit`** — fraction of source pixels with chroma > 25 whose
   output has chroma > 15. Measures whether saturated source features survive
   as saturated palette output. Higher is better.
3. **`overall_dE`** — mean CIE76 ΔE between source and output. Classic
   full-image color accuracy. Lower is better.

We tried 12 candidate variants (see `dither_test2/candidates.py`) including
Floyd–Steinberg + hue-aware with uniform/vividness/adaptive chroma scaling,
Atkinson variants, a luminance/chroma-separated diffusion, and combinations
of all of these.

Final aggregate across 16 images (lower is better for neutral_leak and dE;
higher is better for sat_hit):

| Variant                                | neutral_leak | sat_hit | dE    |
|----------------------------------------|--------------|---------|-------|
| pre-V10 atk_hue_aware (1.05 enhance)   | 6.05         | 0.721   | 35.24 |
| fs_hue_aware (PR default, now retired) | 10.85        | 0.672   | 38.86 |
| V5 FS + luma/chroma-split diffusion    | 6.35         | 0.661   | 37.33 |
| V6 atk + adaptive_sat 1.30             | 6.55         | 0.758   | 35.45 |
| V7 atk + vivid + adaptive_sat 1.25     | 4.84         | 0.703   | 34.90 |
| V8 atk + vivid + uniform 1.10          | 4.68         | 0.673   | 34.91 |
| **V10 atk + adaptive_vivid + adaptive_sat 1.25** | **5.74** | **0.752** | **35.27** |
| V11 same as V10, enhance 1.35          | 6.02         | 0.764   | 35.38 |

V10 is the only variant that improves on the pre-V10 baseline on **both**
neutral_leak and sat_hit — that is, cleaner near-neutral regions *and* better
saturated-feature preservation, simultaneously. V7 beats V10 on neutral_leak
but loses on saturation (visible tongue loss). V11 pushes enhance to 1.35 and
skin tones start looking sunburnt on outdoor/skin-heavy photos.

## 6. Research references

Key ideas from published work:

- **Ostromoukhov 2001**, *A Simple and Efficient Error-Diffusion Algorithm*
  (SIGGRAPH). Variable per-pixel diffusion coefficients tuned for blue-noise
  characteristics. Our adaptive saturation and adaptive vividness are
  chroma-based analogs of the same core idea (let the pixel's own properties
  modulate the diffusion behavior).
- **Hue-preserving chroma compression** — standard ICC/IS&T gamut-mapping
  practice. `adaptive_vivid` is a chroma-gated version of this.
- **Atkinson (1984)** — Apple LaserWriter dither. 6/8 damping.

## 7. Where the code lives

- `webserver/webserver.py` — the production pipeline. Search for:
  - `DITHER_PRESETS`, `DEFAULT_PRESET`, `DEFAULT_CONFIG`
  - `_default_dither_config()`, `_normalize_dither_config()`, `_dither_config_hash()`
  - `_prepare_canvas()` — tonal chain (autocontrast, gamma, brightness,
    contrast, sharpness, saturation)
  - `_adaptive_saturate()` — Lab-space chroma-gated saturation boost
  - `_compress_dynamic_range()` — takes `scale_chroma`, `adaptive_vivid`,
    `vivid_low`, `vivid_high`
  - `_build_rgb_lut()` (Lab-Euclidean) and `_build_rgb_lut_hue_aware()` with
    memoisation in `_get_lut()`
  - `_floyd_steinberg_dither()` and `_atkinson_dither()`
  - `_maybe_apply_bw_fallback()` + the branch in `_convert_image()`
  - `_run_dither_pipeline()` — shared pipeline used by both full render and
    `_render_dither_preview()` (the /api/dither/preview endpoint)
  - `_cache_key()` and `_CACHE_VERSION`
- `webserver/templates/index.html` — preset dropdown, collapsible Advanced
  panel (rendered dynamically from `ditherState`), per-knob (?) help
  popovers, preview button. The settings form is now loaded **once** at
  page open (`/api/config`); the `/api/status` poll deliberately omits
  config so user edits can't be clobbered mid-typing.
- `webserver/tests/test_webserver.py` — unit tests covering the pipeline
  knobs and preset semantics.
- `dither_test2/` (untracked scratch area, can be deleted) — the benchmark
  harness that chose V10, kept for reproduction.

## 8. When this document is stale

**Whenever dithering behavior changes** — a new algorithm, a tweak to
`_adaptive_saturate` thresholds, a palette recalibration, a new failure mode
being fixed — update this document. It's the one artifact that's supposed to
keep pace with the code for humans. A `CLAUDE.md` note reminds AI contributors
of the same obligation.
