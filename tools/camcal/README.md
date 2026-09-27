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
| `screen_arms.py` | measure how visible each candidate is before spending panel time — but see the caveat below |
| `bigrun_plan.py` | build the capture plan: photographs x renderings, balanced |
| `supervise.sh` | run a long capture to completion, resuming past transient failures |
| `pick_oog.py` | pick photographs weighted to content the panel cannot reproduce |
| `lut_plan.py` / `build_luts.sh` / `run_lut_round.sh` | build a family of correction LUTs and photograph them against each other |
| `focus_pair.py` | a two-arm page, for when a wide round has narrowed to one question |
| `rate_report.py` | decode a rating export against its key, translating numeric and left/right positions into arm names |
| `pool_pages.py` | pool several evenings into one verdict, paired within a photograph |
| `decompose_arms.py` | split each rendering's difference from the baseline into colour and texture |

`rate_report.py` replaces the lost `grouprate_report.py`. Three others from that
deletion were not rewritten, because the campaign closed before they were needed
again: `rating_corpus.py` (join rating exports to a measurement of the frame that
was rated), `rating_fit.py` (fit an objective to the ratings, reported against
the achievable ceiling rather than against 100 %) and `search_sanity.py` (check a
search result for degeneracy and extrapolation before spending panel time on it).
The method each encoded is recorded in the campaign write-up; the code is gone.

**Caveat on `screen_arms.py`.** It ranks candidates by how large a measured
change they make, on the assumption that a bigger change is likelier to be
judged different. The campaign tested that assumption and it does not hold:
magnitude and verdict correlate at rho = +0.03 over 233 judged pairs. Use it to
discard arms that change *nothing*, not to prioritise among those that do.

What the whole exercise concluded, and what it retracted:
[docs/screens/huessen_epf1301/rendering_campaign.md](../../docs/screens/huessen_epf1301/rendering_campaign.md).
