# Blind A/B session, 2026-08-11 — what a human actually preferred

28 blind side-by-side trials on the glass, one judge, one sitting. The point was
not to pick a winner: it was to produce **verdicts a candidate metric can be
scored against**, because three earlier conclusions in this project were reached
by a metric and then overturned by a person looking at the panel.

Data: [`data/ab_verdicts.jsonl`](data/ab_verdicts.jsonl), plan in
[`data/ab_plan.json`](data/ab_plan.json). Tool: `tools/ab_session.py`.

## Conditions

| | |
|---|---|
| illuminant | measured, 2706 K, 75 lux — [`data/ambient.jsonl`](data/ambient.jsonl) |
| viewing distance | 25 in → **55.9 px/degree** at this panel's 128 PPI |
| each half | 400×480, subtending 7.1° × 8.6° |
| tonal chain | bypassed (only `dither()` ran), as in `f7_ab_compare.py` |

Blind throughout: sides assigned by seeded coin flip, never printed, and the
prompt said nothing about what differed or where to look. The plan reproduces
bit-for-bit from its seed, so the mapping is recoverable without having been
visible during judging.

## Is the session valid

**Catch trials 2/2 passed** — the `bw` two-ink arm lost both times, once with the
unprompted note *"right is black and white, not a real comparison of nuance"*.

**Self-agreement 4/5 on repeats.** That is the ceiling: no metric can be
expected to predict these verdicts better than the judge predicts himself. The
single disagreement was `tone_closed` vs `production` on the same image, judged
both ways — which is itself the finding for that arm.

## Tone correction: rejected

| contrast | result |
|---|---|
| naive open-loop curve vs production | **production 4–0** |
| closed-loop curve vs production | 2–2, and the repeat flipped |
| closed-loop vs naive | closed-loop 2–0 |

The naive curve — the inverse of the open-loop dot-gain arch — was rejected
unanimously, with *"left looks very compressed which makes it ugly"*. This is the
over-correction [`findings.md`](findings.md) predicted, now confirmed by a person
rather than argued from theory.

The closed-loop curve is a **wash**: indistinguishable from production, with the
repeat landing on the opposite side of the same image.

**Recommendation: ship no tone curve.**

### But the stated mechanism was wrong

`findings.md` argues no correction is needed because *"error diffusion is a closed
loop … end-to-end the rendered mid-tones already land close"*. Measured against
the 191 neutral-request **pipeline** patches — the closed loop, end to end — that
is not what happens:

| requested | linear-mix L\* | measured L\* | dot gain surviving |
|---:|---:|---:|---:|
| 78 | 38.02 | 32.30 | **+5.72** |
| 98 | 45.73 | 40.57 | +5.16 |
| 119 | 52.45 | 47.48 | +4.97 |
| 140 | 58.01 | 54.58 | +3.43 |

**+3.41 L\* mean, +5.72 peak**, against a 0.35 ΔE noise floor. The loop is closed
over the renderer's *model* of the panel (`palette_measured_rgb`, mixed linearly
in sRGB), not over the panel — nothing measures the glass, so it cannot correct a
mixing error it has no knowledge of.

The conclusion survives anyway, for a different reason: the net error is two
errors partly cancelling, since mixing in gamma-encoded sRGB lightens midtones
while optical dot gain darkens them. Correcting only one of them is what
over-corrects. **Right answer, wrong reason** — worth recording, because the wrong
reason predicts that a *better* curve would help, and the human test says it does
not.

A useful by-product: Yule-Nielsen predicts real closed-loop renderer output to
**0.52 L\* mean**, at the noise floor. The forward model is now validated against
the shipping pipeline, not just against open-loop Bayer patches.

## Illuminant-adaptive palette: the one candidate that survived

Both sides were **our own measured palette**, differing only in the illuminant it
was computed under — deliberately not production, whose palette comes from a
third-party table and would have varied provenance and illuminant at once.

| | |
|---|---|
| ambient-derived vs D65-derived | **2 wins, 0 losses, 2 ties** |
| repeats | **2/2 agreed** — the only perfectly reproducible contrast |

Zero losses, and it confirmed a prior prediction. Re-illumination analysis said
black and white shift only 1.6–2.0 ΔE after adaptation while chromatic inks shift
10–14, so a black-and-white image should tie. `neutral_forest` tied. The other tie
came with *"both are far too light"* — a different failure dominating.

Wins were described modestly — *"slightly less harsh cut between blue and white"*,
*"water reflection slightly more detail"* — consistent with a real but subtle
effect at 75 lux, where chromatic discrimination is already reduced.

## Prior verdicts, partially reproduced

`euclidean` lost 2–0, matching the record. `oklab` (1–1–1) and `cam16ucs` (1–1)
came out **mixed**, where earlier sessions rejected all three on sight. One trial
reproduced the documented finding exactly: production won on skin with *"much
more natural skin colours"*.

That partial reproduction is a caution about the earlier verdicts, the current
ones, or both — different illuminant, different content, different day.

## Caveats

- **n is 2–4 per contrast.** Directional, not definitive. Only the illuminant
  result has clean repeats behind it, and that is still 4 trials.
- One judge, one sitting, one illuminant, tonal chain bypassed.
- **Six trials hit a crop bug.** `grayscale_linear_bar` is 1200×300 against a
  400×480 half, and `crop_to` centre-crops to aspect — so it showed roughly a
  fifth of the ramp, blown up. Trials 6, 11, 17, 20, 25 and 26 are affected;
  trial 6 was answered *"no idea what I am looking at"*. Several still gave usable
  answers, since banding survives a bad crop. Re-run with a letterboxed fit if
  those contrasts ever matter again.

## Where this leaves the work

Illuminant-adaptivity was ranked fifth and "second-order" before the measurements
and is now the only change with evidence behind it. The tone curve is closed. The
3-D correction LUT over the dense gamut phase remains unstarted and unaffected by
any of this.
