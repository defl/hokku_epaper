# Image quality metrics for the Spectra 6 dither pipeline

This document explains `image_compare()` in
`webserver/hokku_server/image_quality.py`, what each metric actually measures,
how to interpret the numbers, and what a "good" result looks like given the
constraints of the 6-colour panel.

If you change any metric definition, add a metric, or change the thresholds
used to classify pixels as neutral or saturated, **update this document to
match**. `AGENTS.md` carries the same reminder.

---

## 1. Why a dedicated comparator?

Evaluating dither quality visually is slow and subjective. The same image
rendered with Floyd-Steinberg, Atkinson, and Stucki looks plausibly fine at a
glance; the differences only become apparent when you look at a grey sky and
notice that one preset speckles it with tiny blue dots while another keeps it
clean.

`image_compare()` gives those differences a number. It takes:

- **`original`** — the source image resized and letterboxed to panel dimensions
  (1200 × 1600), *before* any pipeline enhancements are applied (no adaptive
  saturation, no dynamic-range compression, no gamma/contrast).
- **`derived`** — the dithered output at the same size, with each pixel
  replaced by the measured RGB of the ink that was actually chosen
  (`PALETTE_MEASURED_RGB`, *not* the punchier preview palette).

Both arguments are H × W × 3 arrays, uint8 or float, in the 0–255 range.
Passing measured-ink RGB for the derived image is important: the preview palette
uses punchier colours to make the browser preview look good, but the panel
physics are captured by `PALETTE_MEASURED_RGB`. Metrics computed against the
preview palette would be systematically wrong.

The function returns a plain `dict[str, float]` with ten keys, described below.

---

## 2. Color space foundation

Nearly every metric works in **CIE L\*a\*b\*** (Lab), not in RGB. Lab is a
perceptually uniform space where equal numerical distances correspond to equal
perceived colour differences (approximately). Its three axes are:

- **L\*** — perceptual lightness, 0 (black) to 100 (diffuse white).
- **a\*** — green–red axis, negative = green, positive = red.
- **b\*** — blue–yellow axis, negative = blue, positive = yellow.

**Chroma** (C\*) is the distance from the grey axis:

    C* = sqrt(a*² + b*²)

A pixel with C\* = 0 is a perfect neutral (grey). A pixel with C\* = 60 is
strongly saturated. The Spectra 6 palette covers a chroma range of 0 (black,
white) to ~81 (yellow).

**Hue angle** (h°) is the angle around the grey axis:

    h = atan2(b*, a*)  [degrees, 0–360]

All conversions use the sRGB → linear → XYZ → Lab path with the D65 white
point, which is standard and matches the pipeline's own colour math.

---

## 3. Metric reference

### 3.1 `neutral_leak`

**Lower is better.**

The mean chroma (C\*) of derived pixels whose *source* pixel is near-neutral
(source C\* < 10). A grey sky, a white wall, a desaturated background — these
should all come out of the pipeline as black, white, or a mixture of the two,
not as a scattering of coloured ink dots.

When this number is high it means coloured palette entries (typically blue,
which sits close in L\* to the white ink) are being scattered into regions that
the human eye reads as neutral. The result is a visible blue or green tint in
what should be grey.

Reference values from the 10-image test suite:

| Preset               | neutral_leak |
|----------------------|:------------:|
| atkinson_hue_aware   |     6.67     |
| atkinson             |     7.95     |
| stucki_hue_aware     |    10.48     |
| floyd_steinberg      |    12.10     |

The hue-aware LUT variants score meaningfully better here because they
explicitly forbid palette entries whose hue angle differs too much from the
source pixel's hue. For a near-neutral source pixel the hue-aware LUT will
refuse to assign blue ink even if blue is the closest ink in raw L\* distance.

---

### 3.2 `neutral_blue_fraction`

**Lower is better.**

The fraction of source-neutral pixels (source C\* < 10) whose derived pixel is
*specifically* blue ink (palette index 4, measured RGB ≈ (5, 64, 158)). This is
a more surgical version of `neutral_leak`: where `neutral_leak` measures how
much chroma in general bleeds into neutral areas, `neutral_blue_fraction` says
*exactly what fraction of the problem is blue*.

The two metrics are related but distinct. A preset could have moderate
`neutral_leak` because it sometimes assigns green ink to neutral areas (green
has lower chroma than blue), but `neutral_blue_fraction` of 0.0 because green
is not blue. The Spectra 6 has a particularly harsh blue ink whose CIE L\* is
only 29 — close to black — so it tends to creep into shadowed neutral regions
where the pipeline is torn between black and blue.

A value of 0.02 means 2 % of pixels that should be grey have been printed with
blue ink. In practice, values above 0.05 are visually noticeable.

