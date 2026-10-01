# The colour and rendering campaign

A closed investigation into how this panel should render photographs, run from
"the reds look wrong" to a measured answer. It spans a spectrophotometer
campaign, a rebuild of the tonal chain, a gamut-correction LUT, a camera rig
pointed at real glass, and four evenings of blind rating.

**The last phase concluded that nothing beats what the pipeline already does.**
Twenty-one alternative renderings were photographed off the panel and judged
blind against the shipped configuration; not one won, and three were
significantly worse. That is a real result, and the earlier phases are why: by
the time the search ran, the pipeline had already been moved to where the
measurements pointed.

This document is the whole arc, so none of it has to be re-derived. The raw
spectrophotometer dataset and its own conclusions live in
[measurements/findings.md](measurements/findings.md); the metric definitions
live in [image_quality.md](image_quality.md). Neither is repeated here.

---

## 1. Getting pixels onto the glass

Nothing downstream was possible until the panel could be driven deliberately
rather than through the normal fetch-and-sleep cycle.

**`frame` upload over USB.** A console command that accepts a full 960 KB
panel-native frame, CRC-checked, and refreshes. Calibration targets and test
renders go to the glass exactly as authored, with no server, no network and no
scaling in between. Colour measurement is USB-only for this reason.

**USB-interactive mode**, because the console was a race by construction. A
screen's job is to wake, fetch and sleep, and every one of those steps takes the
console away — the ESP32 boards reboot for each refresh and drop USB entirely. A
host driving hundreds of consecutive uploads could only poll harder and hope to
land in a gap, which one bring-up session spent 28 minutes and a machine reboot
proving is not unlikely enough. `interactive on` asks the screen to stop deciding
things for itself.

Two properties make that safe to expose. The flag is plain RAM — not NVS, not
RTC-retained — so **any reset clears it** and a crashed host cannot leave a
screen permanently mute. And it is ANDed with the caller's own USB reading at the
call site, so **pulling the cable falls straight back to normal behaviour**
rather than sitting awake on battery until flat.

## 2. What the panel actually is

1733 spectrophotometer readings over 20 sessions; all 1505 planned patches
measured. Full write-up in [measurements/findings.md](measurements/findings.md).
The numbers that drive everything below:

| | |
|---|---|
| reachable lightness | **L\* 10.86 – 66.94** |
| contrast | 29.4 : 1 |
| gamut | 41 % of sRGB |
| black ink | a\* +8.56, b\* −12.86 — not neutral |
| Yule–Nielsen | n = 1.38, residual 1.50 ΔE |
| noise floor | 0.656 ΔE |
| peak dot gain | +0.117 at 50 % nominal |

**The first thing it found was a bug.** The palette table implied white at
L\* 79.86 and black at L\* 0.55. The glass measures 66.94 and 10.86 — a reachable
range roughly 20 L\* narrower than the code assumed. On the F7 that same mismatch
had been collapsing half a test portrait into flat black. `drc_anchor_l` now
comes from the measurement.

**Re-illumination validates hard.** Reflectance belongs to the ink, so any
illuminant can be applied afterwards without a meter: recomputing the inks under
D65 from their own spectra reproduces the campaign's measured XYZ to **0.06 ΔE**
worst case against a 0.35 ΔE noise floor. Under the room's actual light
(2706 K, 75 lux, measured SPD), chromatic adaptation moves neutrals barely at all
(1.6–2.0 ΔE) while every chromatic ink shifts 10.3–14.2 ΔE and the a\*b\* hull
loses 18.3 %. For scale: the LUT arguments this project spent months on turn on
skin differences of 1–3 ΔE.

