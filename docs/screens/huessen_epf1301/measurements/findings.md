# What the measurements say

Huessen EPF1301 colour campaign: **1733 readings over 20 sessions**, X-Rite
ColorMunki Photo via ArgyllCMS `spotread`, reflective 45°/0°, one unit, the
meter clamped in a single position for the entire campaign. **All 1505 planned
patches measured — nothing missing.**

Every number below is regenerated from the committed dataset by
`tools/color_findings.py`, the same tool used for the F7's campaign. Run it to
check this document rather than trusting it.

Data and schema: [`data/`](data/). Tools: `tools/color_*.py`.

## The one thing worth fixing, not yet fixed

**The same DRC bug the F7 had exists here too, unfixed.**
`palette_measured_rgb` implies white at **L\* 79.86** and black at **L\* 0.55**.
This campaign measured the real panel at **white L\* 66.94, black L\* 10.86** —
white 13 L\* too bright, black 10 L\* too dark, a reachable range roughly 20 L\*
narrower than the table assumes. On the F7 this exact mismatch clipped both ends
of the tone range, collapsing half a test portrait into flat black.
`huessen_epf1301`'s `display.py` has no `drc_anchor_l` set (see
[`display.py`](../../../../python/hokku/screens/bigme_f7/display.py) for the F7's
fix and the mechanism). Not applied here — this campaign was measurement only,
and fixing it is a separate, deliberate change. Flagged, not fixed.

## The panel

| | |
|---|---|
| contrast | 29.4 : 1 (white Y 36.55 %, black Y 1.24 %) |
| gamut | 41 % of sRGB, 24 % of the visible locus |
| black ink | a\* +8.56, b\* −12.86 — not neutral, more blue-shifted than the F7's |
| noise floor | **0.656 ΔE**, from 38 anchor brackets per ink |
| Yule-Nielsen | n = 1.38, residual 1.50 ΔE, removing 63 % of linear-mixing error |
| peak dot gain | **+0.117** at 50 % nominal — prints like 61.7 % coverage |

## Head-to-head against the F7's campaign (1700 readings, final)

| | Huessen | F7 |
|---|---:|---:|
| contrast | 29.4 : 1 | 33.0 : 1 |
| gamut | 41 % sRGB / 24 % locus | 46 % sRGB / 26 % locus |
| peak dot gain | **+0.117** | +0.213 |
| Yule-Nielsen | n=1.38, removes 63 % | n=1.51, removes 70 % |
| noise floor | 0.656 ΔE | 0.35 ΔE |
| dedup agreement | 0.896 ΔE (4814 pairs) | 0.461 ΔE (166 pairs) |

**Tone response is the clearest, most consistent difference.** This panel's dot
gain sits at roughly half the F7's, and stayed there from the first anchor
bracket through the full 1733-reading dataset — a real characteristic of this
panel, not measurement noise settling out. A 50 % dither on the F7 prints like
71 %; on this panel it prints like 62 %.

**Stability is the other real difference, and it went the wrong way as data
accumulated.** The noise floor moved 0.512 → 0.723 → 0.656 ΔE across early,
mid, and final checkpoints of the campaign — it plateaued roughly double the
F7's, rather than tightening toward it. It is not evenly spread across inks:

| ink | mean ΔE to bracket mean | worst |
|---|---:|---:|
| black | 0.450 | 1.082 |
| white | 0.379 | 1.215 |
| blue | 0.501 | 0.930 |
| yellow | 0.499 | 1.983 |
| green | 1.167 | 1.864 |
| **red** | **0.942** | **4.090** |

Black, white and blue are close to the F7's numbers. **Red and green are
measurably less stable on this panel** — built from 38 brackets each, so this
is not a small-sample artifact. Any conclusion drawn from this dataset that
leans on red or green needs a wider margin than one built on the other four.

**Contrast and gamut are both a touch behind the F7's**, consistent across the
whole campaign — this panel's black is not quite as deep and its ink gamut a
little smaller. Real, but modest: both are within about 10 % of the F7's figures.

## Two things a reader would otherwise get wrong

**`duplicate_of` rows were not measured.** Validated on 4814 independent pairs
of identical rasters (vs the F7's 166) — mean agreement 0.896 ΔE, consistent with
the noise floor above. Inherited rows are real data; do not count them as
independent samples. Controls are excluded from inheritance in both directions.

**`interactive_confirmed` matters more here than anywhere in the F7's
dataset.** USB-interactive mode ([`interactive.h`](../../../../firmware/common/all/interactive.h))
lets a host suspend the device's own refresh schedule for the length of a
session — this board's equivalent of the F7's console-catch, but held
continuously rather than caught once per upload. It is plain RAM: any reset,
for any reason, silently clears it. During this campaign the device was
repeatedly observed resuming its own scheduled refresh — fetching a real image
from its configured server — in the idle gap between sessions (dial rotation,
meter repositioning), which a single per-session assertion did not catch.
Fixed mid-campaign by re-asserting immediately before every individual upload
and recording the result on the row. **`interactive_confirmed` is `true` on
all 1733 rows** — every reset that occurred was caught and recovered *between*
sessions, never silently mid-session.

## Rig notes worth keeping

- **Native USB is ~15× faster to upload than the F7's CH340 bridge** (~790
  KiB/s vs the F7's 115200-baud ceiling), and needs no console-catch polling —
  interactive mode holds the console open for the whole session instead. Panel
  refresh (~19 s here vs ~30 s on the F7) still dominates per-patch time either
  way.
- **A calibration-adjacent instrument failure is a real, separate failure
  mode from the well-documented mid-run "Communications failure" glitch.**
  Once this campaign, `spotread` reported `"Measurement misread" /
  "Dark calibration reading is inconsistent"` on the calibration step itself.
  No stamp is written on a failed calibration (`f7_calibrate.py` only records
  success), so nothing was left in a bad state; a bare retry succeeded
  immediately.
- **A venv can silently lose previously-working packages between sessions.**
  Twice during this campaign, packages that had worked cleanly through several
  prior sessions (`click`, `blinker`, plus a genuinely new gap —
  `colour-science`, which crashed the very first `skin`-phase patch that
  needed a CAM16UCS LUT) were missing on a later invocation, with no action in
  this project's own tooling that should have removed them. Cause unconfirmed.
  Mitigation: `pip check` before every session launch from partway through the
  campaign onward, and `pip install -r requirements.txt` to resync in one shot
  rather than chasing individual missing packages — the second occurrence
  additionally surfaced `bitstring`, `cryptography` and `coverage` missing,
  and a corrupted `opencv-python-headless` install (`cv2/config.py` absent)
  needing a separate `--force-reinstall`.

## What this dataset is now for

Coverage is complete: all 1505 planned patches, including the full 729-point
`gamut_dense` phase this measurement exists to support. The same **3-D
correction LUT** the F7's campaign was collected for has not been built for
either panel yet. That build is entirely offline — no panel, no meter, no
calibration window — and could now be attempted for both screens together.