---

### 3.3 `sat_hit`

**Higher is better.**

The fraction of source-saturated pixels (source C\* > 25) that remain
saturated in the derived image (derived C\* > 15). The thresholds are generous
deliberately: the panel has a narrower gamut than most source images, so some
chroma loss is unavoidable and expected. This metric asks whether *most* of the
colour survives.

A value of 0.75 means 75 % of colourful source pixels are still visibly
colourful in the output. The remaining 25 % were mapped to black or white. On
the Spectra 6, a scene with a lot of sky-blue or mid-tone orange will always
have some pixels fall outside the gamut of the six inks; `sat_hit` ≤ 1.0 is
normal and does not indicate a bug.

A low `sat_hit` combined with a low `neutral_leak` often means the pipeline is
playing it safe: it avoids blue noise in greys by refusing to use coloured inks,
but this conservatism then bleeds into genuinely colourful areas and desaturates
them. The Atkinson preset finds a better balance than Floyd-Steinberg here.

---

### 3.4 `overall_dE` (CIE76 ΔE)

**Lower is better.**

The mean Euclidean distance in Lab space between every source pixel and its
corresponding derived pixel:

    ΔE76 = sqrt((ΔL*)² + (Δa*)² + (Δb*)²)

CIE76 is the simplest and oldest ΔE formula. Its main weakness is that it is
not truly perceptually uniform: a difference of ΔE76 = 10 in a saturated blue
region looks smaller to a human than the same ΔE76 = 10 in a neutral grey
region. Despite this it is still widely used as a first-pass sanity number.

Rule of thumb: ΔE76 < 1 is imperceptible, 1–3 is noticeable to a trained
observer, > 5 is clearly visible. For a 6-colour quantised output, values in
the 25–40 range are typical and do not indicate a problem — the panel simply
cannot reproduce most source colours exactly.

---

### 3.5 `overall_dE2000` (CIE ΔE2000)

**Lower is better.**

The mean CIE ΔE2000 across all pixels. ΔE2000 is the current industry standard
for perceptual colour difference. It applies different weights to lightness,
chroma, and hue differences, and includes a rotation correction term (RT) that
accounts for the perceptual non-uniformity of blue colours. In practice:

- Lightness differences in dark regions count for less than in bright regions.
- Hue differences in low-chroma (desaturated) colours count for less than in
  vivid colours.
- Large chroma differences in the blue hue angle range are weighted down
  because human vision is less sensitive to blue chroma than to red or green
  chroma.

The formula is significantly more expensive to compute than CIE76 (about 20
intermediate variables per pixel) but the resulting number is a better proxy
for "how bad will this look to a human". For the same pair of images,
`overall_dE2000` is typically 10–30 % lower than `overall_dE` because ΔE2000
discounts some of the differences that ΔE76 over-counts.

---

### 3.6 `lightness_dE`

**Lower is better.**

The mean absolute lightness difference |ΔL\*|. This isolates the luminance
component from the colour component. If `lightness_dE` is high relative to
`overall_dE`, the pipeline is getting brightness wrong (under- or
over-exposure). If `lightness_dE` is low but `chroma_dE` is high, the
brightness is right but the colours are off.

For a black-and-white source image, `lightness_dE` captures most of the error
since there is no chroma to preserve. A `lightness_dE` above ~15 on a B&W image
suggests the dynamic-range compression is miscalibrated.

---

### 3.7 `chroma_dE`

**Lower is better.**

The mean absolute chroma difference |ΔC\*|. This is the saturation error:
how much the pipeline reduces the vividness of colours on average. A
`chroma_dE` of 20 on a colourful photo means the colours are, on average, 20
chroma units flatter than the source — moderate and expected given the panel's
gamut. A `chroma_dE` of 40 would mean almost all colour is gone.

Combined with `sat_hit`, these two metrics tell the saturation story from
different angles: `sat_hit` is a pass/fail binary across a threshold;
`chroma_dE` measures the continuous average loss.

---

### 3.8 `hue_error`

**Lower is better.**

The mean absolute hue angle difference (in degrees) between source and derived
pixels, computed only for *source-saturated* pixels (source C\* > 25). Hue
errors are perceptually important for saturated colours: a red tomato rendered
orange, or a blue sky rendered cyan, is a clearly wrong result even if the
chroma and lightness are preserved.

Hue differences are computed on a wrapped circle so that, for example, the
difference between 350° and 10° is 20°, not 340°.

If there are no saturated source pixels (for example, a greyscale image),
`hue_error` is returned as 0.0 as a sentinel. Do not interpret this as "perfect
hue preservation" for greyscale inputs.

