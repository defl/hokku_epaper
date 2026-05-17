# Dithering on the Spectra 6 e-ink panel

This document explains how the webserver converts arbitrary uploaded photos
into the 6-colour bitmap the EL133UF1 panel can display, and — more
importantly — *why* it does what it does. Every design choice here was driven
by a specific visible failure on a specific test image; the sections below tell
that story.

If you change anything in the dithering pipeline, **update this document to
match**. This file exists so a future maintainer (human or AI) doesn't have to
re-run the whole investigation. `AGENTS.md` carries the same reminder.

---

## 1. The hardware constraint

The 13.3" Spectra 6 (EL133UF1) is a 6-colour electrophoretic display. Each of
its 1200 × 1600 pixels can be exactly one of:

| # | Name   | Measured RGB       | Lab (L\*, a\*, b\*)     | Hue angle | Chroma |
|---|--------|--------------------|-------------------------|-----------|--------|
| 0 | Black  | (2, 2, 2)          | (0.55, −0.00,   0.00)   | 158°      | 0.0    |
| 1 | White  | (190, 200, 200)    | (79.86, −3.41, −1.18)   | −161°     | 3.6    |
| 2 | Yellow | (205, 202, 0)      | (79.17, −16.80, 79.56)  | 102°      | 81.3   |
| 3 | Red    | (135, 19, 0)       | (28.43,  46.30, 41.20)  | 42°       | 62.0   |
| 4 | Blue   | (5, 64, 158)       | (29.83,  22.18, −55.47) | −68°      | 59.7   |
| 5 | Green  | (39, 102, 60)      | (38.30, −30.62, 17.87)  | 150°      | 35.5   |

Two key properties:

- **The palette is sparse.** Six anchors in a 3D colour space leave huge gaps.
  A warm mid-tone like peach skin (L≈60, a\*≈+15, b\*≈+15) sits far from every
  primary.
- **The lightness range is clipped.** "White" is measured at L\*≈80, not 100 —
  the panel physically cannot reproduce paper-bright whites. Anything brighter
  than L\*=80 in the source has to be mapped down.

Dithering is how we fake the missing colours: we alternate palette pixels in a
pattern so the eye perceives an average.

---

## 2. Code architecture

The pipeline is split across several modules. Understanding the split helps
locate the right file to change:

```
webserver/webserver/
├── dither_config.py       DitherConfig dataclass (algorithm, LUT, serpentine, …)
├── image_config.py        ImageConfig dataclass (tonal chain + DitherConfig)
├── presets.py             Named presets + PRESET_IMAGE_CONFIGS dict
├── image_classifier.py    B&W / face detection → ImageConfig dispatch
├── image.py               Top-level pipeline orchestration:
│                            open_image_for_render(), render_panel_bytes(),
│                            render_preview_png(), compress_dynamic_range()
├── dither_constrained.py  Streaming error-diffusion (production, ≤ 50 MB):
│                            adaptive_saturate(), build_rgb_lut*(),
│                            dither(), dither_with_prep()
├── dither_unconstrained.py Full-canvas reference dither (quality comparison only,
│                            ~60 MB peak, NOT used in production)
└── dither.py              Backward-compat re-export shim
```

`dither.py` is a thin shim that re-exports everything from `dither_constrained`
so older imports don't break. New code should import directly from the right
module.

### Data flow for a full render

```
ImageClassifier.screen_config_for(path, sha1)
    └─ returns ScreenImageConfig { image_config, orientation, crop_threshold }

image.render_panel_bytes(img, cfg, orientation)
    ↓
_render_indices(img, cfg, orientation, FULL_W, PANEL_H)
    1. Resize / crop-to-fill → PIL canvas (uint8 RGB, ≤ 15 MB)
    2. _apply_prepare_enhancements()   # autocontrast → gamma → b/c/s
    3. Rotate canvas for landscape (−90°)
    4. np.asarray(canvas) → uint8 H×W×3 array
    5. dither_constrained.dither_with_prep(arr, cfg.dither, prep_stripe)
           prep_stripe(100-row stripe) →
               adaptive_saturate() + compress_dynamic_range()
               → float32 stripe (3.8 MB)
           _streaming_diffusion_dither()
               rolling 2–3 row error buffer, LUT lookup per pixel
    6. result_idx[padding_mask] = WHITE
    ↓
indices_to_panel_bytes(result_idx) → wire bytes
```

