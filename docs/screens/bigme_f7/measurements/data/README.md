# F7 colour campaign — raw dataset

The complete measurement record from the Bigme F7 characterisation campaign:
**1700 readings over 20 sessions**, covering 1434 of the 1436 planned patches.
Every reading carries its full reflectance spectrum.

This directory is the primary record. It exists so that any later analysis — a
3-D correction LUT, a different observer, a different illuminant — can be redone
without putting a meter back on glass. Reproducing it costs roughly 20 hours of
panel time spread across 20 human-attended calibration cycles, so it is stored
rather than regenerated.

## Files

| file | what it is |
|---|---|
| `campaign.jsonl` | one JSON object per line; the measurements |
| `campaign_spec.json` | the exact patch plan that was run (`spec_sha256` in the data ties to it) |
| `gamut_raster_groups.json` | dedup index: which planned patches dither to an identical raster |

## Provenance

X-Rite ColorMunki Photo via ArgyllCMS 3.5.0 `spotread -O -N -s -i D65`,
reflective 45°/0°. Bigme F7 firmware 1.2.11, one unit, poll URL parked at an
unroutable address so nothing could repaint mid-run.

The meter was **clamped in one position and never moved for the whole campaign**,
so every patch fills the entire panel. Patches were uploaded over USB with the
`frame` protocol — never served over HTTP. Patch aiming is therefore not an error
source anywhere in this dataset.

## Schema

Not every key appears on every row; rows are one of three kinds, distinguished by
`phase` and `anchor_tag`.

**Measurement identity**

| field | meaning |
|---|---|
| `uid` | stable patch id; the resume key |
| `phase` | `inks`, `tone_fine`, `lut_gain`, `algo_gain`, `lut_algo_cross`, `skin`, `plain_lut_grey`, `gamut_dense`, `control` |
| `label` | human-readable description |
| `rgb` | source colour fed to the dither |
| `config` / `config_name` | the `DitherConfig` used (algorithm, LUT, serpentine, hue cutoff, neutral chroma) |
| `ink_index` / `ink_fraction` | set on `inks` and Bayer-ramp rows |
| `bayer_k` | Bayer level k/8, where applicable |
| `raster_sha1` | SHA-1 of the dithered raster actually displayed |
| `is_control` | true for the interleaved drift-instrumentation repeats |

**The measurement**

| field | meaning |
|---|---|
| `xyz_d65_pct` | Bradford-adapted to D65, Y=100 for a perfect diffuser — the headline value |
| `median_raw` | median of the repeats, exactly as the instrument reported (D50) |
| `raw` | every individual repeat |
| `spectrum_pct` / `median_spectrum_pct` | reflectance %, 36 bands |
| `wavelengths_nm` | 380–730 nm, parallel to the spectra |
| `repeats` | how many reads were averaged (3 for spec patches) |

The spectra are the part that **cannot be reconstructed**. XYZ is a projection of
the spectrum through one observer and one illuminant; a session recorded without
spectra can never answer "how does this look under tungsten?" without going back
to the hardware.

**Provenance per row**

`t_unix`, `session`, `settle_s`, `instrument`, `instrument_args`, `spec`,
`spec_sha256`, `model`, `firmware`, `battery_mv`, `battery_mv_start`,
`anchor_tag` (`open`/`close` on the per-session primaries brackets),
`duplicate_of` (see below), `error`, `record`, `source`.

## Two things a reader will otherwise get wrong

**`duplicate_of` rows were not measured.** Many planned patches dither to a
raster byte-identical to one already measured — different source colours, same
ink pattern. Those inherit the earlier reading rather than re-displaying it, and
say so. Inheritance ran at ~54 % across `gamut_dense`. It was validated: 166
independent pairs that were measured twice anyway agree to ΔE 0.461, against a
measurement noise floor of ~0.44 ΔE. Treat inherited rows as real data, but do
not count them as independent samples.

Control patches are excluded from inheritance in both directions — they exist to
measure drift, and a control that silently reused an old reading would report
zero drift by construction.

**Sessions are bracketed, and the brackets are the error bars.** Every session
re-measures all six solid inks at open and close (`anchor_tag`). Across 20
sessions those brackets converge at ~0.44 ΔE, non-directional and uncorrelated
with elapsed time — which is what identifies the residual as panel refresh
repeatability rather than instrument drift. Any conclusion drawn from this
dataset that rests on a difference smaller than ~0.44 ΔE is inside the noise.

## Coverage and known gaps

1434 of 1436 spec patches. The 2 missing are `control` patches, deliberately
dropped: they would have cost a full calibration cycle to add two drift samples
to the twenty-plus brackets already collected. No gamut, tone, or skin patch is
missing.

## Redaction

The `firmware` field is the device's own config echo, captured so a reader knows
the panel was awake and on `cfg_ver=2`. On 20 rows it contained the LAN address
of the test unit; `ip=`, `gw=` and `nm=` are replaced with `<redacted>`. **No
measured value is altered** — a field-by-field diff against the working copy
differs only in `firmware`, only on those 20 rows.

## Caveats

- **One unit, one meter, one clamp position.** Panel-to-panel spread is
  unmeasured. These are this screen's numbers, not the model's.
- **The meter rests on the glass.** 45°/0° rejects the specular component, which
  is correct, but contact pressure on a flexible panel may shift the surface.
  Unquantified.
- **Dither gate parameters matter more than they look.** `hue_aware` at the
  shipped gate (`hue_cutoff_deg=95`, `neutral_chroma=8`) is bit-identical to
  `euclidean` on skin tones; at `30`/`12` it is not. Rows record the full config
  for exactly this reason — never compare LUTs without checking the gate.

See [`../findings.md`](../findings.md) for what was concluded, including what was
concluded wrongly and retracted.

## Room-light and human-verdict data

Added 2026-08-11. These are not part of the campaign proper but live here for the
same reason: neither can be regenerated.

| file | what it is |
|---|---|
| `ambient.jsonl` | measured room-light SPD, one JSON object per reading session |
| `ab_verdicts.jsonl` | one line per blind A/B trial — the human verdict |
| `ab_plan.json` | the trial list, including the side assignment held back during judging |

`ambient.jsonl` is what makes the campaign's spectra usable for a real room:
reflectance is a property of the ink, but an *appearance* needs an illuminant, and
D65 is a standard rather than anywhere anyone sits. Each record carries the median
of several reads plus the full 380–730 nm SPD.

**A reading is only valid for the dimmer setting it was taken at.** The measured
lamp is dim-to-warm, so its correlated colour temperature moves with brightness by
design; the `note` field records the setting and is not optional in practice.

`ab_verdicts.jsonl` is irreplaceable in a different way — it is an hour of one
person's judgement, and re-running it produces a different sample rather than the
same one. `winner` is resolved at write time so the verdicts stand alone if the
plan is ever lost. See [`../ab_session_2026-08-11.md`](../ab_session_2026-08-11.md)
for conditions, validity checks and what the session concluded.