A `hue_error` above ~15° on a colour photograph suggests the palette-assignment
LUT is making hue-inaccurate choices. Values of 5–10° are typical for images
whose dominant hues sit between palette colours.

---

### 3.9 `error_roughness`

**Lower is better.**

The standard deviation of the per-pixel ΔE76 values. Where `overall_dE` tells
you the *average* error, `error_roughness` tells you how *uneven* that error is.

A dither with low `overall_dE` but high `error_roughness` has a few pixels that
are very wrong and many that are nearly right — this produces a visibly speckled
or grainy result. A dither with the same `overall_dE` but low `error_roughness`
distributes the error more evenly across the image, which the human eye finds
less jarring.

Floyd-Steinberg tends to concentrate error into diagonal "worm" patterns with
strong local correlation, leading to higher `error_roughness`. Atkinson's 75 %
damping spreads energy more broadly and scores better here on most images.

---

### 3.10 `high_freq_energy_ratio`

**Higher is better.**

The fraction of the L\* quantisation error's spatial energy that lies above a
spatial frequency threshold. In other words: is the error concentrated at fine
scales (high frequency = blue noise, ideal) or at coarse scales (low frequency =
visible blotches, worms, bands)?

The implementation:

1. Compute the L\* error image: `derived_L - original_L` at every pixel.
   Padding pixels (if `padding_mask` is supplied) are zeroed out.
2. Apply a 2D FFT to the error image and shift so DC is at the centre.
3. Compute the radius from centre for each frequency bin.
4. Split at `min(H, W) / 4` as the low/high cutoff.
5. Report `high_energy / total_energy`.

A checkerboard pattern (maximum possible spatial frequency) achieves a ratio
near 1.0. A constant offset (pure DC error) achieves a ratio near 0.0. Typical
well-dithered images fall in the 0.55–0.75 range.

This metric captures what listeners call "grain" in audio and viewers call
"graininess" in print. The ideal dither has all of its error energy at spatial
frequencies the human visual system cannot resolve at normal viewing distance —
roughly above 30 cycles per degree. Whether that threshold is above or below
the `min(H,W)/4` cutoff depends on display size and viewing distance; the cutoff
is a reasonable default for the 13.3" Spectra 6 viewed at arm's length.

---

## 4. The `padding_mask` parameter

The letterbox fit used by the rendering pipeline adds white borders when the
source image has a different aspect ratio than the 4:3 panel. These padding
pixels are trivially "correct" (white source → white ink) and would artificially
improve all metrics if included in the averages. Pass the same padding mask that
`_prepare_canvas` computes to exclude them:

```python
src_arr, padding_mask = _fit_source_to_panel_rgb(src_path)
src_lab = rgb_to_lab(src_arr)
idx = render_panel_bytes(img, cfg, "landscape")
out_rgb = PALETTE_MEASURED_RGB[panel_bytes_to_indices(idx)]
metrics = image_compare(src_arr, out_rgb, padding_mask=padding_mask)
```

The helper `_fit_source_to_panel_rgb` in `test_dither_quality.py` replicates
the pipeline's letterbox geometry without applying any pipeline enhancements, so
the padding mask it returns is spatially aligned with the rendered output.

---

## 5. Running the comparator

The comparator is wired into two test targets:

**`test_dither_quality_metrics`** (slow, `time_intensive` marker) — loops over
every test image in `images/test/` × every named preset, renders with
`NumbaStreamingDither` (production path), and prints a table to stdout plus
files to `build/test_dither_metrics/`:

```
pytest -m time_intensive -s -k test_dither_quality_metrics tests/test_dither_quality.py
```

Output files:
- `build/test_dither_metrics/metrics.txt` — human-readable table, per-image and aggregate.
- `build/test_dither_metrics/metrics.tsv` — spreadsheet-importable, one row per image × preset.

**`test_dither_full_scale`** (slow, `time_intensive` marker) — renders every
image × preset × mode (streaming / unconstrained / numba variants) and writes
`<stem>__<preset>__<mode>.metrics.txt` sidecar files alongside the PNG output
in `build/test_dither_full/`. This lets you open a PNG and immediately read its
three-line metric summary.

---

## 6. Reference numbers

Benchmark results for all production presets are kept in
[docs/dithering.md § 13](dithering.md#13-benchmark-results).

---

## 7. When this document is stale

Update this document if:

- A new metric is added to or removed from `image_compare()`.
- A metric's definition changes (threshold values, formula, axis choice).
- The set of quality goals changes (e.g. a new failure mode is identified that
  the existing metrics do not capture).

The implementation lives in `webserver/hokku_server/image_quality.py`.
Unit tests live in `webserver/tests/test_image_quality.py`.
Integration into the dither quality tests lives in
`webserver/tests/test_dither_quality.py`.
