# Camera-rig analysis scripts

These live here, tracked, rather than under `build/camcal/analysis/` where they
started. `build/` is gitignored, so when the `gamut-lut` worktree was removed on
2026-09-23 it took with it every capture, the 832-cell rating corpus, the fitted
objective, the rig calibration and all of these scripts. Only what had been
committed to `tools/` survived.

Captures and fitted artefacts are genuinely build output and stay under
`build/camcal/` — they are gigabytes of DNG and are reproducible from the panel.
Code and method are not reproducible, so they belong in git.

Paths inside these scripts still point at `build/camcal/` for their data; run them
from the repository root.

| script | what it does |
|---|---|
| `fetch_library.py` | download the live server's pool and pick a spread to photograph |
| `screen_arms.py` | measure how visible each candidate rendering is, before spending panel time |
| `bigrun_plan.py` | build the capture plan: photographs x renderings, balanced |
| `supervise.sh` | run a long capture to completion, resuming past transient failures |

Four more were lost in that deletion and are not yet rewritten:
`rating_corpus.py` (join rating exports to a measurement of the frame that was
rated), `rating_fit.py` (fit an objective to the ratings, reported against the
achievable ceiling rather than against 100 %), `search_sanity.py` (check a search
result for degeneracy and extrapolation before spending panel time on it) and
`grouprate_report.py` (decode a rating export against its key and plan,
translating both numeric and left/right positions into arm names). The method
each encoded is recorded in the campaign notes; the code is gone.