**Tone correction was closed by a person, not a metric.** The naive inverse of
the open-loop dot-gain arch lost 4–0 ("very compressed … ugly") — the
over-correction the dataset predicted. The closed-loop curve was a wash at 2–2,
with the repeat trial flipping on the same image. Neither shipped. But the
*reason* the dataset gave was wrong, and the data says so: error diffusion is
closed over the renderer's model of the panel, not over the panel, so **+3.41 L\*
mean (+5.72 peak) of dot gain survives into real rendered output**. The
conclusion holds only because two errors partly cancel — gamma-space mixing
lightens midtones while dot gain darkens them — not because the loop
self-corrects.

## 3. The tonal chain, settled on glass

Every change in this section was judged on real hardware, on both panels, not
from a simulation.

**Dynamic-range compression: linear-plus-shoulder → bounded S-curve.** The old
map was one slope from shadows through midtones with a tanh shoulder at the top.
Once the anchor range was corrected to the measured (narrow) one, that map went
measurably flat — a linear map cannot have both a correct range and the old
range's punch, because it has only one slope. Replaced with a logistic sigmoid
applied to normalised source lightness before mapping into the anchor range:
steeper through the middle, tapering to zero slope at both anchors so it can
never overshoot the way an unclamped shoulder can, and symmetric, so it pushes
shadows darker and highlights brighter rather than only one end.

**Then swept for its steepness.** k = 8 had been picked on a modelling argument
and never re-validated. On glass against a very dark, mostly-shadow photograph:
k = 8 left a solid-black dress not dark enough; k = 20 overcorrected, flattening
highlights along with shadows because the curve is symmetric; k = 12 and k = 6
both lost to k = 8. **k = 7 won**, and every capture in the later campaign
rendered through it.

**CLAHE off — later partly reversed, see §8.** Local contrast equalisation per
tile renormalises away the global contrast the earlier `Contrast(1.1)` stage had
just added, wherever a region is smooth and the boost is not backed by local
textural variance. Traced through every pre-DRC stage on a synthetic gradient
wedge, then judged clearly better off on hardware. Set to 0.0 for the general and
face pipelines; the black-and-white pipeline was left alone on a separate
rationale.

**Per-channel autocontrast turned out to be an automatic white balance.** PIL
stretches R, G and B independently, and across 30 test photographs that moved
mean b\* by up to **+13 (yellow)** and a\* by **−17**, depending on how each
photograph's channels happened to sit. That is what the rating notes had been
calling "skin of baby is nearly all yellow". Turning it off scored **+0.45 of a
five-point rating, better on 9 photographs and worse on 3** — and the
photographs it improved most were exactly the ones with the largest measured
cast. It is now a three-way setting (`off` shipped, `preserve_tone` for one
stretch derived from luminance, `per_channel` for the old behaviour), and
existing configs migrate to `per_channel` so an upgrade changes nothing until
asked.

A cutoff of 0 is not the same as off: at cutoff 0 the stage still stretches each
channel to its own extremes. That is also why `calibration_raw` had never been
the pixel-exact passthrough it claimed to be.

**The saturation boosts came off, and that was the single largest win.**
`color_enhance` and `saturate_max_enhance` to 1.0, `adaptive_vivid` off:
**better on 17 photographs, worse on 1, p < 0.001**. It took 9 of the 10 "good"
ratings in a session where the then-current rendering scored 3 "ok or better"
against its 15.

## 4. The gamut-correction LUT

Worth recording in full, because it was built, shipped, found broken, fixed, and
then dialled back — and the final phase found it does much less than anyone
expected.

**Built by simulation rather than from the measurements.** 729 dense gamut points
per panel had been measured, but under a dither configuration that differs from
production, and 164 of those 729 points select a *different physical ink* under
the two configurations. Training on them would have baked in errors silently.
Instead the config-independent physics was fitted from solid-ink patches only
(Yule–Nielsen n plus measured primaries) and "requested RGB → predicted on-glass
Lab" was simulated by dithering a real canvas through the actual production
path. A first attempt at classifying which points were safe undercounted config
sensitivity by about 15× by checking a single nominal cell — it missed that error
diffusion spreads a flat field's values around the nominal, so nearby values
cross a hue-cutoff boundary even when the nominal agrees.