---

## 3. Config dataclasses

### `DitherConfig`

```python
@dataclass(frozen=True)
class DitherConfig:
    algorithm: "floyd_steinberg" | "atkinson" | "stucki" | "noop"
    lut_name:  "euclidean" | "hue_aware"
    serpentine: bool
    hue_cutoff_deg: float   # hue_aware only — how many degrees off-hue to forbid
    neutral_chroma: float   # chroma below which a palette entry is "neutral" (always allowed)
```

`cache_slug()` returns a 14-char SHA-256 prefix used to name panel `.bin`
files. Two renders of the same source with the same `DitherConfig` produce the
same cache key.

### `ImageConfig`

Wraps `DitherConfig` plus the tonal-chain settings:

```python
@dataclass(frozen=True)
class ImageConfig:
    dither: DitherConfig
    prepare_autocontrast_cutoff: float
    prepare_gamma: float
    prepare_brightness: float
    prepare_contrast: float
    color_enhance: float          # used only when use_adaptive_saturate=False
    use_adaptive_saturate: bool
    saturate_max_enhance: float
    saturate_low_chroma_thresh: float
    saturate_high_chroma_thresh: float
    scale_chroma: bool            # legacy: uniformly scale chroma in DRC
    adaptive_vivid: bool          # recommended: chroma-gated DRC (see §5b)
    vivid_chroma_low: float
    vivid_chroma_high: float
    # ── post-release additions (lenient: old configs load with defaults) ──────
    prepare_midtone: float = 1.0       # >1 lifts mid-tones independently of gamma
    clahe_clip_limit: float = 0.0      # local contrast (CLAHE); 0 = off, 2–4 typical
    clahe_keepout_feather: float = 0.015  # Gaussian feather at keepout boundary; 0 = hard edge
    prepare_usm_radius: float = 1.0    # unsharp mask radius (px)
    prepare_usm_amount: int = 120      # unsharp mask strength (percent)
    dither_noise: float = 0.0          # pre-dither Gaussian noise std (RGB units)
```

`AppConfig` holds three `ImageConfig` objects: `image_config_default`,
`image_config_bw`, and `image_config_face`. The classifier selects among them
per image.

---

## 4. Presets

Six named presets cover the three algorithms in plain (Euclidean LUT) and
hue-aware variants. The default is `atkinson_hue_aware`.

| Preset key                  | Algorithm       | LUT        | Adaptive sat | Adaptive vivid | Default? |
|-----------------------------|-----------------|------------|:------------:|:--------------:|:--------:|
| `floyd_steinberg`           | Floyd-Steinberg | euclidean  |              |                |          |
| `floyd_steinberg_hue_aware` | Floyd-Steinberg | hue_aware  | ✓            | ✓              |          |
| `atkinson`                  | Atkinson        | euclidean  |              |                |          |
| `atkinson_hue_aware`        | Atkinson        | hue_aware  | ✓            | ✓              | **Yes**  |
| `stucki`                    | Stucki          | euclidean  |              |                |          |
| `stucki_hue_aware`          | Stucki          | hue_aware  | ✓            | ✓              |          |

Plain presets use `ImageEnhance.Color(1.2)` (flat PIL saturation) because they
have no chroma-gated pipeline to protect near-neutral pixels.

Hue-aware presets enable `adaptive_saturate` and `adaptive_vivid` because
the hue-constrained LUT pairs well with chroma-selective saturation boosting —
the LUT's hue gate prevents accumulated error from escaping to a wrong-hue
palette entry even after the saturation boost.

Presets live in `presets.py` as `PRESET_IMAGE_CONFIGS`, a plain
`dict[str, ImageConfig]`. There are no partials; every field is spelled out so
each preset is fully inspectable.

---

## 5. Why the naïve approach fails — and how each stage fixes it

The naïve pipeline — resize, pick nearest palette per pixel, propagate error
Floyd–Steinberg style — produces four specific artifacts on our palette. Each
artifact drove a concrete countermeasure in the code.

### 5a. Blue speckles on warm skin

Source: skin tone L≈60, a\*≈+20, b\*≈+15 (warm pink).
Closest palette entry: **White** (L\*=80, a≈−3, nearly neutral).
Residual error propagated forward: large positive L, large positive a, large
positive b.

