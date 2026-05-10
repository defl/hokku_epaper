# Plan: Memory-constrained dithering (≤50 MB / thread, hard guarantee)

## Goal

A single dither render must hold its peak RSS to **≤ 50 MB**, on a Pi
Zero 2 W (512 MB total). This is a precondition for running multiple
dither workers in parallel — the per-thread budget is what makes the
N-1-cores parallel design feasible without OOM-killing the kernel.

This document plans **only** part 1 (per-render memory bound). Multi-
thread coordination + system-RAM headroom will be covered separately.

---

## 1. Where memory goes today

Panel dims (post-rotation buffer): `FULL_W=3200 × PANEL_H=1600` =
**5,120,000 pixels**. Reference sizes:

| Buffer                                | Size       |
|---------------------------------------|-----------:|
| uint8  RGB full panel                 | 15.4 MB    |
| float32 RGB full panel                | **61.4 MB**|
| float64 RGB full panel                | **122.9 MB**|
| uint8  index plane                    |  5.1 MB    |
| bool   padding mask                   |  5.1 MB    |
| 64³ uint8 LUT                         |  0.26 MB   |

`_render_indices()` currently holds, simultaneously:

```
composed (PIL uint8 RGB)              ~15 MB
padding_mask (bool)                    ~5 MB
arr      = np.asarray(composed, f32)  ~61 MB
compressed (f32, returned by DRC)     ~61 MB
canvas_d (PIL from compressed)        ~15 MB
result_idx (uint8)                     ~5 MB
                                  -----------
                              peak ≈ 162 MB
```

`compress_dynamic_range()` itself allocates **float64** intermediates
(`rgb`, `lab`, `chroma`, `xyz_out`, `linear`, `srgb`) → another
**~700 MB** transient in float64 on a full panel. This is the single
worst offender.

`_diffusion_workspace()` casts the canvas to float32 yet again (~61 MB)
because diffusion kernels mutate in place.

The **source image** is also unbounded — a 6000×4000 JPEG decodes to
~72 MB before any pipeline work starts.

So today's peak is **somewhere north of 250 MB** for a typical render,
and far worse for large sources.

---

## 2. Target budget breakdown (≤ 50 MB / render)

| Buffer (held concurrently)                      | Size       |
|-------------------------------------------------|-----------:|
| Decoded source image (uint8 RGB, ≤ 4 MP)        | ≤ 12 MB    |
| Resized canvas (uint8 RGB, full panel)          | 15.4 MB    |
| Output index plane (uint8)                      |  5.1 MB    |
| Working tile, float32 RGB (3200 × 100 × 3 × 4)  |  3.8 MB    |
| Lab / DRC working tile (float32)                |  3.8 MB    |
| Error-carry rows for diffusion (float32 RGB×3)  |  0.15 MB   |
| LUT (cached, shared across renders)             |  0.26 MB   |
| Misc (PIL handles, temporaries)                 | ~3 MB      |
| **Subtotal**                                    | **~43 MB** |

Headroom on top of that: ~7 MB for spikes during tile transitions and
PIL conversions.

The two non-negotiables:
- **No float64 anywhere in the hot path.** float32 round-trips
  through Lab/RGB are visually indistinguishable for dithering.
- **No simultaneous full-panel float buffer.** Operate on horizontal
  stripes (tiles) instead.

---

## 3. Concrete changes

### 3.1 Source decode: cap input resolution

`open_image_for_render()`:
- For JPEG: call `img.draft("RGB", (max_long, max_long))` *before*
  `load()` — JPEG decoder downsamples by factors of 2 during decode.
- For all formats: after open, if either dim exceeds `2 × max(FULL_W,
  PANEL_H)`, call `img.thumbnail(...)` to shrink in place (Pillow's
  `thumbnail` uses transient memory but releases the larger buffer).
- Convert to RGB last.

This caps the source at ≤ ~12 MB (≈ 4 MP × 3 bytes) before the
pipeline starts.

### 3.2 Resize to canvas: stream into stripes

`_render_indices()` currently does:
```python
img_resized = img.resize(...)               # full target buffer
composed.paste(img_resized, (x_off, y_off)) # full target buffer again
```

Replace with: pre-allocate one `composed` PIL buffer and resize
directly into a `crop()` view. PIL's resize already streams internally
when the source is larger than the target; the wasteful copy is the
intermediate `img_resized`. Drop it by feeding `img.resize()`'s output
straight into the paste, or by `crop`+`resize` per stripe.

### 3.3 `compress_dynamic_range`: float32 + tile-based

Rewrite as a generator producing one stripe of float32 RGB at a time:

```python
def drc_stripes(canvas_uint8, *, scale_chroma, adaptive_vivid, ...,
                stripe_h=100):
    H, W, _ = canvas_uint8.shape
    for y0 in range(0, H, stripe_h):
        y1 = min(y0 + stripe_h, H)
        rgb = canvas_uint8[y0:y1].astype(np.float32)   # 3.8 MB
        lab = _rgb_to_lab_f32(rgb)                     # in place where possible
        # apply DRC math in float32 directly on lab views, no float64
        out = _lab_to_rgb_f32(lab)
        yield y0, y1, out  # caller writes into final canvas / dither feed
```

