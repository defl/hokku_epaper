# TODO

Items captured during development that aren't urgent enough to block current
work but worth picking up later. Add new entries at the top.

## Webserver

### Parallelise the dither pipeline across cores under a memory budget

The dither pipeline currently runs single-threaded — `_sync_pool_inner`
processes one image at a time. On a Pi Zero 2 W (4 cores) that means three
cores sit idle while a 1200×1600 conversion takes ~30–45 s. With the 2.2.2
memory work (~68 MB peak per image) we have headroom: a 512 MB Pi with
~150 MB free could in principle run two converters concurrently and still
fit, four if we pre-decode aggressively.

Goals:

- Constrain peak memory per worker so we know exactly how many can run
  side-by-side. Current peak (~68 MB) is per-image, but `Image.draft()`
  decode + Lab-buffer reuse means the steady-state working set is smaller.
  Measure properly with `tracemalloc` snapshots at the highest-water mark
  inside `_convert_image`.
- Run `min(cpu_count() - 1, mem_budget // worker_peak)` worker threads
  (or processes — see below). Leave at least one core for Flask + the
  web GUI poll loop so the browser stays responsive while bulk-converting.
- Decide thread vs process. NumPy releases the GIL during the heavy ops,
  so threads might be enough; but the dither inner loop is a Python
  for-loop (Floyd-Steinberg / Atkinson) and won't parallelise with threads.
  ProcessPoolExecutor with `forkserver` start method avoids re-importing
  the world per task. Tradeoff: serializing 30 MB image arrays across
  the process boundary eats 1–2 s per image — not worth it for short
  conversions. Best plan is probably: keep the heavy NumPy parts in
  threads (GIL released); only spawn processes when a Python loop is
  actually the bottleneck.

Open questions:

- Can we push this far enough that we always use all `cpu_count()` cores,
  or does the memory budget cap us at fewer? On a 1 GB Pi Zero 2 W (rare)
  we'd be unconstrained. On 512 MB we're probably stuck at 2–3 workers.
- If memory caps us, the policy should be `n_workers = min(cores - 1,
  available_mem // worker_peak)` recomputed at startup. Configurable in
  `config.json` for users who know their host better than we do.
- Need to gate the failed-marker / quarantine logic against concurrent
  workers — currently `_processing_marker` assumes one converter at a
  time. With N parallel workers, each writes its own per-image marker
  (which already works since markers are keyed by filename), but the
  `_promote_processing_markers_to_failed` startup pass is fine because
  it runs before any workers spawn.
- Pi Zero 2 W has known thermal throttling under sustained 4-core load.
  Worth checking that we don't end up worse-off than serial because the
  CPU clamps to 600 MHz.