**Shipped, and immediately wrong on glass.** Colours near black and white came
back with a strong cyan-green cast — pure white rendered as (127, 226, 199) on
the F7. The inversion target was reference sRGB white and black, L\* 0–100, which
neither panel can reach. Searching for the closest achievable colour to an
*unreachable* target picks whatever is nearest in raw Lab distance, not anything
sensible. Disabled on both panels until fixed.

**Three fixes, each traced through the actual nearest neighbours being picked:**

1. *Adapt the target* to the panel's own reachable lightness range, using the
   same formula the renderer already applies to the image.
2. *Weight lightness over chroma* in the neighbour search. Neither panel's black
   ink is neutral, so a target demanding both exact L\* and perfect neutrality is
   unreachable near black — unweighted distance picked a lighter, more neutral
   candidate over pure black ink. A washed-out grey background reads as visibly
   wrong; ordinary ink impurity does not.
3. *Break ties toward RGB proximity.* Ink selection has real plateaus — near
   white, very different requests achieve identical measured Lab once the
   channels are high enough to force white ink — so unweighted interpolation
   averaged far-apart requests into something resembling none of them. That was
   the direct mechanism behind white coming back green.

A grey-axis sanity gate now blocks the build if the worst per-channel spread
across a nine-point grey ramp exceeds 20.

**Then dialled back to half strength.** At full strength the correction produced
a quantified skin artifact: skin-locus samples ran **+7.6 R−B warmer** on this
panel while neutral greys moved only +1.2 — specific to skin hues, not a general
miscalibration, and judged "too much" on glass. At blend 0.5 the warmth roughly
halves, and glass confirmed it.

**And a 0.85 chroma trim** composed on top, on a 33³ grid so the composition is
not resampled through a coarser one. A perceptual choice, not a measurement, and
recorded as a build-tool flag so a rebuild keeps it. Rated better on 11
photographs, worse on 3.

## 5. Measuring with a camera instead of an argument

The remaining questions were about photographs, not patches, and no metric had
earned the right to answer them. So renders were photographed off the glass.

| stage | |
|---|---|
| render | full 1200×1600, through the shipped pipeline, not a stand-in |
| push | `frame` upload over USB, ~30 s refresh, settle |
| capture | Raspberry Pi + ov5647, pinned exposure, RAW/DNG |
| correct | two-flat photometric separation (illumination + ~5–6 % additive glare) |
| register | content homography, then rectify to the panel grid |
| colour | camera→Lab fit, validated leave-one-out |
| judge | blind grouped rating pages, 5-point scale, opaque filenames, per-photograph arm shuffling |

The rating pages never name an arm, shuffle position per photograph, and stamp
the session so an export cannot be decoded against the wrong page.

## 6. What the ratings can and cannot support

Three properties of the data constrain every claim below, and all three were
measured rather than assumed.

**Absolute levels drift between evenings.** The same baseline renders scored a
mean of 2.32 on one page and 2.67 on another; "ok or better" moved from 45 % to
78 %. Nothing is pooled on absolute score. Every comparison here is a *paired
difference within one photograph*, which holds the picture and the sitting fixed.

**Rating noise is ±0.55 of a five-point step**, from repeat trials. Only about
**33 %** of the observed spread between versions is real signal.

**The achievable ceiling is 77.7 %, not 100 %.** A fitted objective reached
64.8–66.2 % held-out sign accuracy against that ceiling — genuinely predictive,
but not a substitute for looking.

## 7. The result

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

### The LUT round specifically

The correction LUT is a property of the `Display`, not of `ImageConfig`, so no
config knob can reach it; it needed its own round of 210 captures over 30
photographs weighted towards out-of-gamut content.

**The control worked.** `b50_t85` has the same blend and chroma trim as the
deployed LUT but half the grid resolution. It matched the deployed rendering on
17 of 19 photographs, which is what makes the rest of the column readable — a
difference elsewhere is the setting, not the resampling.