Internal helpers (`rgb_to_lab`, `_lab_to_rgb`) get float32 variants
that reuse buffers (`out=` arg) and avoid the allocate-per-step
pattern.

The full-panel float buffer disappears entirely.

### 3.4 Prepare enhancements: in place on uint8 where possible

`autocontrast`, gamma LUT, brightness, contrast are all PIL `point()`
operations that already operate per-pixel on uint8. Keep these as PIL
calls on the full canvas (cheap — no float buffer).

`Sharpness` is a 3×3 convolution in PIL. Keep as PIL call.

`adaptive_saturate` currently allocates a float64 array for the full
canvas. Rewrite as a tile generator (same shape as DRC) and return
results to the caller as uint8 stripes.

### 3.5 Streaming dither

The dither algorithms only need:
- **Current row** (read+write)
- **Next row** (write only, error diffusion target)
- **Next-next row** for Stucki / Atkinson (writes only)

Rewrite `_diffusion_workspace` + the three dither functions to
operate on a **rolling float32 buffer** of `K` rows where `K = 1 +
max_y_offset_of_kernel`. For Floyd-Steinberg `K = 2`; Atkinson and
Stucki `K = 3`.

Sketch:

```python
def floyd_steinberg_stream(stripe_iterator, w, lut, lut_scale, palette):
    result_idx = np.empty((H, w), dtype=np.uint8)
    rows = np.zeros((2, w, 3), dtype=np.float32)   # rolling buffer
    cur = 0
    for y, src_row_f32 in stripe_iterator:        # one row at a time
        rows[cur] = src_row_f32 + rows[cur]       # add carried error, then clear next slot
        # ... existing per-pixel logic operating on rows[cur] / rows[1-cur] ...
        result_idx[y] = ...
        cur = 1 - cur
        rows[cur] = 0   # clear error target for the new "next" row
    return result_idx
```

The DRC stripe generator and the dither stream feed each other: DRC
produces stripes of float32; dither consumes one row at a time and
emits `result_idx` rows. Total live float32 memory: `2 × W × 3 × 4
≈ 75 KB`. Down from 60 MB.

For Atkinson/Stucki (kernel rows 0..2), use a 3-row rolling buffer.

The `result_idx` plane is allocated up front (5.1 MB uint8).

### 3.6 Result indices: write directly into output, no full-canvas-float

After dither: `result_idx[padding_mask] = 1` is fine — `padding_mask`
is bool (5 MB) and `result_idx` is uint8 (5 MB). Both stay in budget.

Better: build `padding_mask` on the fly per stripe and apply during
DRC (skip DRC math entirely on padded pixels), so we never hold the
full bool mask.

### 3.7 Hard guarantee (Linux)

Wrap the render call:
```python
import resource
soft_old, hard_old = resource.getrlimit(resource.RLIMIT_AS)
resource.setrlimit(resource.RLIMIT_AS, (50 * 1024 * 1024, hard_old))
try:
    return _render_panel_bytes_inner(...)
finally:
    resource.setrlimit(resource.RLIMIT_AS, (soft_old, hard_old))
```

If the pipeline blows the budget, `MemoryError` is raised at the
allocation site rather than triggering OOM-killer. The image is
recorded as failed with the error message; user sees it in the
"Failed conversions" list.

(Skip `setrlimit` on Windows — RLIMIT_AS isn't supported. Tests that
need the hard guarantee live on Linux only; see §4.)

---

## 4. Test design — peak memory measurement

Beginning/end RSS is useless; we need **peak during render**.

### 4.1 Three measurement layers

**Layer A — `tracemalloc` (in-process, deterministic).**
Use for unit tests of individual pipeline functions on small inputs.
- Starts a Python-heap allocation tracker; reports
  `get_traced_memory()` returning `(current, peak)`.
- Catches numpy + PIL Python-side allocations precisely. Misses some
  C-level transient buffers (PIL libjpeg internals etc.), so it
  understates real RSS — fine for unit tests asserting "this function
  doesn't allocate a 60 MB float buffer", not for end-to-end claims.

```python
import tracemalloc

def peak_python_heap(fn, *args, **kw):
    tracemalloc.start()
    try:
        fn(*args, **kw)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak
```

**Layer B — psutil sampling (in-process, real RSS).**
Background thread polls `psutil.Process().memory_info().rss` every
5 ms while the target function runs; returns peak observed.
- Catches everything (numpy, PIL C, decoder libs). Has sampling
  jitter — a 1 ms allocation spike between samples is missed. For a
  multi-second render that's not a problem; the budget-relevant
  buffers all live for many seconds.