After several pixels the accumulated target drifts into a region where, despite
the hue mismatch, the nearest Lab-Euclidean palette entry is **Blue**
(L=30, a=+22, b=−55 — hue 110° off). Result: isolated blue speckles through
skin regions.

**Fix: hue-aware palette LUT** (`build_rgb_lut_hue_aware()`).
At LUT build time, for every (R, G, B) grid point the code computes the
pixel's hue angle and marks any palette entry "forbidden" if its hue differs
by more than `hue_cutoff_deg` (default 95°). `np.inf` replaces the forbidden
distances so `argmin` skips them. Neutral palette entries (chroma <
`neutral_chroma`, i.e. Black and White) are **always** allowed — they have
no meaningful hue, and forbidding them would leave very-low-chroma pixels
with no valid pick.

The LUT is a 32³ `uint8` cube (`32×32×32 = 32 768` cells); build takes ~40 ms
and is cached with `@lru_cache`. At dither time, every pixel lookup is O(1).

### 5b. Small saturated features vanishing

Source: a child's pink tongue, ~10 × 15 pixels; each pixel should be Red
(L=28) but source L≈55.

The dither's only way to produce perceived L≈55 is to alternate Red (L=28)
and White (L=80) pixels. But Atkinson's 6/8 damping throws away 25 % of the
error at every step. For a region only 10 pixels wide, the residual never
accumulates enough to force a Red pick before the neighbourhood has already
been assigned White. The tongue renders as near-uniform skin colour.

**Fix: adaptive saturation boost** (`adaptive_saturate()`).
Instead of PIL's flat `ImageEnhance.Color(factor)` (which multiplies *all*
chroma uniformly, including near-neutral noise), the code boosts only
already-colourful pixels:

```
factor(chroma) = 1.0                        if chroma ≤ low_thresh (5.0)
                 max_enhance (1.25)         if chroma ≥ high_thresh (15.0)
                 smooth linear ramp between
```

Pixels that are already saturated (tongues, lipstick, red shirts) get the
full 1.25× boost, pushing them firmly toward a coloured palette entry.
Near-neutral pixels (skin highlights, white umbrellas) are left at 1.0× —
no chroma amplification, no cascade into §5c.

The math lives in `adaptive_saturate()` in `dither_constrained.py`, which
works in float32 Lab space: RGB → Lab, scale a\* and b\* by `factor`, back to
RGB. This is applied per-stripe (100 rows at a time) to stay within the
memory budget (see §7).

### 5c. Phantom pink speckle in near-white regions

Source: white beach umbrella fabric, RGB≈(245, 240, 230), Lab chroma ≈ 5.
Technically non-neutral, but visually cream-white.

With flat `ImageEnhance.Color(1.2)`, the tiny warm bias is boosted to chroma
≈ 7. Floyd–Steinberg then cascades that small bias from pixel to pixel. After
several pixels the accumulated target crosses the decision boundary and Red
gets picked. Red's residual shoves everything around it cool again — but the
single Red pixel is visible against otherwise-white fabric, and the surrounding
correction produces pink-noise speckle.

**Fix: `adaptive_vivid=True` in `compress_dynamic_range()`.**

The Spectra 6 panel's white ink is measured at L\*≈80, not 100. Without
remapping, source pixels at L\*=100 and L\*=80 both end up at the White
palette entry, and the dither has no room to represent them differently.
`compress_dynamic_range()` linearly maps source L\* into the panel's range:

```
L'_pixel = black_L + (L_source / 100.0) × (white_L − black_L)
         ≈ 0.55 + L_source × 0.79
```

Without any chroma treatment, compressing L by 0.79× also shifts the
chroma-to-lightness ratio, making already-dim near-white pixels look
relatively more saturated after mapping. `adaptive_vivid` fixes this
with a chroma-gated remap applied alongside the L compression:

```
c_factor(chroma) = c_ratio (≈ 0.79)         if chroma ≤ vivid_chroma_low (5.0)
                   1.0                       if chroma ≥ vivid_chroma_high (15.0)
                   smooth ramp between
```

Near-neutral pixels (white umbrellas, sky) have their chroma scaled down
alongside L so that the tiny warm tint doesn't get a relative boost.
Saturated pixels (tongues, red shirts) keep their full chroma (`c_factor=1.0`)
so Red/Blue/Green remain reachable.

