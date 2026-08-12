# Bigme F7 panel measurements

Instrument readings taken off real glass. Raw data lives here so a later analysis
can be re-run without repeating a physical session.

Two datasets, in the order they were taken:

- **[`data/`](data/)** — the full campaign. 1700 readings with spectra over 20
  sessions, covering 1434 planned patches across eight phases (tone, LUTs,
  algorithms, skin, dense gamut). This is the one to analyse.
- **`f7_panel_2026-08-08.json`** — the first absolute-colorimetry run, described
  below. Superseded in coverage, but it is what the dot-gain and contrast numbers
  in this file were derived from, so it stays.

## `f7_panel_2026-08-08.json`

First absolute-colorimetry run on the F7. X-Rite ColorMunki Photo (s/n 2017853)
via ArgyllCMS 3.5.0 `spotread -O -N -i D65`, reflective 45°/0°, instrument
calibrated against its internal white tile immediately before the run.

Method: the meter was **clamped in one position** on the glass and never moved.
Each measurement field therefore fills the WHOLE panel — 6 flat inks, then 7
Bayer levels at exactly k/8 black coverage — uploaded one at a time over USB with
the `frame` protocol (`tools/color_measure_f7.py`). This is the meter-can't-move
counterpart to the hand-aimed grid in `color_target.py`; it removes patch-aiming
as an error source entirely. Firmware 1.2.11, poll URL parked so nothing could
repaint mid-run.

XYZ is percent-scaled (Y=100 for a perfect diffuser), two readings per field
except `ramp 5/8` which has one — the second lost the instrument to a transient
`Communications failure` and was not retried. Repeatability across held-still
reads was ~0.4 %, so a single reading there is not a material loss.

### Results

Paper-white is **Y = 37.8 % (L\* 67.9)** and black **Y = 1.18 %** — a contrast
ratio of **31.9 : 1**. That is the physical ceiling: nothing the render pipeline
does can produce a brighter white or a deeper black than the ink itself.

**Tone response is strongly non-linear.** The dither pipeline assumes palette
inks mix linearly by area. They do not:

| nominal | measured Y% | linear Y% | effective coverage | dot gain | ΔL\* |
|--------:|------------:|----------:|-------------------:|---------:|-----:|
| 0.125 | 30.38 | 33.21 | 0.202 | +0.077 | 2.3 |
| 0.250 | 23.99 | 28.63 | 0.377 | +0.127 | 4.4 |
| 0.375 | 17.47 | 24.06 | 0.555 | +0.180 | 7.3 |
| 0.500 | 11.84 | 19.48 | 0.709 | **+0.209** | 10.3 |
| 0.625 | 8.87 | 14.91 | 0.790 | +0.165 | 9.8 |
| 0.750 | 6.28 | 10.33 | 0.861 | +0.111 | 8.3 |
| 0.875 | 3.67 | 5.76 | 0.932 | +0.057 | 6.3 |

A 50 % dither renders like **71 %** ink coverage — ΔL\* ≈ 10, far above any
just-noticeable threshold. The curve is the classic dot-gain arch, largest in the
mid-tones and closing at both ends, so it cannot be corrected by a gain or an
exposure shift; it needs an inverse-transfer LUT applied before quantisation.

**Ink anchors disagree with `palette_measured_rgb`.** Comparing absolute
measurements to the palette directly is invalid — the palette encodes a rendering
intent with white near sRGB white, while paper-white here is L\* 67.9. After
white-normalising (so the panel's own white maps to the palette's white), the
remaining error is hue and chroma:

| ink | palette sRGB | measured, normalised | ΔE76 | Δchroma |
|---|---|---|---:|---:|
| black | (31, 34, 38) | (45, 30, 53) | 15.5 | +14.1 |
| white | (185, 199, 201) | (185, 199, 201) | 0.0 | — |
| yellow | (193, 187, 30) | (205, 199, 0) | 12.7 | +11.9 |
| red | (98, 32, 30) | (142, 30, 23) | 24.4 | +22.4 |
| blue | (35, 63, 142) | (13, 93, 170) | 15.2 | −2.7 |
| green | (53, 86, 58) | (41, 108, 82) | 12.6 | +6.7 |

Mean ΔE76 13.4, worst 24.4 on red. Δchroma is positive almost everywhere: the
real inks are **more saturated** than the palette assumes, red substantially so.

### Caveats before acting on this

- **One unit, one session.** Panel-to-panel spread is unmeasured, so treat these
  as this screen's numbers, not the model's, until a second unit is run.
- **The meter rested on the glass.** 45°/0° geometry rejects the specular
  component, which is the right way round, but contact pressure on a flexible
  panel can shift the surface slightly. Unquantified.
- Changing `palette_measured_rgb` moves every rendered image. The dot-gain
  correction and the anchor update are separate changes and should be evaluated
  separately — applying both at once makes a regression impossible to attribute.
