# End-to-end deb test (Docker)

Full-loop packaging + functional test:

1. **Build** the `hokku-server` `.deb` (fresh; skipped if sources are unchanged).
2. **Install** it in a clean `debian:bookworm` container — exactly as an
   appliance would (`apt-get install ./hokku-server.deb` runs the postinst that
   pip-installs numba / opencv / pillow-heif / pillow-avif / pillow-jxl /
   resvg-py / colour-science / ...).
3. **Boot** the *installed* `hokku-server` entry point against a test config.
4. **Upload** every image in [`../../images/test/`](../../images/test/) through
   `POST /hokku/api/upload`.
5. **Validate** that each image ends in its expected state and that every
   successfully converted image was actually dithered to the panel palette.

This catches packaging regressions the in-process unit tests can't see: a
missing dependency, a module that isn't shipped in the `.deb`, un-bundled
`templates/`/`static/`, an entry point that doesn't launch.

## Run it

From anywhere in the repo (needs Docker):

```sh
bash test_server/e2e/run.sh
```

Exit code `0` = every image matched expectations. Flags:

- `--rebuild-deb` — force a `.deb` rebuild even if sources are unchanged.
- `--no-cache` — `docker build --no-cache` (re-run the slow pip postinst layer).

The first run is slow (deb build + `apt`/`pip` install). Later runs reuse the
cached `.deb` (when `python/` sources are unchanged) and Docker layer cache.

### Windows / Git Bash

`run.sh` handles the Git Bash quirks itself: it disables MSYS path mangling
(`MSYS_NO_PATHCONV`) so docker's container-internal paths survive, converts the
build-context path with `cygpath`, and pins `DOCKER_DEFAULT_PLATFORM=linux/amd64`
(the locally cached `debian:bookworm` may be arm64 from Pi-image work — running
the amd64 host under qemu is slow/flaky for numba). Just run the command above.

## What "dithering worked" means here

The dithered preview PNG (`GET /hokku/api/dithered/<name>`) is reconstructed
straight from the rendered panel indices, so a genuinely quantised image uses
**only** the 6 Spectra-6 palette colours. The driver asserts `<= 6` distinct
colours — an un-dithered photo would have thousands. That is a real proof the
render pipeline ran, not just a status flag flipping.

## Expected outcomes

Expectations depend on the memory budget (see below), so there are two files:

- [`expectations.json`](expectations.json) — a **well-provisioned** server (the
  default `run.sh`, `--memory=1g`). The decode budget hits the 40 MP cap, so the
  38.9 MP Albi panorama renders. Only the 100 MP synth bomb is refused.
- [`expectations-constrained.json`](expectations-constrained.json) — a
  **memory-constrained** server (~512 MB budget). The decode budget lands
  ~23 MP, so Albi is gated at ingest and FAILS gracefully (must **not** OOM).

Any file not listed defaults to `ok`. `synth_black_10000x10000.png` is always
`reject` (100 MP > the 40 MP upload/bomb cap). Keep both files in sync with the
test set.

## Memory budget

The server sizes its **decode budget** (largest un-draftable image it will
decode) and **worker count** from a memory budget that is cgroup-aware — a
`docker --memory` / systemd `MemoryMax` / k8s limit is respected, unlike raw
`psutil` / `os.cpu_count()` (see `resource_budget.py` / `resource_limits.py`).
`run.sh` pins `--memory=1g` for a deterministic run (override `HOKKU_E2E_MEMORY`).

Measured behaviour on amd64 (the server's warmed baseline is ~290 MB — numba JIT
+ OpenCV + LUTs — so the amd64 floor is higher than the arm64 Pi's ~464 MB):

| container `--memory` | detected | decode budget | workers | result |
|---|---|---|---|---|
| `--memory=300m` | 300 MB | 0 px | 1 | under-provisioned warning; OOMs (baseline alone > 300 MB) |
| `--memory=512m` | 512 MB | 23 MP | 1 | OOMs during render (amd64 needs ~540 MB+) |
| `--memory=768m` | 768 MB | 40 MP | 1 | renders everything incl. Albi |
| `--memory=1g` (default) | 1024 MB | 40 MP | 2 | full PASS, no OOM |
| unconstrained (~2 GB) | ~1957 MB | 40 MP | 6 | full PASS, no OOM |

To exercise an **explicit self-imposed cap** (distinct from a real cgroup
limit), pass `-e MEMORY_BUDGET_MB=<n>`: the entrypoint writes `memory_budget_mb`
into the config, so the server limits its own decode budget / workers regardless
of the container's real RAM. `MEMORY_BUDGET_MB=300` in a roomy container makes
the server refuse every decode (budget 0) **without** OOM-ing — the graceful
under-provisioned path.

## The driver

[`../../tools/e2e_deb_smoke.py`](../../tools/e2e_deb_smoke.py) is a standalone,
HTTP-only driver (stdlib + Pillow). It does not import `hokku`, so the same
driver can validate this Docker container **and**, later, a real appliance over
the network:

```sh
python tools/e2e_deb_smoke.py \
    --base-url http://<appliance>:8080 \
    --images-dir images/test \
    --expectations test_server/e2e/expectations.json
```

That is the intended path for the Pi hardware-in-the-loop step and GitHub CI.