`compress_dynamic_range()` lives in `image.py` and is also called per-stripe.

### 5d. B&W photos developing a pink cast

Any near-greyscale photo has residual chroma of ~1–3 Lab units from JPEG
compression noise, film grain, or scanning artifacts. Flat saturation
amplifies that noise into visible colour.

**Fix: detect near-grayscale images and route them to a conservative
`ImageConfig`.** The `ImageClassifier` (see §6) runs B&W detection and, if
enabled, selects `AppConfig.image_config_bw` instead of the default. The B&W
config typically has `use_adaptive_saturate=False`, `color_enhance=1.05` (very
mild), `adaptive_vivid=False`, and an Euclidean LUT instead of hue-aware —
because there is no meaningful hue in a grey image to protect, and the
hue-aware LUT's extra cost buys nothing.

---

## 6. Image classifier — per-image config dispatch

```
ImageClassifier.screen_config_for(path, sha1)
  │
  ├─ B&W detection enabled? → is_grayscale(path)?
  │      → yes → use AppConfig.image_config_bw
  │
  ├─ Face detection enabled? → has_face(path)?
  │      → yes → use AppConfig.image_config_face
  │
  └─ otherwise → use AppConfig.image_config_default
```

Detection results (is_bw, has_face) are cached by sha1 of the original file
in `<cache_dir>/image_classifier.json`. A restart doesn't re-detect; clearing
the classifier cache (via `/api/classifier/clear`) forces fresh detection on
the next sync.

Detection fires **before** the render, not inside it. `image.py`'s render
functions take an explicit `ImageConfig` with no hidden fallback — the right
config is already chosen before `render_panel_bytes` is called.

`is_grayscale()` samples a 200×200 thumbnail and checks whether the 95th-
percentile Lab chroma is below `GRAYSCALE_CHROMA_THRESHOLD = 8.0`.

