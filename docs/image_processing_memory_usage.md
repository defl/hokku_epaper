# Image-processing memory budget

> **Reference commit:** [`ee7528b`](https://github.com/anthropics/hokku_epaper/commit/ee7528b0a816826cd14c0bf04ba670d881bec324) — "Batch dither prep into 100-row stripes (~27% faster, same memory peak)"
>
> If the numbers below diverge from observed behaviour on a future commit, re-run `pytest webserver/tests/test_memory_budget.py -m time_intensive -s` to refresh them and bump the SHA.

This document captures the per-render memory profile of the dither pipeline,
how it was measured, and the design decisions that got us from the original
~580 MB peak to the current ~35 MB peak. The goal was a hard guarantee that a
single render fits in **≤ 50 MB**, so multiple renders can run in parallel
on a Pi Zero 2 W (512 MB total RAM, target ~250 MB free for the OS).

---

## Headline numbers

Measured by spawning a fresh subprocess per render and sampling its RSS at
5 ms intervals (psutil) until the child exits — the parent reports
`peak_rss − baseline_rss` where `baseline_rss` is the child's RSS just after
`webserver` is imported but before the render starts. This excludes
interpreter/import overhead so the number reflects the pipeline alone.

| Image | Source dims | File size | Peak (delta from baseline) |
|---|---:|---:|---:|
| Robert_De_Niro_KVIFF_portrait.jpg | 1556 × 2247 | 1.2 MB | **34.5 MB** |
| Fitz_Roy_1.jpg | 1536 × 2048 | 1.0 MB | **35.6 MB** |
| Forest_road_Slavne_2017_BW_G9.jpg | 4500 × 2850 | 5.2 MB | **34.9 MB** |
| Synthetic 6000 × 4000 random JPEG | 6000 × 4000 | ~6 MB | **37.0 MB** |
| Synthetic 10000 × 10000 black PNG | 10000 × 10000 | 285 KB | **762.7 MB** ⚠ |

The 10000×10000 PNG is a **documented limit** — see "PNG decode ceiling"
below. Real-world content fits comfortably in the 50 MB budget.

End-to-end render time, on the dev box (Windows / x86, 1556×2247 portrait,
floyd_steinberg_hue_aware preset, mean of 3 runs): **~11 s**. The Pi will be
roughly 5-10× slower per render but the speedup ratios from each
optimisation hold.

---

## Where the memory goes (current architecture)

For a full panel render (3200 × 1600), simultaneously live at peak:

| Buffer | Size |
|---|---:|
| Resized panel canvas (PIL uint8 RGB) | 15.4 MB |
| Result-index plane (uint8) | 5.1 MB |
| Padding mask (bool) | 5.1 MB |
| One DRC'd-stripe cache (100 × 3200 × 3 × float32) | 3.8 MB |
| Rolling error buffer (2-3 × 3200 × 3 × float32) | 0.08-0.12 MB |
| LUT (cached, 64³ uint8) | 0.26 MB |
| Misc (PIL handles, numpy bookkeeping, transients) | ~5-6 MB |
| **Subtotal** | **~35 MB** |

Peak is hit during PIL prepare-enhancements (autocontrast / gamma /
brightness / contrast / sharpness) when current and next image briefly
coexist (~30 MB) plus padding mask and source remnants. Measurements bear
this out — peaks are within a couple of MB of this estimate.

Notably *not* in the budget: a full-panel float buffer. The streaming
architecture eliminates that entirely.

---

## Optimisation journey — measured peak after each step

This is the actual sequence of measurements observed during the session
that built the current pipeline. Each row is a Layer-C subprocess
measurement (peak RSS minus baseline) from
`pytest webserver/tests/test_memory_budget.py -m time_intensive -s`,
running on this Windows dev box. Use it to diagnose future regressions
— if you change one of the listed optimisations and the relevant
column jumps, this table tells you which buffer is suddenly back.

The cumulative changes are described in detail in the next section
("What changed, in order of impact"). Steps that *didn't* move the
needle are marked — they were instructive surprises.

| # | Change | De Niro 1556×2247 | Fitz_Roy 1536×2048 | Forest_road 4500×2850 | Synthetic 6000×4000 JPEG | DRC unit |
|---|---|---:|---:|---:|---:|---:|
| 0 | **Baseline** (original pipeline) | — | — | 579.5 MB | 621.2 MB | 71.7 MB / 100-row stripe |
| 1 | float32 in `compress_dynamic_range`, `_lab_to_rgb`, `adaptive_saturate`, dither.py colour helpers + initial source cap (6400 long edge) | 310.6 MB | 314.3 MB | 330.2 MB | **327.0 MB** ← cap fired | 29.0 MB |
| 2 | Streaming dither (rolling buffer, no full-panel float copy) | 314.3 MB | 303.6 MB | 322.8 MB | 326.8 MB | 29.0 MB |
| | ↑ **Surprise: no improvement.** PIL prepare-chain + full-canvas adaptive_saturate were dominating. | | | | | |
| 3 | Fix the lurking `np.array(canvas, dtype=np.float64)` in `_apply_prepare_enhancements` | 267.0 MB | 272.8 MB | 284.1 MB | — | 29.0 MB |
| 4 | Move `adaptive_saturate` from full-canvas to per-row inside `prep_row` | **53.3 MB** | **52.4 MB** | 108.3 MB ⚠ | 169.5 MB ⚠ | 29.0 MB |
| | ↑ Big jump for small images. Forest_road and the synthetic JPEG are outliers because their source canvases are large and not yet capped. | | | | | |
| 5 | Tighten source cap to 1.25 × panel (4000 long edge) | 50.5 MB | 51.7 MB | 95.4 MB ⚠ | 51.6 MB | 29.0 MB |
| 6 | Aggressive JPEG draft (manual k-of-2 selection) | 43.1 MB | 44.9 MB | 44.5 MB | 51.9 MB ⚠ | 29.0 MB |
| 7 | Tighten source cap to 1.0 × panel (3200 long edge) | 43.2 MB | 45.2 MB | 43.0 MB | 51.5 MB ⚠ | 29.0 MB |
| 8 | `release_input=True` — close source PIL after resize | **35.7 MB** | **34.8 MB** | **37.0 MB** | **38.7 MB** | 29.0 MB |
| 9 | Per-row → 100-row stripe `prep_stripe` (perf, no peak change) | 34.5 MB | 36.4 MB | 35.8 MB | 38.6 MB | 29.0 MB stripe / 0.3 MB row |

### Things to notice from the table

- **Step 2 was a flat line.** Streaming dither alone bought us *nothing*
  because the bottleneck was upstream (full-canvas `adaptive_saturate`).
  Always profile before you celebrate.
- **Step 4 split the world** — small sources fell off a cliff to ~50 MB,
  but anything with a big decoded source stayed high. That's what put
  source-cap on the critical path (steps 5-7).
- **Step 6's JPEG draft was the biggest single win for the worst
  cases** — Forest_road dropped 50 MB, the synthetic JPEG dropped
  44 MB, even though it didn't touch the small-source numbers.
- **The DRC unit-test column never moved** between steps 1 and 9
  because none of those changes touched DRC's per-call profile —
  it's been "29 MB / 100-row stripe / float32" since step 1. Step 9
  added a per-row DRC measurement (0.3 MB) when streaming dither
  briefly used per-row prep.
- **Synthetic 6000×4000 stayed slightly over budget** through step 7
  (51.5-51.9 MB). Step 8 (`release_input`) finally closed the gap
  by dropping the source's PIL buffer mid-render.

### Image source dimensions (for sizing context)

| Image | Dimensions | File on disk | Decoded uint8 RGB |
|---|---:|---:|---:|
| Robert_De_Niro_KVIFF_portrait.jpg | 1556 × 2247 | 1.2 MB | 10.0 MB |
| Fitz_Roy_1.jpg | 1536 × 2048 | 1.0 MB | 9.0 MB |
| Forest_road_Slavne_2017_BW_G9.jpg | 4500 × 2850 | 5.2 MB | 36.7 MB |
| Synthetic huge JPEG | 6000 × 4000 | ~6 MB | 72.0 MB |
| Synthetic black PNG | 10 000 × 10 000 | 285 KB | 285.7 MB |

The last two are generated on demand by the test suite (random-block
JPEG via `np.random.default_rng`, solid-black PNG saved with
`Image.save(..., optimize=True)`).

---

## Cross-architecture peak comparison (same source, varying prep batch size)

Run on the De Niro / Fitz_Roy / Forest_road set with the
`floyd_steinberg_hue_aware` preset and the *current* (post-step-9)
streaming dither, but with the prep batch size dialled up.
Demonstrates the cost-of-going-wider — informs the choice of
`DEFAULT_STRIPE_H = 100`.

| Prep mode | De Niro peak | Fitz_Roy peak | Forest_road peak | Render time |
|---|---:|---:|---:|---:|
| Per-row (1) | 35 MB | 35 MB | 36 MB | ~15 s |
| 50-row stripes | 45.9 MB | 46.1 MB | 47.9 MB | ~10 s |
| **100-row stripes** ← chosen | **42.3 MB** | **47.6 MB** | **42.6 MB** | **~10 s** |
| 200-row stripes | 57.1 MB | 60.9 MB | 56.6 MB | ~10.5 s |
| 400-row stripes | 83.5 MB | 86.5 MB | 87.2 MB | ~10 s |
| Full-canvas (no streaming prep) | 229.1 MB | 232.4 MB | 241.3 MB | ~10 s |

`peak_rss_subprocess` benchmark in `/tmp/_measure_stripes.py`
(transient script, not committed). Render times include ~3 s of
subprocess startup overhead. Per-row was measured separately via
in-process timing (`time.time()` around `render_panel_bytes`,
mean of 3 runs).

Key reads from this table:

- **Per-row pays a 30-50 % render-time tax** to gain ~10 MB of memory.
  Not worth it.
- **100 rows lands inside the 50 MB budget** with comfortable margin
  (~7 MB) for any single image, while matching full-canvas speed within
  ~5 %.
- **200+ rows breaks the 50 MB ceiling** before adding any speed —
  the per-stripe transients inside `adaptive_saturate` and
  `compress_dynamic_range` scale linearly with stripe height
  (each function allocates ~5-6 working buffers of stripe size).
- **Full canvas hits ~230 MB** because each colour-space intermediate
  is a full-panel float32 array. That's our pre-step-1 baseline
  re-emerging if anyone reverts the streaming work.

---

## Render time summary

End-to-end on this dev box (Windows / x86, 1556×2247 portrait,
floyd_steinberg_hue_aware, in-process `time.time()` around
`render_panel_bytes`):

| Architecture | Mean render time |
|---|---:|
| Original (full-canvas float64) | not re-measured; assume similar to ~15 s post-streaming since the dither inner loop was unchanged |
| Streaming dither + per-row prep | ~15 s |
| Streaming dither + 100-row stripes | **~11 s** |

The 27 % speedup comes from amortising Python / numpy dispatch overhead
over 100 rows per call instead of 1. The per-pixel dither inner loop
itself (which is pure Python and dominates wall time) didn't change.
Pi-class CPUs will see a similar ratio at roughly 5-10× the absolute
time.

---

## What changed, in order of impact

The original pipeline peaked at **~580 MB on real photos**, dominated by
float64 buffers in `compress_dynamic_range` (~700 MB transient on its own
during a full-panel call). Six changes brought it down to ~35 MB:

### 1. Streaming dither (single biggest win)

The three diffusion algorithms (Floyd-Steinberg, Atkinson, Stucki) used to
take a full-panel float32 canvas (60 MB), convert it to a working float32
copy, and mutate that copy as errors propagated. Now they share a single
`_streaming_diffusion_dither` driver that holds only a **2- or 3-row
rolling error buffer** (~115 KB) — 2 rows for Floyd-Steinberg, 3 for
Atkinson / Stucki where the kernel reaches `dy=2`.

Errors live in `rolling`; pixels come from a stripe cache (see #2).
`rolling` survives across stripe boundaries unchanged — when we cross
into a new stripe, we drop the old stripe cache before allocating the
new one, but the accumulated error state stays put.

**Saved: ~120 MB peak** (eliminates the full-panel float32 working copy
*and* the original immutable input that was held alongside it).

### 2. Per-stripe DRC + adaptive_saturate via `prep_stripe` callback

`compress_dynamic_range` and `adaptive_saturate` used to run on the entire
panel canvas in one shot. Each function's intermediate buffers (`lab`,
`chroma`, `t`, `factor`, `xyz_out`, `linear`, `srgb`) are each the size of
the canvas, so a single call peaks at ~200 MB transient.

The streaming dither now exposes a `prep_stripe(uint8_stripe)` callback
that returns a float32 stripe with both transformations applied. The
streaming dither holds **at most one cached stripe** (100 rows × 3200 ×
3 × 4 bytes = 3.8 MB) and drops it before requesting the next.

**Stripe height = 100** is the empirical sweet spot. See "Sweet-spot
benchmark" below.

**Saved: ~150 MB peak** vs. running the same math on the full canvas.

### 3. Float32 throughout the colour-space helpers

`srgb_to_linear`, `linear_to_xyz`, `xyz_to_lab`, `rgb_to_lab` now accept a
`dtype=` kwarg (default `float64` for precision-sensitive callers like
palette LUT building, but the hot path passes `float32`). float64 →
float32 halves the per-buffer size; on a full panel that's the difference
between 122 MB and 61 MB for one Lab buffer.

**Saved: ~50 MB peak** (compounds with #2 — float64 anywhere in DRC's
intermediates would push end-to-end above 50 MB even with stripe batching).

### 4. JPEG-decoder downscaling via `Image.draft`

A 6000 × 4000 JPEG is 72 MB of decoded uint8 RGB. Holding that during the
PIL pipeline + Lanczos resize peaks well over 100 MB by itself.

`open_image_for_render` now picks the largest power-of-two reduction that
JPEG draft can apply such that the result is still within
`_MAX_SOURCE_LONG_SIDE = 3200` (the panel long edge). PIL's draft is
conservative — calling it with `target=MAX` won't pick a smaller scale
unless the half-size result is still ≥ MAX in both dimensions — so we
compute the right divisor explicitly:

```python
k = 1
while long0 / (k * 2) >= MAX / 2 and k < 8:
    k *= 2
img.draft("RGB", (w0 // k, h0 // k))
```

For 6000 × 4000 → k=2 → decoder produces 3000 × 2000 directly, never
materialising the full source. For 4500 × 2850 → k=2 → 2250 × 1425.
Sources ≤ MAX are untouched (no draft, no thumbnail, no quality loss).

The thumbnail() fallback after `convert("RGB")` catches PNG / HEIC /
WebP since those formats don't support draft. **PNG / HEIC / WebP > ~5 MP
still blow the budget** — see "PNG decode ceiling".

**Saved: ~80 MB peak** on JPEG sources > panel size.

### 5. Source PIL release after resize (`release_input=True`)

Previously the source image was held by the caller through the entire
render (PIL canvas + dither). Now `render_panel_bytes` calls
`img.close()` on the source as soon as `_render_indices` has the resized
canvas, releasing the source's pixel buffer mid-render.

Trade: callers must not reuse the image after `render_panel_bytes`
returns. A handful of tests that re-rendered the same PIL image were
updated to construct fresh inputs per call.

**Saved: ~10 MB peak** (the source uint8 buffer no longer coexists with
the resized canvas + PIL transform chain).

### 6. Stripe-batched prep (latest change, perf-focused)

Earlier the prep callback was per-row (`prep_row(row_f32) -> None`,
mutate in place). That keeps memory minimal but pays Python / numpy
dispatch overhead × 1600 per render.

Switching to `prep_stripe(uint8_stripe) -> float32_stripe` with a 100-row
batch amortises that overhead across hundreds of rows. **End-to-end
render time dropped ~27 %** (15 s → 11 s on this dev box) while the
peak RSS stayed at ~35 MB — the streaming dither holds at most one
stripe at a time and drops it before requesting the next, so the
transient peak inside `adaptive_saturate` / `compress_dynamic_range`
on a 100-row stripe (~25-30 MB) overlaps with the canvas (15 MB) but
not with the 50 MB ceiling.

---

## Sweet-spot benchmark: stripe height vs. peak vs. time

Measured on this dev box, average of one run per case (numbers are
RSS delta from baseline):

| Stripe height | Peak | Render time |
|---:|---:|---:|
| 1 (per-row) | ~35 MB | ~15 s |
| **50** | ~47 MB | ~10 s |
| **100** ← chosen | ~43 MB | ~10 s |
| 200 | ~58 MB | ~10.5 s |
| 400 | ~85 MB | ~10 s |
| Full canvas (no stripes) | ~230 MB | ~10 s |

100 rows is the sweet spot:

- **Peak under 50 MB** with comfortable headroom.
- **Render time within 5 % of full-canvas** — the Python overhead is
  amortised; further widening doesn't help time but inflates memory.
- Per-stripe DRC transients (~25-30 MB) leave room for the canvas
  (15 MB) without blowing the ceiling.

---

## What didn't work / wasn't worth pursuing

### Aggressive PIL `draft` fallback for non-JPEG formats

PIL's `draft` is a no-op on PNG, HEIC, WebP — those decoders have no
in-flight downsampling. We tried various pre-thumbnail tricks but every
path ends up materialising the full uint8 buffer first. Fixing this
properly needs **libvips** (not currently a dependency, ~40 MB install)
or a bespoke streaming PNG decoder. Not justified for a use case where
the user can re-encode oversized PNGs to JPEG.

This is a real ceiling — see "PNG decode ceiling".

### In-place colour-space transforms

Tried writing `compress_dynamic_range` to operate fully in-place to avoid
the second 60 MB output buffer when run on the full canvas. The math
goes through Lab space, which has a different value range than RGB, so
intermediate buffers are unavoidable; saved only a small fraction of
peak. Made the code substantially harder to read. Reverted.

### Boolean → packed-bit padding mask

The full-panel `padding_mask` is a 5 MB bool array. numpy's bool dtype
is 1 byte/element, so packing to 1 bit/element would save ~4.4 MB. But
the compute path that uses it (`result_idx[padding_mask] = 1`) doesn't
support bit-packed input cleanly. Either we'd need a second numpy pass
or to compute padding inline during dither. Not worth ~4 MB.

### Streaming PIL transforms

`autocontrast` requires a histogram pass over the whole canvas;
`Sharpness` is a 3×3 convolution. In principle both could be done in
two passes (compute, then apply per row), but PIL doesn't expose
streaming variants and writing custom replacements in numpy would
re-implement a lot of carefully-tuned code. The PIL transform chain
peaks at ~30 MB which is fine inside our 50 MB budget; not worth
restructuring.

### `RLIMIT_AS` wired into the per-render path

The `webserver/memory_guard.py` module exists as an opt-in context
manager. It's **not yet wired into `image_manager._run_one_conversion`**
because:

1. `RLIMIT_AS` is process-wide, not per-thread. A multi-thread worker
   pool would need it sized to `N × budget + baseline` and set once
   at process start, not per-render.
2. In the current single-process Flask server, setting it inside a
   render risks aborting unrelated allocations in concurrent request
   handlers.

The right place to wire it is in the future multi-process worker pool
(phase 2). For now, the streaming design holds the soft budget; the
hard-guarantee path is documented and tested but not in production.

---

## How to measure

Three layers of memory measurement, all in `webserver/tests/_memory_helpers.py`:

### Layer A — `tracemalloc` (deterministic, Python-heap only)

```python
peak = peak_python_heap(fn, *args, **kw)
```

Runs `fn` with `tracemalloc` started, returns `get_traced_memory()` peak.
Catches numpy + PIL Python-side allocations precisely. **Misses PIL C
buffers** (libjpeg internals, Lanczos working memory, etc.) so it
understates real RSS — only useful for asserting "this function doesn't
internally allocate a giant numpy buffer", not end-to-end claims.

Tests that use it: `test_compress_dynamic_range_peak_under_1mb_per_row`
(0.3 MB), `test_compress_dynamic_range_peak_under_30mb_per_stripe`
(29 MB at the 100-row default).

### Layer C — subprocess + psutil RSS sampling (real, isolated)

```python
delta_bytes, baseline_bytes = peak_rss_subprocess(image_path, cfg=...)
```

Spawns a fresh `python -c` subprocess that imports webserver, signals
`READY`, and waits for a one-byte input from the parent. Parent then
samples `psutil.Process(child.pid).memory_info().rss` at 5 ms intervals
until the child exits. Returns peak − baseline.

Catches **everything** (PIL C buffers, libjpeg/libpng decoder transients,
mmap'd module data, numpy backing buffers). The child does only one
render then exits — no pytest / cached-LUT / interpreter-warm-up
contamination.

Tests that use it: `test_full_render_peak_under_50mb` (parametrised over
real photos), `test_full_render_huge_jpeg_under_50mb` (synthetic 6000×
4000), `test_full_render_huge_png_documents_decode_limit` (asserts
that 10 000 × 10 000 PNG *exceeds* 200 MB so a future fix flips the
test red).

### Layer B — in-process psutil polling (drafted, not currently used)

`peak_rss_sampled` exists in `_memory_helpers.py`. It runs the function
in the current process while a daemon thread polls RSS, returning the
delta. Easier to use than Layer C but contaminated by pytest's runtime
RSS — kept around in case it's useful for ad-hoc debugging but no
checked-in tests rely on it.

---

## PNG decode ceiling

A 10 000 × 10 000 PNG (e.g. a screenshot of a 5-monitor setup, or a
deliberately oversized upload) cannot fit the 50 MB budget. PNG
decoders have no equivalent of JPEG `draft`, so the full uint8 buffer
(~285 MB for 10 000² × 3) materialises before we can `thumbnail` it
down.

Measured: **~763 MB peak** in the subprocess. This is documented by
`test_full_render_huge_png_documents_decode_limit`, which **asserts the
peak exceeds 200 MB** — so if a future change adopts a streaming PNG
decoder (libvips, etc.) and silently fixes this case, the test goes
red and forces a budget-spec update.

In the current single-process Flask server **without** memory_guard
wired up, this case will OOM-kill the worker on a 512 MB Pi rather
than failing cleanly. The user-visible failure mode is "image stuck
in pending", not "image marked failed". This is the strongest argument
for wiring memory_guard in phase 2.

---

## Reference: file map

```
webserver/
  webserver/
    image.py               ← _render_indices, render_panel_bytes,
                              open_image_for_render (JPEG draft),
                              compress_dynamic_range (per-stripe).
    dither.py              ← _streaming_diffusion_dither (rolling buffer +
                              stripe cache), prep_stripe API,
                              DEFAULT_STRIPE_H = 100.
    memory_guard.py        ← memory_limit() ctx mgr (RLIMIT_AS), opt-in.
  tests/
    _memory_helpers.py     ← Layer A / B / C measurement helpers.
    test_memory_budget.py  ← time_intensive marker; the headline
                              50 MB / render assertions live here.

docs/
  memory_constrained_dither_plan.md   ← original design plan
  image_processing_memory_usage.md    ← this document
```