```python
def peak_rss(fn, *args, sample_ms=5):
    import psutil, threading, time
    p = psutil.Process()
    peak = [p.memory_info().rss]
    stop = threading.Event()
    def watch():
        while not stop.is_set():
            peak[0] = max(peak[0], p.memory_info().rss)
            time.sleep(sample_ms / 1000)
    t = threading.Thread(target=watch, daemon=True); t.start()
    try:
        fn(*args)
    finally:
        stop.set(); t.join()
    return peak[0]
```

The number this returns is `peak_rss − baseline_rss`; subtract
`p.memory_info().rss` measured immediately before starting the
watcher to get the render delta.

**Layer C — subprocess isolation (most accurate, slowest).**
The test launches a fresh Python subprocess that imports the
pipeline, runs one render, then exits. Parent samples the child's
RSS via `psutil.Process(child.pid).memory_info().rss` until exit.
- Eliminates pytest / interpreter / cached-LUT contamination.
- Gives a single clean number that corresponds 1:1 to what a real
  worker thread would consume.
- Slow (~1 s startup overhead per run), so reserved for the
  "headline" assertion test.

```python
def peak_rss_in_subprocess(image_path, cfg_dict):
    import subprocess, sys, json, psutil, time
    code = "from webserver._test_render import _run; _run()"
    env = {**os.environ, "RENDER_IMAGE": str(image_path),
                          "RENDER_CFG": json.dumps(cfg_dict)}
    proc = subprocess.Popen([sys.executable, "-c", code], env=env)
    p = psutil.Process(proc.pid)
    peak = 0
    while True:
        try:
            peak = max(peak, p.memory_info().rss)
        except (psutil.NoSuchProcess, ProcessLookupError):
            break
        if proc.poll() is not None:
            break
        time.sleep(0.005)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"child render failed (rc={proc.returncode})")
    return peak
```

`webserver/_test_render.py` exposes `_run()` that reads env vars and
calls `render_panel_bytes()` once.

### 4.2 Test cases (tests/test_memory_budget.py)

Marked `time_intensive` (each subprocess case is ~5–10 s):

```
test_drc_stripes_peak_under_5mb            # Layer A, full-panel input
test_dither_stream_peak_under_2mb          # Layer A, full-panel input
test_full_render_peak_subprocess_under_50mb  # Layer C, real images
test_full_render_under_rlimit_50mb         # Layer C, with setrlimit;
                                           # asserts no MemoryError on
                                           # representative images
test_full_render_huge_jpeg_under_50mb      # 6000×4000 JPEG fixture
```

The Layer-C test runs across the same 4 images we already use as
classifier fixtures, plus a deliberately huge JPEG synthesised on
demand (`PIL.Image.new('RGB', (6000, 4000)).save(...)`) so it doesn't
have to ship in the repo.

Asserts use a generous slack (`< 50 * 1024 * 1024`) so transient
1-MB jitter doesn't break the test, while still failing fast on a
real regression (e.g. someone re-introducing a float64 buffer would
take peak to ~120 MB and trip immediately).

### 4.3 What the tests catch

| Regression                                  | Caught by                  |
|---------------------------------------------|----------------------------|
| float64 RGB buffer reintroduced             | A (DRC test) + C (full)    |
| Full-panel float32 RGB buffer reintroduced  | A (DRC test) + C (full)    |
| Source not downsampled before pipeline      | C (huge JPEG test)         |
| Dither workspace re-allocates full canvas   | A (dither test)            |
| Tile size accidentally raised to 1000 rows  | C (full-render test)       |
| Linux RLIMIT_AS guard wrapped wrong         | rlimit test (MemoryError)  |

---

## 5. Implementation order

1. **Test harness first** (Layer A, B, C helpers + one failing
   end-to-end test that asserts the current pipeline blows 50 MB).
   This locks in the measurement before we rewrite anything.
2. Source-decode cap (`draft` + `thumbnail`) — easy win, big impact
   on huge JPEGs.
3. Float32-only `compress_dynamic_range` (still on full panel) —
   smaller change, halves DRC peak.
4. Tile-based DRC stripes + tile-based adaptive_saturate.
5. Streaming dither (rolling 2- or 3-row error buffer).
6. RLIMIT_AS hard guard wrapper.
7. Re-run full test suite under the rlimit harness on Linux.

Each step keeps the tests green and incrementally lowers the peak
recorded by the Layer-C test. The Layer-C threshold drops from
"current peak − 5 MB" → "100 MB" → "75 MB" → "50 MB" as the work
lands.

---

## 6. Open questions

- **PIL's libjpeg buffers**: not Python-heap, not directly bound by
  `RLIMIT_AS` either (mmap'd). Need to verify on a Pi that decoding
  a 6000×4000 JPEG with `draft()` actually keeps RSS in budget. If
  not, switch huge JPEGs to a streaming decoder
  (`pillow-simd`/`turbojpeg`) or pre-shrink with a tool that drops
  the source memory before the pipeline starts.
- **HEIC** via `pillow_heif`: same concern. May need a pre-flight
  resize pass at the file level before opening.
- **Preview render** (`render_preview_png`) uses `max_side_px=800`
  → tiny canvas. Already well under budget — verify but don't
  optimise.