Face detection (`face_detect.py`) uses OpenCV's Haar cascade. Presence of a
face routes to `image_config_face`, which can be configured with settings tuned
for skin tone accuracy (e.g. slightly tighter sharpness, no adaptive-vivid so
skin highlights don't get overcooled).

---

## 7. Diffusion algorithms

All three algorithms share a single inner loop in
`_streaming_diffusion_dither()` (`dither_constrained.py`). The kernel is
the only thing that differs.

### Floyd-Steinberg

```
  # #  7/16
3/16  5/16  1/16
```

Four neighbours; sums to 1.0 — all error is distributed. Produces smooth
gradients. Can leave diagonal streaks in flat areas.

### Atkinson

```
  # 1/8 1/8
1/8 1/8 1/8
    1/8
```

Six neighbours; sums to 6/8 = 0.75 — 25 % of the error is discarded at each
step. This "6/8 damping" gives a characteristic look: high contrast, bold
edges, mid-tones that may clip but the result is punchy. The Apple LaserWriter
algorithm. Its damping is the reason adaptive saturation matters more for
Atkinson — the discarded error makes it harder for low-chroma pixels to
accumulate enough error to pick a saturated palette entry, so we pre-boost
them.

### Stucki

```
      # 8/42  4/42
2/42 4/42 8/42 4/42 2/42
1/42 2/42 4/42 2/42 1/42
```

Twelve neighbours across two rows; sums to 1.0. Wider diffusion kernel than
Floyd-Steinberg (reaches two rows ahead), produces a more even noise texture
without FS's diagonal streaks. Slower per pixel because of the larger kernel.

### Serpentine scan

Optional (`serpentine: bool` in `DitherConfig`). Alternate rows are processed
right-to-left with the kernel's `dx` values mirrored, so quantisation error
still flows only into unvisited pixels. Reduces directional streaking in smooth
gradients. Disabled by default for backward compatibility.

### noop

Nearest-palette quantisation per pixel with no error diffusion at all.
No blending, maximum sharpness, very visible banding. Used only in tests as a
fast sanity check that the LUT and indexing are correct.

---

## 8. Palette LUTs

Both LUTs are 32³ `uint8` cubes. At index time the algorithm does:

```python
ri = min(int(r / lut_scale), 31)
gi = min(int(g / lut_scale), 31)
bi = min(int(b / lut_scale), 31)
palette_idx = lut[ri, gi, bi]
```

The 8-bit quantisation from 256 → 32 steps before the lookup introduces an
error of at most ±4 counts, which is far below the dither noise and the
panel's ink variability.

### `euclidean` LUT

For every (R, G, B) grid cell, convert to Lab and pick the palette entry with
minimum Euclidean Lab distance. Simple, correct, fast to build.

```python
dists = ||lab_grid - PALETTE_LAB||²  # shape (32768, 6)
lut = argmin(dists, axis=1).reshape(32, 32, 32)
```

### `hue_aware` LUT

Same as Euclidean, but chromatic (chroma > `neutral_chroma`) palette entries
whose hue angle differs from the source pixel's hue by more than
`hue_cutoff_deg` are set to `np.inf` before `argmin`. This forces the nearest
*same-hue* palette entry to win, preventing the error-cascade hue-swaps
described in §5a.

```python
forbidden = (
    (pix_chroma > neutral_chroma)     # pixel is chromatic
    & (~neutral_pal)                  # palette entry is chromatic
    & (dh_deg > hue_cutoff_deg)       # and hue is too far off
)
dists = where(forbidden, inf, dists)
lut = argmin(dists, axis=1).reshape(32, 32, 32)
```

Both LUTs are cached with `@lru_cache`: `_cached_euclidean_lut()` (maxsize=1)
and `_cached_hue_aware_lut(hue_cutoff_deg, neutral_chroma)` (maxsize=16).
Build cost is ~40 ms each; subsequent calls are free.

---

## 9. The streaming (memory-constrained) architecture

A full 3200 × 1600 panel render at float32 is 60 MB as a flat buffer. The
original implementation held two such buffers (input + working) plus PIL
intermediates — peak ~580 MB on large sources. The production target is a Pi
Zero 2 W with 512 MB total RAM.

The streaming architecture brings peak RSS to **≤ 50 MB** (measured: 34–37 MB
on real photos) by never materialising a full-panel float buffer.

### Rolling error buffer

`_streaming_diffusion_dither()` holds only `max_dy + 1` rows of float32 RGB
working state:

- **Floyd-Steinberg** (`max_dy=1`): 2 rows × 3200 × 3 × 4 bytes = 77 KB
- **Atkinson / Stucki** (`max_dy=2`): 3 rows × 3200 × 3 × 4 bytes = 115 KB

Errors from row `y` flow into `rolling[1]` (one row ahead) or `rolling[2]`
(two rows ahead). At the end of each row the buffer slides up:
`rolling[i] = rolling[i+1]`, and `rolling[n-1]` is zeroed for the fresh row.

### Stripe-cached preprocessing

`adaptive_saturate` and `compress_dynamic_range` each allocate several
working buffers the size of their input (Lab, chroma, factor, xyz, …). On a
full panel that would be ~150 MB of transients. On a 100-row stripe
(3.8 MB per RGB stripe) the transients stay under 30 MB.

`dither_with_prep(canvas, cfg, prep_stripe, stripe_h=100)` accepts a callback:

```python
def prep_stripe(stripe_uint8: np.ndarray) -> np.ndarray:
    # stripe_uint8: (100, W, 3) uint8
    # returns: float32 of same shape, with adaptive_saturate + DRC applied
```

The streaming dither calls this lazily: when row `y` belongs to a new stripe,
the old stripe is dropped (`stripe_data = None`) **before** the new one is
allocated, so the GC can reclaim before the transients arrive.

### Why the unconstrained path still exists

`dither_unconstrained.py` is a self-contained copy of the original full-canvas
algorithm with no shared code with `dither_constrained`. It is used by
`test_dither_quality.py` to produce side-by-side PNGs for visual comparison
and as a regression baseline — if the streaming output ever diverges from the
reference algorithm, the test catches it. The unconstrained path is **never
used in production** (peak ~60 MB per dither call, ~230 MB full render).

### Memory budget breakdown

At peak during a real-photo render (3200 × 1600, post-step-9 streaming):

| Buffer | Size |
|---|---:|
| Resized panel canvas (PIL uint8 RGB) | 15.4 MB |
| Result-index plane (uint8) | 5.1 MB |
| Padding mask (bool) | 5.1 MB |
| One DRC'd stripe cache (100 × 3200 × 3 × float32) | 3.8 MB |
| Rolling error buffer (2–3 rows × 3200 × 3 × float32) | 0.1 MB |
| LUT (cached, 32³ uint8) | 0.03 MB |
| PIL + numpy bookkeeping, transients | ~5–6 MB |
| **Subtotal** | **~35 MB** |

See `docs/image_processing_memory_usage.md` for the full optimisation journey,
stripe-height benchmarks, and measurement methodology.

---

## 10. Source-image ingestion (`open_image_for_render`)

Before any pipeline stage runs, the source image is opened and normalised:

1. **EXIF rotation** (`ImageOps.exif_transpose`) — phone photos are typically
   stored landscape and tagged for portrait display; we rotate them up front.
2. **Convert to RGB** — drop alpha, handle palette/CMYK/L modes.
3. **Source cap and JPEG draft** — the panel's long edge is 3200 px.
   Sources larger than 3200 px on the long edge trigger JPEG draft
   (`Image.draft`) at the largest power-of-two scale factor that still lands
   above the cap. A 6000 × 4000 JPEG decodes at 3000 × 2000 without ever
   materialising the full 72 MB buffer. Non-JPEG formats fall back to
   `thumbnail()` after open (their decoders have no equivalent of draft).
4. **Return to caller** — the caller gets a `≤ 3200-long-edge` PIL image.
   `render_panel_bytes` calls `img.close()` as soon as the panel canvas has
   been composed (`release_input=True`), dropping the source buffer
   mid-render.

**PNG ceiling:** a 10 000 × 10 000 PNG requires ~285 MB decoded buffer before
any cap can fire. This is a documented hard limit — the memory test asserts
the peak exceeds 200 MB so any future fix that drops below 200 MB causes the
test to go red and forces a budget-spec update.

---

## 11. Tonal preparation chain

The preparation pipeline runs in two phases. The PIL phase operates on the
full uint8 canvas; the stripe phase operates on 100-row float32 windows.

### PIL phase — `_apply_prepare_enhancements()` (`image_abc.py`)

Runs before the canvas is rotated or handed to the dither strategy.

1. **Autocontrast** — `ImageOps.autocontrast(cutoff=prepare_autocontrast_cutoff)`:
   stretch the histogram, ignoring the darkest/brightest `cutoff`% of pixels.
2. **Gamma** — 256-entry uint8 LUT: `out[i] = (i/255)^gamma × 255`. Default
   gamma 0.85 brightens mid-tones to compensate for e-ink's darker appearance
   under typical ambient light.
3. **Midtone lift** — a second 256-entry LUT: `out[i] = (i/255)^(1/midtone) × 255`.
   Applied only when `prepare_midtone ≠ 1.0`. Independently lifts mid-tones
   without affecting the gamma curve; useful for pulling shadow detail into the
   panel's visible range without globally washing out highlights.
4. **Brightness / Contrast** — `ImageEnhance.Brightness` then `ImageEnhance.Contrast`.
5. **CLAHE** — if `clahe_clip_limit > 0.0` and `cv2` is available: convert
   to Lab, run `cv2.createCLAHE(clipLimit, tileGridSize=(8,8))` on the L\*
   channel only, convert back to RGB. CLAHE redistributes contrast locally
   within 8×8-pixel tiles, pulling out shadow detail and highlight texture
   simultaneously. **Note:** this stage trades pixel-level hue accuracy for
   structural legibility — see `image_quality.md §3.8` for the implication on
   metrics.

   When face keepout bboxes are present (`clahe_keepout_bboxes_canvas` non-empty),
   the face regions are protected from CLAHE. With `clahe_keepout_feather = 0` the
   original L\* channel is hard-pasted back after CLAHE (sharp boundary). With
   `clahe_keepout_feather > 0` a float32 mask (1 inside bboxes, 0 outside) is
   Gaussian-blurred with `sigma = min(canvas_w, canvas_h) * clahe_keepout_feather`,
   then used to linearly blend original and CLAHE L\* — eliminating the hard line
   at the face boundary.
6. **Unsharp mask** — `PIL.ImageFilter.UnsharpMask(radius=prepare_usm_radius,
   percent=prepare_usm_amount, threshold=3)`. Replaces the old fixed PIL
   `ImageEnhance.Sharpness` kernel. The `threshold=3` suppresses noise
   amplification in flat areas. A `prepare_usm_amount` of 0 is a no-op.
7. **Colour enhance** — `ImageEnhance.Color(color_enhance)`, only when
   `use_adaptive_saturate=False`. The flat saturation boost is skipped when
   the chroma-gated path is active (§5b).

All PIL operations work on the uint8 canvas. Each one peaks at about one
extra 15 MB image copy and recycles immediately. The float32 work doesn't
start until `dither_with_prep` is called.

### Stripe phase — `_prep_stripe()` closure in `ImageRenderer.render_indices()` (`image_renderer.py`)

Called per 100-row batch, returning float32 data ready for the dither loop.

1. **Adaptive saturation** (if `use_adaptive_saturate=True`) — Lab-space
   chroma boost gated on source chroma (§5b).
2. **Dynamic range compression** — `compress_dynamic_range()`: maps L\* from
   [0, 100] into [panel_black_L, panel_white_L]; applies a tanh soft shoulder
   for the top 15 % of the L\* range to prevent near-white regions from hard-
   clipping. With `adaptive_vivid=True`, chroma is preserved for saturated
   pixels (§5c).
3. **Pre-dither noise** (if `dither_noise > 0.0`) — adds `N(0, dither_noise)`
   Gaussian noise to the float32 RGB values (clipped to [0, 255]). Applied
   immediately before quantisation so the error-diffusion kernel sees
   independently perturbed inputs on each pixel. This breaks up the regular
   "worm" patterns that error diffusion produces in smooth gradients, at the
   cost of slightly higher pixel-level colour error.

---

## 12. Presets and the UI

The web UI's preset dropdown populates from `PRESET_META` (labels +
descriptions) in `presets.py`. Selecting a preset loads the full
`ImageConfig` into all the Advanced panel knobs via the JS `ditherState`
object. Touching any knob flips the preset label to "Custom (your edits)"
without changing values. Saving writes the complete nested config to
`config.json`.