**Correction blend does nothing the eye can see.** Blend 0.00, 0.25, 0.50 and
1.00 all tie within ±0.11. Given §4, that is the most surprising result in this
document: months of work on the correction LUT, and at the end the blend it is
set to cannot be seen on real photographs.

## 8. The one lead this campaign could not close

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

## 9. What changed in the code at the end

The deployed configuration won, but `presets.py` had drifted away from it over
months of tuning through the web UI. **The repository was describing a
configuration that had just lost to the one actually running.** Since the
`DEFAULT_*_IMAGE_CONFIG` values seed only a fresh install, this affected new
installs rather than the running appliance — quietly, which is worse.

- **General and face defaults now match the judged configuration exactly.**
- **CLAHE is back on (1.75) for both**, reversing §3. That A/B compared a handful
  of photographs; the campaign put the same arm in front of 20 and it lost
  (`clahe_off`, worse on 11, better on 6). The larger blind evidence wins, and
  the older argument is kept in the source beside the lineage it still applies
  to rather than deleted.
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

## 10. Bugs this exercise uncovered

**The metric bank cached the wrong thing for LUT arms.** The cache key was
`(image, config, crop)` — but the correction LUT is a property of the *display*,
so every LUT variant shared a key with the baseline and the cache returned
whichever was measured first, reporting success. Any LUT comparison made before
this fix is void. Pinned by a test.

**An A/B tool silently compared a thing against itself.** The DRC harness
monkeypatched the CIELAB lightness function, but the shipped config runs DRC in
OKLAB, so the patched function was never called and both sides rendered through
the same curve. It looked exactly like "this change does nothing". The fix was to
dispatch the patch on the configured colour space — and the change turned out to
win clearly once the two sides actually differed.

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

**A dead camera rig could burn an hour of panel refreshes.** 55 refreshes over 27
minutes went into captures that could not succeed, because the host was offline.
The capture loop now aborts after five consecutive failures with none succeeding.

## 11. Claims that were made and withdrawn

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

## 12. What generalises about the method

- **Metrics picked winners the eye overturned, repeatedly.** A minimum-ΔE gamut
  map raised measured red-wall chroma from 11 to 33 while flattening the shading
  into a slab, and the chroma metric called it an improvement. Every phase after
  that was built to be vetoed by looking.
- **Judge paired, within a picture.** Absolute levels drift far more between
  sittings than most candidate changes are worth.
- **Report against the achievable ceiling**, never against 100 %. Self-agreement
  on repeat trials is the ceiling, and here it was 77.7 %.
- **A control arm is worth its panel time.** The LUT round is readable only
  because one arm changed the resolution and nothing else.
- **Archive the export next to its session.** Every browser download arrives with
  the same filename, and one round's notes were lost to that before it was
  noticed.

## 13. The apparatus

Code is in [`tools/camcal/`](../../../tools/camcal/README.md) plus
`tools/render_bank.py`, `tools/param_search.py`, `tools/judge_capture.py` and
`tools/judge_pick.py`. Captures and fitted artefacts are gigabytes of DNG under
the gitignored `build/camcal/` and are reproducible from the panel; the rig
calibration and the rating exports are not reproducible and are small.

## 14. Open, and deliberately not pursued

- **Chroma trim against out-of-gamut content** (§8) — the one live lead.
- **The neon metrics measure the wrong thing.** They measure chroma *loss*; the
  defect they were meant to catch is probably better defined as
  single-chromatic-ink coverage within a region.
- **Per-image configuration.** §8 is the argument for it, and the server already
  has both delivery paths (per-image overrides for the known library, a
  classifier branch for new images).
- **The DRC chroma stage still uses Huessen-hardwired palette constants** rather
  than the per-panel anchors the lightness stage uses. Harmless on this panel,
  wrong for the F7.