The config round-trips through `_image_config_from_dict()` in `image_config.py`
which validates every field and raises on any missing key — no silent
defaults inside the serialised form.

---

## 13. Benchmark results

Three metrics are tracked. Full definitions — what they measure, how they are
computed, and how to interpret the numbers — are in
[docs/image_quality.md](image_quality.md). Short summary:

> **Metrics caveat:** these numbers measure pixel-level colour fidelity. They
> are reliable for comparing dither algorithms and LUT variants, but will
> produce misleading results for pipeline stages that improve structural
> legibility at the cost of per-pixel colour accuracy (CLAHE, pre-dither noise,
> midtone lift). When those stages are active, a rising `hue_error` or falling
> `sat_hit` does not necessarily indicate a quality regression. See
> [image_quality.md §⚠](image_quality.md#-the-fundamental-limit-metrics-measure-colour-fidelity-not-perceptual-quality)
> for a full discussion.

| Metric        | Good direction | What it catches                                  |
|---------------|:--------------:|--------------------------------------------------|
| `neutral_leak`| lower          | colour bleed into grey/neutral areas (blue tint) |
| `sat_hit`     | higher         | saturation preservation in colourful areas       |
| `overall_dE`  | lower          | mean CIE76 ΔE — overall colour accuracy          |

### Current production presets

Aggregate across 10 test images, `NumbaStreamingDither`, landscape,
`crop_to_fill_threshold=0.0`. Re-run `test_dither_quality_metrics` and update
this table whenever the pipeline changes.

| Preset                    | neutral_leak | sat_hit | overall_dE |
|---------------------------|:------------:|:-------:|:----------:|
| **atkinson_hue_aware**    |   **6.67**   | **0.683** | **29.64** |
| atkinson                  |     7.95     |  0.679  |   29.76    |
| stucki_hue_aware          |    10.48     |  0.658  |   32.07    |
| stucki                    |    11.77     |  0.653  |   32.26    |
| floyd_steinberg_hue_aware |    10.80     |  0.650  |   32.42    |
| floyd_steinberg           |    12.10     |  0.646  |   32.62    |

`atkinson_hue_aware` is the current default because it is the only preset that
wins on both `neutral_leak` and `sat_hit` simultaneously.

### Historical development comparison

These numbers were recorded during the V10 design exploration against 16 test
images. The image set and pipeline version differ from the current table above,
so the absolute values are not directly comparable — this table exists to
document *why* V10 was chosen over the alternatives.

| Variant                                              | neutral_leak | sat_hit | dE    |
|------------------------------------------------------|:------------:|:-------:|:-----:|
| pre-V10 atk_hue_aware (1.05 enhance)                |     6.05     |  0.721  | 35.24 |
| fs_hue_aware (now retired default)                  |    10.85     |  0.672  | 38.86 |
| V5 FS + luma/chroma-split diffusion                  |     6.35     |  0.661  | 37.33 |
| V6 atk + adaptive_sat 1.30                           |     6.55     |  0.758  | 35.45 |
| V7 atk + vivid + adaptive_sat 1.25                   |     4.84     |  0.703  | 34.90 |
| V8 atk + vivid + uniform 1.10                        |     4.68     |  0.673  | 34.91 |
| **V10 atk + adaptive_vivid + adaptive_sat 1.25**    |   **5.74**   | **0.752** | **35.27** |
| V11 same as V10, enhance 1.35                        |     6.02     |  0.764  | 35.38 |

V10 is the only variant that improves on the pre-V10 baseline on **both**
neutral_leak and sat_hit simultaneously. V7 beats V10 on neutral_leak but
loses on saturation (visible tongue loss). V11 pushes enhance to 1.35 and
skin tones start looking sunburnt on outdoor/skin-heavy photos.

---

## 14. File map

```
webserver/
  webserver/
    dither_config.py        DitherConfig dataclass + cache_slug()
    image_config.py         ImageConfig dataclass + _image_config_from_dict()
    presets.py              PRESET_IMAGE_CONFIGS, DEFAULT_PRESET, PRESET_META
    image_classifier.py     B&W + face detection, per-image config dispatch
    image.py                Full pipeline: open_image_for_render, _render_indices,
                             render_panel_bytes, render_preview_png,
                             compress_dynamic_range, _apply_prepare_enhancements,
                             _is_near_grayscale
    dither_constrained.py   Production streaming dither (≤ 50 MB peak):
                             adaptive_saturate, build_rgb_lut,
                             build_rgb_lut_hue_aware, dither, dither_with_prep,
                             _streaming_diffusion_dither, noop_dither
    dither_unconstrained.py Reference full-canvas dither (~60 MB dither peak):
                             dither() — quality comparison / regression baseline only
    dither.py               Re-export shim → dither_constrained
    memory_guard.py         RLIMIT_AS context manager (opt-in, not yet wired)

  tests/
    test_dither_quality.py  Side-by-side streaming vs unconstrained PNGs
    test_memory_budget.py   50 MB / render assertions (subprocess RSS sampling)
    test_render_worker.py   render_one() smoke test in the pool worker context
    _memory_helpers.py      Layer A (tracemalloc) / Layer C (subprocess + psutil)

docs/
  dithering.md                      ← this file
  image_processing_memory_usage.md  ← full memory optimisation journey
  image_processing_memory_usage.md  ← memory optimisation journey and measurements
```

---

## 15. Research references

- **Ostromoukhov 2001**, *A Simple and Efficient Error-Diffusion Algorithm*
  (SIGGRAPH). Variable per-pixel diffusion coefficients tuned for blue-noise
  characteristics. Our adaptive saturation and adaptive vivid are
  chroma-based analogs of the same core idea (let the pixel's own properties
  modulate the diffusion behaviour).
- **Hue-preserving chroma compression** — standard ICC/IS&T gamut-mapping
  practice. `adaptive_vivid` is a chroma-gated variant.
- **Atkinson (1984)** — Apple LaserWriter dither. 6/8 damping.
- **Stucki (1981)** — two-row, 12-neighbour kernel. Used in commercial
  typesetting systems; wider diffusion than FS without the FS diagonal artefacts.

---

## 16. When this document is stale

**Whenever dithering behaviour changes** — a new algorithm, a tweak to
`adaptive_saturate` thresholds, a palette recalibration, a new failure mode
being fixed — update this document. It's the one artifact that's supposed to
keep pace with the code for humans.

Key triggers:
- Adding a new `AlgorithmName` literal to `dither_config.py`
- Changing kernel weights or the number of neighbours in any algorithm
- Adding a new `LutName` or changing how the LUT is built
- Changing `DEFAULT_STRIPE_H` (update §9 and `image_processing_memory_usage.md`)
- Changing the tonal-chain order in `_apply_prepare_enhancements`
- Adding a new classifier detector or dispatch priority
- Changing palette calibration values in `display.py`
